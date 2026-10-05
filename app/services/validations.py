import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    GeneratedTestNotFoundError,
    ProcessingRetryError,
    QueuePublishError,
    RunClaimedError,
    SandboxPolicyError,
    SandboxUnavailableError,
    ValidationArtifactError,
    ValidationNotFoundError,
    ValidationPersistenceError,
    ValidationQueuePublishError,
)
from app.core.logging import bind_log_context
from app.db.models import GeneratedTest, RegressionValidation
from app.integrations.execution.contracts import ValidationRunner
from app.schemas.validations import (
    VALIDATOR_VERSION,
    ExecutionResult,
    RuntimeIdentity,
    ValidationAccepted,
    ValidationRecord,
    classify,
)
from app.services.validation_artifact import verify_artifact

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class ValidationPublisher(Protocol):
    def publish(self, validation_id: UUID, request_id: str | None) -> None: ...


@dataclass(frozen=True)
class ValidationClaim:
    id: UUID
    generated_test_id: UUID
    token: UUID
    request_id: str | None


class ValidationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        runner: ValidationRunner | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.runner = runner
        self.lease_seconds = 2 * settings.validation_timeout_seconds + 180

    def get(self, validation_id: UUID) -> ValidationRecord:
        try:
            with self.sessions() as session:
                row = session.get(RegressionValidation, validation_id)
                if row is None:
                    raise ValidationNotFoundError
                return ValidationRecord.model_validate(row)
        except SQLAlchemyError as exc:
            raise ValidationPersistenceError from exc

    def submit(
        self, generated_test_id: UUID, request_id: str, publisher: ValidationPublisher
    ) -> tuple[ValidationAccepted, bool]:
        with bind_log_context(
            generated_test_id=str(generated_test_id),
            request_id=request_id,
            stage="validation_submission",
        ):
            try:
                with self.sessions.begin() as session:
                    parent = session.execute(
                        select(GeneratedTest)
                        .where(GeneratedTest.id == generated_test_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    artifact, _ = verify_artifact(session, parent, self.settings)
                    existing = session.scalar(
                        select(RegressionValidation).where(
                            RegressionValidation.generated_test_id == generated_test_id,
                            RegressionValidation.validator_version == VALIDATOR_VERSION,
                        )
                    )
                    if existing is not None:
                        return ValidationAccepted.model_validate(existing), False
                    row = RegressionValidation(
                        generated_test_id=generated_test_id,
                        validator_version=VALIDATOR_VERSION,
                        source_digest=artifact.source_digest,
                        compiler_version=artifact.compiler_version,
                        harness_identifier=artifact.harness_identifier,
                        harness_version=artifact.harness_version,
                        image_reference=self.settings.validation_image,
                        request_id=request_id,
                    )
                    session.add(row)
                    session.flush()
                    validation_id = row.id
                logger.info("Validation persisted", extra={"validation_id": str(validation_id)})
                error: QueuePublishError | None = None
                with self.sessions.begin() as session:
                    row = session.execute(
                        select(RegressionValidation)
                        .where(RegressionValidation.id == validation_id)
                        .with_for_update()
                    ).scalar_one()
                    try:
                        publisher.publish(validation_id, request_id)
                    except QueuePublishError as exc:
                        error = exc
                        self._fail(row, exc.code, self._now(session))
                        logger.exception("Validation publication failed")
                    else:
                        row.status = "queued"
                if error is not None:
                    raise ValidationQueuePublishError(validation_id=validation_id) from error
                return ValidationAccepted(
                    id=validation_id, generated_test_id=generated_test_id, status="queued"
                ), True
            except SQLAlchemyError as exc:
                logger.exception("Validation submission persistence failed")
                raise ValidationPersistenceError from exc

    @staticmethod
    def _now(session: Session) -> datetime:
        now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now

    @staticmethod
    def _fail(row: RegressionValidation, code: str, now: datetime) -> None:
        row.status = "failed"
        row.failure_code = code
        row.failed_at = now
        row.claim_token = None
        row.claim_expires_at = None

    def claim(self, validation_id: UUID) -> ValidationClaim | None:
        with self.sessions.begin() as session:
            row = session.execute(
                select(RegressionValidation)
                .where(RegressionValidation.id == validation_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise ValidationNotFoundError
            if row.status in {"completed", "failed"}:
                return None
            now = self._now(session)
            if row.claim_expires_at is not None and row.claim_expires_at > now:
                raise RunClaimedError(math.ceil((row.claim_expires_at - now).total_seconds()))
            if row.processing_attempts >= MAX_ATTEMPTS:
                self._fail(row, "VALIDATION_ATTEMPTS_EXHAUSTED", now)
                return None
            if (
                row.validator_version != VALIDATOR_VERSION
                or row.image_reference != self.settings.validation_image
            ):
                self._fail(row, "VALIDATION_CONFIGURATION_CHANGED", now)
                return None
            row.status = "processing"
            row.processing_attempts += 1
            row.claim_token = uuid4()
            row.claim_expires_at = now + timedelta(seconds=self.lease_seconds)
            return ValidationClaim(row.id, row.generated_test_id, row.claim_token, row.request_id)

    def _owned(self, session: Session, claim: ValidationClaim) -> RegressionValidation | None:
        row = session.execute(
            select(RegressionValidation)
            .where(RegressionValidation.id == claim.id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or row.status != "processing" or row.claim_token != claim.token:
            return None
        if row.claim_expires_at is None or row.claim_expires_at <= self._now(session):
            raise RunClaimedError(1)
        return row

    def record_runtime(self, claim: ValidationClaim, runtime: RuntimeIdentity) -> bool:
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            row.runtime_identity = runtime.model_dump(mode="json")
        return True

    def complete(
        self, claim: ValidationClaim, faulty: ExecutionResult, corrected: ExecutionResult
    ) -> bool:
        verdict = classify(faulty, corrected)
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            row.faulty_execution = faulty.model_dump(mode="json")
            row.corrected_execution = corrected.model_dump(mode="json")
            row.faulty_outcome, row.corrected_outcome = faulty.outcome, corrected.outcome
            row.verdict = verdict
            row.status = "completed"
            row.completed_at = self._now(session)
            row.claim_token = None
            row.claim_expires_at = None
            session.flush()
            ValidationRecord.model_validate(row)
        logger.info("Validation completed", extra={"verdict": verdict, "status": "completed"})
        return True

    def release(self, validation_id: UUID, claim: ValidationClaim | None, code: str | None) -> None:
        try:
            with self.sessions.begin() as session:
                row = session.execute(
                    select(RegressionValidation)
                    .where(RegressionValidation.id == validation_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if row is None or row.status in {"completed", "failed"}:
                    return
                if claim is None or row.claim_token != claim.token:
                    return
                if row.claim_expires_at is None or row.claim_expires_at <= self._now(session):
                    return
                if code is None:
                    row.status = "queued"
                    row.claim_token = None
                    row.claim_expires_at = None
                else:
                    self._fail(row, code, self._now(session))
        except (OperationalError, TimeoutError) as exc:
            logger.exception("Validation state unavailable; lease retained")
            raise ProcessingRetryError from exc

    def process(self, validation_id: UUID, *, final_attempt: bool = False) -> None:
        claim: ValidationClaim | None = None
        with bind_log_context(
            validation_id=str(validation_id),
            stage="validation",
            validator_version=VALIDATOR_VERSION,
        ):
            try:
                claim = self.claim(validation_id)
                if claim is None:
                    return
                if self.runner is None:
                    raise SandboxPolicyError
                with bind_log_context(
                    generated_test_id=str(claim.generated_test_id),
                    request_id=claim.request_id or "",
                ):
                    with self.sessions() as session:
                        artifact, bundle = verify_artifact(
                            session,
                            session.get(GeneratedTest, claim.generated_test_id),
                            self.settings,
                        )
                        operation = session.get(RegressionValidation, validation_id)
                        if operation is None or any(
                            getattr(operation, field) != getattr(artifact, field)
                            for field in (
                                "source_digest",
                                "compiler_version",
                                "harness_identifier",
                                "harness_version",
                            )
                        ):
                            raise ValidationArtifactError
                    self.runner.cleanup_expired()
                    self.runner.cleanup_previous(validation_id, claim.token)
                    runtime = self.runner.prepare(self.settings.validation_image)
                    if not self.record_runtime(claim, runtime):
                        return
                    faulty = self.runner.execute(
                        bundle, runtime, "faulty", validation_id, claim.token
                    )
                    # Check ownership before starting another sandbox after a lease/database gap.
                    with self.sessions.begin() as session:
                        if self._owned(session, claim) is None:
                            return
                    corrected = self.runner.execute(
                        bundle, runtime, "corrected", validation_id, claim.token
                    )
                    self.complete(claim, faulty, corrected)
            except (RunClaimedError, ValidationNotFoundError):
                raise
            except (OperationalError, TimeoutError, SandboxUnavailableError) as exc:
                logger.exception("Transient validation infrastructure failure")
                self.release(
                    validation_id,
                    claim,
                    "VALIDATION_INFRASTRUCTURE_FAILED" if final_attempt else None,
                )
                raise ProcessingRetryError from exc
            except (ValidationArtifactError, GeneratedTestNotFoundError, SandboxPolicyError) as exc:
                logger.exception("Validation rejected", extra={"status": "failed"})
                self.release(validation_id, claim, exc.code)
            except Exception:
                logger.exception("Unexpected validation failure")
                self.release(validation_id, claim, "VALIDATION_FAILED")
                raise
