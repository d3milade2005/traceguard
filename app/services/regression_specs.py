import logging
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    AttributionNotCompletedError,
    AttributionNotFoundError,
    QueuePublishError,
    RegressionNotFoundError,
    RegressionQueuePublishError,
)
from app.core.logging import bind_log_context
from app.db.models import Attribution, RegressionSpecOperation
from app.schemas.attributions import ReasoningEffort
from app.schemas.regression_specs import RegressionAccepted, RegressionRecord
from app.services.regression_input import PROMPT_VERSION

logger = logging.getLogger(__name__)


class RegressionPublisher(Protocol):
    def publish(self, regression_spec_id: UUID, request_id: str | None) -> None: ...


class RegressionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def get(self, regression_spec_id: UUID) -> RegressionRecord:
        with self.sessions() as session:
            row = session.get(RegressionSpecOperation, regression_spec_id)
            if row is None:
                raise RegressionNotFoundError
            return RegressionRecord.model_validate(row)

    def submit(
        self,
        attribution_id: UUID,
        request_id: str,
        publisher: RegressionPublisher,
        *,
        model: str,
        reasoning_effort: ReasoningEffort,
    ) -> tuple[RegressionAccepted, bool]:
        with bind_log_context(attribution_id=str(attribution_id), request_id=request_id):
            logger.info("Regression specification requested", extra={"stage": "submission"})
            with self.sessions.begin() as session:
                attribution = session.execute(
                    select(Attribution).where(Attribution.id == attribution_id).with_for_update()
                ).scalar_one_or_none()
                if attribution is None:
                    raise AttributionNotFoundError
                if attribution.status != "completed":
                    raise AttributionNotCompletedError
                existing = session.scalar(
                    select(RegressionSpecOperation).where(
                        RegressionSpecOperation.attribution_id == attribution_id
                    )
                )
                if existing is not None:
                    return RegressionAccepted.model_validate(existing), False
                row = RegressionSpecOperation(
                    attribution_id=attribution_id,
                    provider="openai",
                    model=model,
                    reasoning_effort=reasoning_effort,
                    prompt_version=PROMPT_VERSION,
                    request_id=request_id,
                )
                session.add(row)
                session.flush()
                spec_id = row.id
            logger.info(
                "Regression operation persisted", extra={"regression_spec_id": str(spec_id)}
            )
            error: QueuePublishError | None = None
            with self.sessions.begin() as session:
                row = session.execute(
                    select(RegressionSpecOperation)
                    .where(RegressionSpecOperation.id == spec_id)
                    .with_for_update()
                ).scalar_one()
                try:
                    publisher.publish(spec_id, request_id)
                except QueuePublishError as exc:
                    error = exc
                    row.status = "failed"
                    row.failure_code = exc.code
                    row.failed_at = datetime.now(UTC)
                    logger.exception("Regression publication failed")
                else:
                    row.status = "queued"
                    logger.info(
                        "Regression task published", extra={"regression_spec_id": str(spec_id)}
                    )
            if error is not None:
                raise RegressionQueuePublishError(regression_spec_id=spec_id) from error
            return RegressionAccepted(
                id=spec_id, attribution_id=attribution_id, status="queued"
            ), True
