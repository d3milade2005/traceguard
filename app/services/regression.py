import logging
import math
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ContextBudgetError,
    LLMOutputError,
    LLMProviderError,
    LLMTransientError,
    ProcessingRetryError,
    RegressionInputError,
    RegressionNotFoundError,
    RegressionValidationError,
    RunClaimedError,
    RunNotNormalizedError,
)
from app.core.logging import bind_log_context
from app.db.models import RegressionSpecOperation
from app.integrations.llm.regression import RegressionGenerator
from app.schemas.attributions import ProviderUsage, ReasoningEffort
from app.schemas.regression_specs import RegressionRecord, RegressionResult
from app.services.attribution_input import input_digest
from app.services.attributions import AttributionStore
from app.services.regression_input import (
    PROMPT_VERSION,
    build_regression_request,
    load_evidence,
    preflight,
    review_spec,
)
from app.services.regression_validation import RegressionEvidence, validate_regression
from app.services.runs import RunService

logger = logging.getLogger(__name__)
MAX_REGRESSION_ATTEMPTS = 4


@dataclass(frozen=True)
class RegressionClaim:
    id: UUID
    attribution_id: UUID
    token: UUID
    request_id: str | None
    model: str
    reasoning_effort: ReasoningEffort


class RegressionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        runs: RunService,
        attributions: AttributionStore,
        generator: RegressionGenerator,
        *,
        lease_seconds: int,
        max_input_bytes: int,
        max_scenario_bytes: int,
    ) -> None:
        self.sessions = sessions
        self.runs = runs
        self.attributions = attributions
        self.max_input_bytes = max_input_bytes
        self.max_scenario_bytes = max_scenario_bytes
        self.generator = generator
        self.lease_seconds = lease_seconds

    def process(self, regression_spec_id: UUID, *, final_attempt: bool = False) -> None:
        claim: RegressionClaim | None = None
        with (
            bind_log_context(regression_spec_id=str(regression_spec_id), stage="regression"),
            ExitStack() as log_scope,
        ):
            try:
                claim = self.claim(regression_spec_id)
                if claim is None:
                    return
                # Keep identifiers bound through error handling as well as the happy path.
                log_scope.enter_context(
                    bind_log_context(
                        attribution_id=str(claim.attribution_id),
                        request_id=claim.request_id or "",
                        model=claim.model,
                        provider="openai",
                        prompt_version=PROMPT_VERSION,
                    )
                )
                attribution = self.attributions.get(claim.attribution_id)
                canonical = self.runs.canonical_trace(attribution.run_id)
                log_scope.enter_context(bind_log_context(run_id=str(attribution.run_id)))
                evidence = load_evidence(attribution, canonical)
                reason = preflight(evidence)
                if reason is not None:
                    result = RegressionResult(spec=review_spec(attribution, reason))
                else:
                    request = build_regression_request(
                        evidence,
                        model=claim.model,
                        reasoning_effort=claim.reasoning_effort,
                        max_input_bytes=self.max_input_bytes,
                    )
                    if not self.record_input(claim, input_digest(request)):
                        return
                    logger.info("Regression generation started", extra={"stage": "inference"})
                    result = self.generator.generate(request)
                    result = RegressionResult.model_validate_json(result.model_dump_json())
                    if not self.record_usage(claim, result.usage):
                        return
                    logger.info(
                        "Regression provider result",
                        extra={"stage": "response", **result.usage.model_dump(exclude_none=True)},
                    )
                validate_regression(
                    result.spec, evidence, max_scenario_bytes=self.max_scenario_bytes
                )
                logger.info("Regression references validated", extra={"stage": "validation"})
                self.complete(claim, result, evidence)
            except (RunClaimedError, RegressionNotFoundError):
                raise
            except (OperationalError, TimeoutError, LLMTransientError) as exc:
                logger.exception("Transient regression failure")
                code = exc.code if isinstance(exc, LLMTransientError) else "PROCESSING_UNAVAILABLE"
                self.release(regression_spec_id, claim, code if final_attempt else None)
                raise ProcessingRetryError from exc
            except (
                LLMProviderError,
                RegressionInputError,
                RegressionValidationError,
                ContextBudgetError,
                RunNotNormalizedError,
            ) as exc:
                logger.exception("Regression failed", extra={"status": "failed"})
                if claim is not None and isinstance(exc, LLMOutputError) and exc.usage is not None:
                    try:
                        self.record_usage(claim, exc.usage)
                    except (OperationalError, TimeoutError) as database_error:
                        self.release(
                            regression_spec_id,
                            claim,
                            "PROCESSING_UNAVAILABLE" if final_attempt else None,
                        )
                        raise ProcessingRetryError from database_error
                self.release(regression_spec_id, claim, exc.code)
            except ValidationError:
                logger.exception("Invalid structured attribution")
                self.release(regression_spec_id, claim, "LLM_OUTPUT_INVALID")
            except Exception:
                logger.exception("Unexpected regression failure")
                self.release(regression_spec_id, claim, "REGRESSION_FAILED")
                raise

    def claim(self, regression_spec_id: UUID) -> RegressionClaim | None:
        with self.sessions.begin() as session:
            row = session.execute(
                select(RegressionSpecOperation)
                .where(RegressionSpecOperation.id == regression_spec_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise RegressionNotFoundError
            if row.status in {"completed", "failed"}:
                logger.info("Regression already terminal", extra={"status": row.status})
                return None
            now = self._now(session)
            if row.claim_expires_at is not None and row.claim_expires_at > now:
                raise RunClaimedError(math.ceil((row.claim_expires_at - now).total_seconds()))
            if row.processing_attempts >= MAX_REGRESSION_ATTEMPTS:
                self._fail(row, "REGRESSION_ATTEMPTS_EXHAUSTED", now)
                return None
            if row.prompt_version != PROMPT_VERSION:
                self._fail(row, "REGRESSION_PROMPT_UNSUPPORTED", now)
                return None
            row.status = "processing"
            row.processing_attempts += 1
            row.claim_token = uuid4()
            row.claim_expires_at = now + timedelta(seconds=self.lease_seconds)
            # Reuse the validated public schema for configuration stored at submission time.
            effort = RegressionRecord.model_validate(row).reasoning_effort
            claim = RegressionClaim(
                row.id, row.attribution_id, row.claim_token, row.request_id, row.model, effort
            )
        logger.info(
            "Regression claimed",
            extra={
                "stage": "claim",
                "attribution_id": str(claim.attribution_id),
                "request_id": claim.request_id,
            },
        )
        return claim

    @staticmethod
    def _now(session: Session) -> datetime:
        now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now

    def _owned(self, session: Session, claim: RegressionClaim) -> RegressionSpecOperation | None:
        row = session.execute(
            select(RegressionSpecOperation)
            .where(RegressionSpecOperation.id == claim.id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or row.status != "processing" or row.claim_token != claim.token:
            logger.info("Stale regression worker ignored")
            return None
        if row.claim_expires_at is None or row.claim_expires_at <= self._now(session):
            raise RunClaimedError(1)
        return row

    def record_input(self, claim: RegressionClaim, digest: str) -> bool:
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            row.input_sha256 = digest
        return True

    def record_usage(self, claim: RegressionClaim, usage: ProviderUsage) -> bool:
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            for field, value in usage.model_dump().items():
                setattr(row, field, value)
        return True

    def complete(
        self, claim: RegressionClaim, result: RegressionResult, evidence: RegressionEvidence
    ) -> bool:
        if evidence.attribution.id != claim.attribution_id:
            raise RegressionValidationError
        snapshot = validate_regression(
            result.spec, evidence, max_scenario_bytes=self.max_scenario_bytes
        )
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            row.spec = result.spec.model_dump(mode="json")
            row.scenario_snapshot = snapshot.model_dump(mode="json") if snapshot else None
            for field in ("disposition", "version", "title", "target_agent", "review_reason"):
                setattr(row, field, getattr(result.spec, field))
            row.status = "completed"
            row.completed_at = self._now(session)
            row.claim_token = None
            row.claim_expires_at = None
        logger.info(
            "Regression completed",
            extra={"stage": "persistence", "status": result.spec.disposition},
        )
        return True

    @staticmethod
    def _fail(row: RegressionSpecOperation, code: str, now: datetime) -> None:
        row.status = "failed"
        row.failure_code = code
        row.failed_at = now
        row.claim_token = None
        row.claim_expires_at = None

    def release(
        self, regression_spec_id: UUID, claim: RegressionClaim | None, code: str | None
    ) -> None:
        try:
            with self.sessions.begin() as session:
                row = session.execute(
                    select(RegressionSpecOperation)
                    .where(RegressionSpecOperation.id == regression_spec_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if row is None or row.status in {
                    "completed",
                    "failed",
                }:
                    return
                if claim is not None:
                    if row.claim_token != claim.token:
                        return
                elif row.status == "processing":
                    return
                if code is None:
                    row.status = "queued"
                    row.claim_token = None
                    row.claim_expires_at = None
                else:
                    self._fail(row, code, self._now(session))
        except (OperationalError, TimeoutError) as exc:
            logger.exception("Could not persist regression failure; lease retained for recovery")
            raise ProcessingRetryError from exc
