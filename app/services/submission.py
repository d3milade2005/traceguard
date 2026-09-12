import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from app.core.exceptions import QueuePublishError, UnsupportedTraceSourceError
from app.core.run_status import RunStatus
from app.db.models import Run
from app.integrations.traces.contracts import TraceAdapter, decode_record
from app.schemas.runs import RunAccepted, RunCreate
from app.services.runs import RunService

logger = logging.getLogger(__name__)


class RunPublisher(Protocol):
    def publish(self, run_id: UUID, request_id: str | None) -> None: ...


class SubmissionService:
    def __init__(
        self, runs: RunService, publisher: RunPublisher, adapters: Mapping[str, TraceAdapter]
    ) -> None:
        self.runs = runs
        self.publisher = publisher
        self.adapters = adapters

    def submit(self, source: str, raw: bytes, request_id: str) -> RunAccepted:
        adapter = self.adapters.get(source)
        if adapter is None:
            raise UnsupportedTraceSourceError
        self.runs.check_size(raw)
        adapter.validate_submission(decode_record(raw))
        record = self.runs.create(
            RunCreate(
                source=source,
                original_trace=raw,
                failure_signal="developer_report",
                request_id=request_id,
            )
        )
        publication_error: QueuePublishError | None = None
        # The original is already committed. Serialize this handoff with worker claims,
        # so a fast worker cannot have its processing/normalized state overwritten.
        with self.runs.sessions.begin() as session:
            run = session.execute(
                select(Run).where(Run.id == record.id).with_for_update()
            ).scalar_one()
            try:
                self.publisher.publish(run.id, request_id)
            except QueuePublishError as exc:
                publication_error = exc
                run.status = RunStatus.FAILED
                run.failure_code = exc.code
                run.failed_at = datetime.now(UTC)
                logger.exception(
                    "Task publication failed",
                    extra={"run_id": str(run.id), "source": source, "stage": "publication"},
                )
            else:
                run.status = RunStatus.QUEUED
                logger.info(
                    "Task published",
                    extra={"run_id": str(run.id), "source": source, "stage": "publication"},
                )
        if publication_error is not None:
            raise QueuePublishError(run_id=record.id) from publication_error
        return RunAccepted(id=record.id, status=RunStatus.QUEUED)
