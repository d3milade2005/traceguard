"""Durable coordination of existing phase services; no inference or code execution here."""

import logging
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    AnalysisNotFoundError,
    ApplicationError,
    AttributionQueuePublishError,
    CompilationError,
    QueuePublishError,
    RegressionQueuePublishError,
    RunNotFoundError,
    RunNotNormalizedError,
    ValidationQueuePublishError,
)
from app.core.logging import bind_log_context
from app.db.models import Analysis, Run
from app.schemas.analyses import AnalysisRecord
from app.schemas.attributions import ReasoningEffort
from app.services.attributions import AttributionPublisher, AttributionStore
from app.services.generated_tests import GeneratedTestService
from app.services.regression_specs import RegressionPublisher, RegressionStore
from app.services.validations import ValidationPublisher, ValidationService

logger = logging.getLogger(__name__)


class AnalysisPublisher(Protocol):
    def publish(self, run_id: UUID, request_id: str | None) -> None: ...


class AnalysisService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        attributions: AttributionStore,
        regressions: RegressionStore,
        compiler: GeneratedTestService,
        validations: ValidationService,
        attribution_publisher: AttributionPublisher,
        regression_publisher: RegressionPublisher,
        validation_publisher: ValidationPublisher,
    ) -> None:
        self.sessions = sessions
        self.attributions = attributions
        self.regressions = regressions
        self.compiler = compiler
        self.validations = validations
        self.attribution_publisher = attribution_publisher
        self.regression_publisher = regression_publisher
        self.validation_publisher = validation_publisher

    @staticmethod
    def _now(session: Session) -> datetime:
        now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now

    def get(self, run_id: UUID) -> AnalysisRecord:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Analysis).where(Analysis.run_id == run_id).with_for_update()
            )
            if row is None:
                raise AnalysisNotFoundError
            # A lost task must never make a timed-out pipeline appear active forever.
            self._expire(row, self._now(session))
            session.flush()
            return AnalysisRecord.model_validate(row)

    def submit(
        self,
        run_id: UUID,
        request_id: str,
        publisher: AnalysisPublisher,
        *,
        model: str,
        reasoning_effort: ReasoningEffort,
    ) -> tuple[AnalysisRecord, bool]:
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None:
                raise RunNotFoundError
            if run.status != "normalized" or run.task is None:
                raise RunNotNormalizedError
            row = session.scalar(select(Analysis).where(Analysis.run_id == run_id))
            created = row is None
            if row is None:
                row = Analysis(
                    run_id=run_id,
                    request_id=request_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    deadline_at=self._now(session) + timedelta(hours=1),
                )
                session.add(row)
        record = self.get(run_id)
        if record.status == "active":
            # Repeated POST also recovers a crash between commit and publication.
            # Repeated delivery is serialized by the analysis row lock.
            try:
                publisher.publish(run_id, request_id)
            except QueuePublishError:
                logger.exception("Analysis publication failed", extra={"run_id": str(run_id)})
                self.fail(run_id, "QUEUE_PUBLICATION_FAILED")
                raise
        return self.get(run_id), created

    def fail(self, run_id: UUID, code: str) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Analysis).where(Analysis.run_id == run_id).with_for_update()
            )
            if row is not None and row.status == "active":
                self._stop(row, "failed", code, self._now(session))

    @staticmethod
    def _stop(row: Analysis, status: str, reason: str, now: datetime) -> None:
        row.status, row.stop_reason, row.finished_at = status, reason, now
        logger.info(
            "Analysis stopped", extra={"status": status, "stage": row.stage, "stop_reason": reason}
        )

    def _expire(self, row: Analysis, now: datetime) -> None:
        if row.status == "active" and now >= row.deadline_at:
            self._stop(row, "failed", "ANALYSIS_DEADLINE_EXCEEDED", now)

    def advance(self, run_id: UUID) -> bool:
        """Advance one stage under a row lock; return whether another poll is needed."""
        with (
            bind_log_context(run_id=str(run_id), component="analysis"),
            self.sessions.begin() as session,
        ):
            row = session.scalar(
                select(Analysis).where(Analysis.run_id == run_id).with_for_update()
            )
            if row is None:
                raise AnalysisNotFoundError
            now = self._now(session)
            self._expire(row, now)
            if row.status != "active":
                return False
            config = AnalysisRecord.model_validate(row)
            try:
                self._advance(row, config, now)
            except CompilationError as exc:
                row.generated_test_id = exc.generated_test_id
                self._stop(row, "failed", exc.code, now)
            except ApplicationError as exc:
                logger.exception("Analysis stage failed")
                if isinstance(exc, AttributionQueuePublishError):
                    row.attribution_id = exc.attribution_id
                elif isinstance(exc, RegressionQueuePublishError):
                    row.regression_spec_id = exc.regression_spec_id
                elif isinstance(exc, ValidationQueuePublishError):
                    row.validation_id = exc.validation_id
                self._stop(row, "failed", exc.code, now)
            session.flush()
            return row.status == "active"

    def _advance(self, row: Analysis, config: AnalysisRecord, now: datetime) -> None:
        if row.stage == "attribution":
            accepted, _ = self.attributions.submit(
                row.run_id,
                row.request_id,
                self.attribution_publisher,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
            )
            row.attribution_id = accepted.id
            result = self.attributions.get(accepted.id)
            if result.status == "failed":
                self._stop(row, "failed", result.failure_code or "ATTRIBUTION_FAILED", now)
            elif result.status == "completed":
                if result.decision == "abstain":
                    self._stop(row, "review_required", "attribution_abstained", now)
                else:
                    row.stage = "regression_spec"
            elif result.status == "pending":
                self.attribution_publisher.publish(result.id, row.request_id)
        elif row.stage == "regression_spec":
            assert row.attribution_id is not None
            accepted_spec, _ = self.regressions.submit(
                row.attribution_id,
                row.request_id,
                self.regression_publisher,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
            )
            row.regression_spec_id = accepted_spec.id
            spec = self.regressions.get(accepted_spec.id)
            if spec.status == "failed":
                self._stop(row, "failed", spec.failure_code or "REGRESSION_SPEC_FAILED", now)
            elif spec.status == "completed":
                if spec.disposition == "needs_review":
                    self._stop(row, "review_required", "regression_needs_review", now)
                else:
                    row.stage = "compilation"
            elif spec.status == "pending":
                self.regression_publisher.publish(spec.id, row.request_id)
        elif row.stage == "compilation":
            assert row.regression_spec_id is not None
            artifact, _ = self.compiler.compile(row.regression_spec_id, row.request_id)
            row.generated_test_id = artifact.id
            if artifact.status == "failed":
                self._stop(row, "failed", artifact.failure_code or "COMPILATION_FAILED", now)
            else:
                row.stage = "validation"
        else:
            assert row.generated_test_id is not None
            accepted_validation, _ = self.validations.submit(
                row.generated_test_id, row.request_id, self.validation_publisher
            )
            row.validation_id = accepted_validation.id
            validation = self.validations.get(accepted_validation.id)
            if validation.status == "failed":
                self._stop(row, "failed", validation.failure_code or "VALIDATION_FAILED", now)
            elif validation.status == "completed":
                assert validation.verdict is not None
                self._stop(
                    row,
                    "completed" if validation.verdict == "valid" else "review_required",
                    validation.verdict,
                    now,
                )
            elif validation.status == "pending":
                self.validation_publisher.publish(validation.id, row.request_id)
