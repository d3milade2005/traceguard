import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import InvalidTraceError, RunNotFoundError
from app.db.models import Run, RunStatus
from app.schemas.runs import RunCreate, RunRecord

logger = logging.getLogger(__name__)


class RunService:
    def __init__(self, sessions: sessionmaker[Session], max_trace_bytes: int) -> None:
        self.sessions = sessions
        self.max_trace_bytes = max_trace_bytes

    def create(self, data: RunCreate) -> RunRecord:
        """Commit the unchanged source before returning; no work is queued in this phase."""
        if len(data.original_trace) > self.max_trace_bytes:
            raise InvalidTraceError
        with self.sessions.begin() as session:
            run = Run(**data.model_dump())
            session.add(run)
            session.flush()
            result = RunRecord.model_validate(run)
        logger.info(
            "Run persisted",
            extra={"run_id": str(result.id), "request_id": result.request_id, "status": "pending"},
        )
        return result

    def get(self, run_id: UUID) -> RunRecord:
        with self.sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunNotFoundError
            return RunRecord.model_validate(run)

    def record_failure(self, run_id: UUID, code: str) -> bool:
        """Persist a safe failure code in a fresh transaction; repeated calls are harmless."""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code):
            raise ValueError("Failure codes must be uppercase identifiers, not exception text")
        with self.sessions.begin() as session:
            changed = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == RunStatus.PENDING)
                .values(status=RunStatus.FAILED, failure_code=code, failed_at=datetime.now(UTC))
                .returning(Run.id)
            ).scalar_one_or_none()
            if changed is None and session.get(Run, run_id) is None:
                raise RunNotFoundError
        if changed is not None:
            logger.error("Run failed", extra={"run_id": str(run_id), "status": "failed"})
        return changed is not None
