import logging
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.attribution_status import AttributionStatus
from app.core.exceptions import (
    AttributionNotFoundError,
    AttributionQueuePublishError,
    QueuePublishError,
    RunNotFoundError,
    RunNotNormalizedError,
)
from app.core.logging import bind_log_context
from app.core.run_status import RunStatus
from app.db.models import Attribution, AttributionEvidence, Run
from app.schemas.attributions import (
    AttributionAccepted,
    AttributionRecord,
    PersistedEvidence,
    ReasoningEffort,
)
from app.services.attribution_input import PROMPT_VERSION

logger = logging.getLogger(__name__)


class AttributionPublisher(Protocol):
    def publish(self, attribution_id: UUID, request_id: str | None) -> None: ...


class AttributionStore:
    """Own explicit submission and read boundaries; never call a provider in the API."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def get(self, attribution_id: UUID) -> AttributionRecord:
        with self.sessions() as session:
            attribution = session.get(Attribution, attribution_id)
            if attribution is None:
                raise AttributionNotFoundError
            evidence = session.scalars(
                select(AttributionEvidence)
                .where(AttributionEvidence.attribution_id == attribution_id)
                .order_by(AttributionEvidence.position)
            ).all()
            return AttributionRecord.model_validate(attribution).model_copy(
                update={
                    "evidence": tuple(PersistedEvidence.model_validate(row) for row in evidence)
                }
            )

    def submit(
        self,
        run_id: UUID,
        request_id: str,
        publisher: AttributionPublisher,
        *,
        model: str,
        reasoning_effort: ReasoningEffort,
    ) -> tuple[AttributionAccepted, bool]:
        with bind_log_context(
            run_id=str(run_id), request_id=request_id, stage="attribution_submission"
        ):
            logger.info("Attribution requested")
            # Serialize creation on Run; the unique run_id constraint also protects direct writes.
            with self.sessions.begin() as session:
                run = session.execute(
                    select(Run).where(Run.id == run_id).with_for_update()
                ).scalar_one_or_none()
                if run is None:
                    raise RunNotFoundError
                if run.status != RunStatus.NORMALIZED or run.task is None:
                    raise RunNotNormalizedError
                existing = session.scalar(select(Attribution).where(Attribution.run_id == run_id))
                if existing is not None:
                    return AttributionAccepted.model_validate(existing, from_attributes=True), False
                attribution = Attribution(
                    run_id=run_id,
                    provider="openai",
                    model=model,
                    reasoning_effort=reasoning_effort,
                    prompt_version=PROMPT_VERSION,
                    request_id=request_id,
                )
                session.add(attribution)
                session.flush()
                attribution_id = attribution.id
            logger.info("Attribution persisted", extra={"attribution_id": str(attribution_id)})
            publication_error: QueuePublishError | None = None
            # Persistent state exists before publication. Lock against fast worker claims.
            with self.sessions.begin() as session:
                attribution = session.execute(
                    select(Attribution).where(Attribution.id == attribution_id).with_for_update()
                ).scalar_one()
                try:
                    publisher.publish(attribution_id, request_id)
                except QueuePublishError as exc:
                    publication_error = exc
                    attribution.status = AttributionStatus.FAILED
                    attribution.failure_code = exc.code
                    attribution.failed_at = datetime.now(UTC)
                    logger.exception(
                        "Attribution publication failed",
                        extra={"attribution_id": str(attribution_id)},
                    )
                else:
                    attribution.status = AttributionStatus.QUEUED
                    logger.info(
                        "Attribution task published", extra={"attribution_id": str(attribution_id)}
                    )
            if publication_error is not None:
                raise AttributionQueuePublishError(
                    attribution_id=attribution_id, run_id=run_id
                ) from publication_error
            return AttributionAccepted(
                id=attribution_id, run_id=run_id, status=AttributionStatus.QUEUED
            ), True
