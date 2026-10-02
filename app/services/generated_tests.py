import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    CompilationError,
    CompilationInputError,
    CompilationInvariantError,
    CompilationPersistenceError,
    GeneratedTestNotFoundError,
    InvalidScenarioSnapshotError,
    RegressionInputError,
    RegressionNotFoundError,
    RegressionNotSynthesizableError,
    RegressionValidationError,
    RunNotNormalizedError,
    UnsupportedAssertionError,
    UnsupportedSpecVersionError,
)
from app.core.logging import bind_log_context
from app.db.models.generated_test import GeneratedTest
from app.db.models.regression_spec import RegressionSpecOperation
from app.regression_runtime import HARNESS_ID, HARNESS_VERSION
from app.schemas.generated_tests import GeneratedTestRecord
from app.schemas.regression_specs import RegressionRecord
from app.services.attributions import AttributionStore
from app.services.pytest_compiler import (
    ARTIFACT_VERSION,
    ASSERTION_HELPERS,
    COMPILER_VERSION,
    SUPPORTED_SPEC_VERSION,
    PytestCompiler,
    canonical_json,
    validate_compilation_input,
)
from app.services.regression_input import load_evidence
from app.services.regression_validation import validate_regression
from app.services.runs import RunService

logger = logging.getLogger(__name__)


class GeneratedTestService:
    """Compile bounded local work synchronously while locking its persisted parent."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        compiler: PytestCompiler,
        runs: RunService,
        attributions: AttributionStore,
    ) -> None:
        self.sessions = sessions
        self.compiler = compiler
        self.runs = runs
        self.attributions = attributions

    def get(self, generated_test_id: UUID) -> GeneratedTestRecord:
        try:
            with self.sessions() as session:
                row = session.get(GeneratedTest, generated_test_id)
                if row is None:
                    raise GeneratedTestNotFoundError
                return GeneratedTestRecord.model_validate(row)
        except SQLAlchemyError as exc:
            logger.exception("Generated test read failed")
            raise CompilationPersistenceError from exc

    def compile(
        self, regression_spec_id: UUID, request_id: str
    ) -> tuple[GeneratedTestRecord, bool]:
        error: CompilationError | None = None
        with bind_log_context(
            regression_spec_id=str(regression_spec_id),
            request_id=request_id,
            compiler_version=COMPILER_VERSION,
            stage="compilation",
        ):
            logger.info("Compilation requested")
            try:
                with self.sessions.begin() as session:
                    parent = session.execute(
                        select(RegressionSpecOperation)
                        .where(RegressionSpecOperation.id == regression_spec_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if parent is None:
                        raise RegressionNotFoundError
                    if parent.status != "completed" or parent.disposition != "synthesizable":
                        raise RegressionNotSynthesizableError
                    existing = session.scalar(
                        select(GeneratedTest).where(
                            GeneratedTest.regression_spec_id == regression_spec_id,
                            GeneratedTest.compiler_version == COMPILER_VERSION,
                            GeneratedTest.harness_identifier == HARNESS_ID,
                            GeneratedTest.harness_version == HARNESS_VERSION,
                        )
                    )
                    if existing is not None:
                        logger.info(
                            "Existing compilation returned",
                            extra={
                                "generated_test_id": str(existing.id),
                                "status": existing.status,
                            },
                        )
                        return GeneratedTestRecord.model_validate(existing), False
                    row = GeneratedTest(
                        regression_spec_id=regression_spec_id,
                        version=ARTIFACT_VERSION,
                        framework="pytest",
                        compiler_version=COMPILER_VERSION,
                        harness_identifier=HARNESS_ID,
                        harness_version=HARNESS_VERSION,
                    )
                    try:
                        if parent.spec is not None and not isinstance(parent.spec, dict):
                            raise CompilationInputError
                        if parent.version != SUPPORTED_SPEC_VERSION or (
                            parent.spec is not None
                            and parent.spec.get("version") != SUPPORTED_SPEC_VERSION
                        ):
                            raise UnsupportedSpecVersionError
                        raw_assertions = (parent.spec or {}).get("assertions")
                        if isinstance(raw_assertions, list) and any(
                            isinstance(assertion, dict)
                            and isinstance(assertion.get("kind"), str)
                            and assertion.get("kind") not in ASSERTION_HELPERS
                            for assertion in raw_assertions
                        ):
                            raise UnsupportedAssertionError
                        record = RegressionRecord.model_validate(parent)
                        spec, snapshot = validate_compilation_input(
                            record, max_scenario_bytes=self.compiler.max_scenario_bytes
                        )
                        row.test_name = spec.test_name
                        # Recheck the server-resolved fixture without inferring any new semantics.
                        attribution = self.attributions.get(record.attribution_id)
                        evidence = load_evidence(
                            attribution, self.runs.canonical_trace(attribution.run_id)
                        )
                        resolved = validate_regression(
                            spec, evidence, max_scenario_bytes=self.compiler.max_scenario_bytes
                        )
                        if resolved is None or canonical_json(
                            resolved.model_dump(mode="json")
                        ) != canonical_json(snapshot.model_dump(mode="json")):
                            raise InvalidScenarioSnapshotError
                        artifact = self.compiler.compile(record)
                        row.source_code = artifact.source_code
                        row.source_digest = artifact.source_digest
                        row.status = "completed"
                        row.completed_at = datetime.now(UTC)
                    except CompilationError as exc:
                        error = exc
                    except (
                        ValidationError,
                        RegressionValidationError,
                        RegressionInputError,
                        RunNotNormalizedError,
                    ) as exc:
                        error = CompilationInputError()
                        error.__cause__ = exc
                    except SQLAlchemyError:
                        raise
                    except Exception as exc:
                        logger.exception("Unexpected compiler failure")
                        error = CompilationInvariantError()
                        error.__cause__ = exc
                    if error is not None:
                        row.status = "failed"
                        row.source_code = None
                        row.source_digest = None
                        row.failure_code = error.code
                        row.failed_at = datetime.now(UTC)
                        logger.warning("Compilation failed", extra={"status": "failed"})
                    session.add(row)
                    session.flush()
                    result = GeneratedTestRecord.model_validate(row)
                logger.info(
                    "Compilation persisted",
                    extra={"generated_test_id": str(result.id), "status": result.status},
                )
            except SQLAlchemyError as exc:
                logger.exception("Compilation persistence failed")
                raise CompilationPersistenceError from exc
            if error is not None:
                error.generated_test_id = result.id
                raise error
            return result, True
