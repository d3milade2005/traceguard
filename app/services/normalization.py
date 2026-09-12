import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ProcessingRetryError,
    RunClaimedError,
    RunNotFoundError,
    TraceAdapterError,
    UnsupportedTraceSourceError,
)
from app.core.logging import bind_log_context
from app.core.run_status import RunStatus
from app.db.models import Run, TraceStep
from app.integrations.traces.contracts import TraceAdapter, decode_record
from app.schemas.traces import CanonicalTrace

logger = logging.getLogger(__name__)
MAX_PROCESSING_ATTEMPTS = 4


@dataclass(frozen=True)
class RunClaim:
    run_id: UUID
    token: UUID
    source: str
    original_trace: bytes
    request_id: str | None


class NormalizationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        adapters: Mapping[str, TraceAdapter],
        *,
        lease_seconds: int,
        max_trace_bytes: int,
    ) -> None:
        self.sessions = sessions
        self.adapters = adapters
        self.lease_seconds = lease_seconds
        self.max_trace_bytes = max_trace_bytes

    def process(self, run_id: UUID, *, final_attempt: bool = False) -> None:
        """Own the workflow boundary, including rollback and durable failure recording."""
        claim: RunClaim | None = None
        with bind_log_context(run_id=str(run_id), stage="normalization"):
            try:
                claim = self.claim(run_id)
                if claim is None:
                    return
                with bind_log_context(source=claim.source, request_id=claim.request_id or ""):
                    logger.info("Normalization started")
                    adapter = self.adapters.get(claim.source)
                    if adapter is None:
                        raise UnsupportedTraceSourceError
                    if len(claim.original_trace) > self.max_trace_bytes:
                        raise TraceAdapterError("Stored trace exceeds processing limit.")
                    record = decode_record(claim.original_trace)
                    adapter.validate_submission(record)
                    trace = adapter.adapt(record)
                    # Revalidate even if an injected adapter bypassed Pydantic construction.
                    trace = CanonicalTrace.model_validate(trace.model_dump())
                    self.complete(claim, trace)
            except (RunClaimedError, RunNotFoundError):
                raise
            except (OperationalError, TimeoutError) as exc:
                logger.exception("Transient database failure")
                self._release_after_error(
                    run_id, claim, "PROCESSING_UNAVAILABLE" if final_attempt else None
                )
                raise ProcessingRetryError from exc
            except (TraceAdapterError, UnsupportedTraceSourceError, ValidationError) as exc:
                logger.exception("Invalid trace", extra={"status": "failed"})
                code = (
                    exc.code
                    if isinstance(exc, (TraceAdapterError, UnsupportedTraceSourceError))
                    else "INVALID_CANONICAL_TRACE"
                )
                self._release_after_error(run_id, claim, code)
            except Exception:
                # One application workflow boundary; detailed errors remain internal.
                logger.exception("Normalization failed", extra={"status": "failed"})
                self._release_after_error(run_id, claim, "NORMALIZATION_FAILED")
                raise

    def claim(self, run_id: UUID) -> RunClaim | None:
        """Commit a lease; tokens fence workers that resume after losing ownership."""
        with self.sessions.begin() as session:
            run = session.execute(
                select(Run).where(Run.id == run_id).with_for_update()
            ).scalar_one_or_none()
            if run is None:
                raise RunNotFoundError
            if run.status in {RunStatus.NORMALIZED, RunStatus.FAILED}:
                logger.info("Run already terminal", extra={"status": run.status})
                return None
            now = session.scalar(select(func.clock_timestamp()))
            assert isinstance(now, datetime)
            if run.claim_expires_at is not None and run.claim_expires_at > now:
                raise RunClaimedError(math.ceil((run.claim_expires_at - now).total_seconds()))
            if run.processing_attempts >= MAX_PROCESSING_ATTEMPTS:
                self._fail(run, "PROCESSING_ATTEMPTS_EXHAUSTED", now)
                logger.error("Processing attempts exhausted", extra={"status": "failed"})
                return None
            run.status = RunStatus.PROCESSING
            run.processing_attempts += 1
            run.claim_token = uuid4()
            run.claim_expires_at = now + timedelta(seconds=self.lease_seconds)
            claim = RunClaim(
                run.id, run.claim_token, run.source, run.original_trace, run.request_id
            )
        logger.info("Run claimed", extra={"run_id": str(run_id), "status": "processing"})
        return claim

    def complete(self, claim: RunClaim, trace: CanonicalTrace) -> bool:
        """Commit every derived step and state together, or roll back all derived writes."""
        with self.sessions.begin() as session:
            run = session.execute(
                select(Run).where(Run.id == claim.run_id).with_for_update()
            ).scalar_one()
            now = session.scalar(select(func.clock_timestamp()))
            assert isinstance(now, datetime)
            if run.claim_token != claim.token or run.claim_expires_at is None:
                logger.info("Stale worker result ignored")
                return False
            if run.claim_expires_at <= now:
                raise RunClaimedError(1)
            session.add_all(TraceStep(run_id=run.id, **step.model_dump()) for step in trace.steps)
            session.flush()
            logger.info("Canonical steps persisted")
            run.task = trace.task
            run.status = RunStatus.NORMALIZED
            run.claim_token = None
            run.claim_expires_at = None
        logger.info("Normalization completed", extra={"status": "normalized"})
        return True

    @staticmethod
    def _fail(run: Run, code: str, now: datetime) -> None:
        run.status = RunStatus.FAILED
        run.failure_code = code
        run.failed_at = now
        run.claim_token = None
        run.claim_expires_at = None

    def _release_after_error(
        self, run_id: UUID, claim: RunClaim | None, failure_code: str | None
    ) -> None:
        try:
            with self.sessions.begin() as session:
                run = session.execute(
                    select(Run).where(Run.id == run_id).with_for_update()
                ).scalar_one_or_none()
                if run is None or run.status in {RunStatus.NORMALIZED, RunStatus.FAILED}:
                    return
                if claim is not None:
                    if run.claim_token != claim.token:
                        return
                elif run.status == RunStatus.PROCESSING:
                    return  # Never release another worker's lease.
                if failure_code is None:
                    run.status = RunStatus.QUEUED
                    run.claim_token = None
                    run.claim_expires_at = None
                else:
                    now = session.scalar(select(func.clock_timestamp()))
                    assert isinstance(now, datetime)
                    self._fail(run, failure_code, now)
        except (OperationalError, TimeoutError) as exc:
            # PostgreSQL is unreachable: retain the lease for recovery, never pretend
            # a failure record was persisted. The task boundary retries this condition.
            logger.exception("Could not persist processing outcome")
            raise ProcessingRetryError from exc
