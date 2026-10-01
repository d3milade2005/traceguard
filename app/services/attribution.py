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

from app.core.attribution_status import AttributionStatus
from app.core.exceptions import (
    AttributionInputError,
    AttributionNotFoundError,
    AttributionValidationError,
    ContextBudgetError,
    LLMOutputError,
    LLMProviderError,
    LLMTransientError,
    ProcessingRetryError,
    RunClaimedError,
    RunNotNormalizedError,
)
from app.core.logging import bind_log_context
from app.db.models import Attribution, AttributionEvidence
from app.integrations.llm.contracts import LLMClient
from app.schemas.attributions import AttributionRecord, LLMResult, ProviderUsage, ReasoningEffort
from app.schemas.context import AnalysisContext, TruncatedTraceStep
from app.schemas.traces import CanonicalTrace
from app.services.attribution_input import (
    PROMPT_VERSION,
    build_attribution_request,
    context_manifest,
    input_digest,
    validate_context_source,
)
from app.services.attribution_validation import validate_attribution
from app.services.context import ContextManager
from app.services.runs import RunService

logger = logging.getLogger(__name__)
MAX_ATTRIBUTION_ATTEMPTS = 4


@dataclass(frozen=True)
class AttributionClaim:
    id: UUID
    run_id: UUID
    token: UUID
    request_id: str | None
    model: str
    reasoning_effort: ReasoningEffort


class AttributionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        runs: RunService,
        context_manager: ContextManager,
        llm_client: LLMClient,
        *,
        lease_seconds: int,
    ) -> None:
        self.sessions = sessions
        self.runs = runs
        self.context_manager = context_manager
        self.llm_client = llm_client
        self.lease_seconds = lease_seconds

    def process(self, attribution_id: UUID, *, final_attempt: bool = False) -> None:
        claim: AttributionClaim | None = None
        with (
            bind_log_context(attribution_id=str(attribution_id), stage="attribution"),
            ExitStack() as log_scope,
        ):
            try:
                claim = self.claim(attribution_id)
                if claim is None:
                    return
                # Keep identifiers bound through error handling as well as the happy path.
                log_scope.enter_context(
                    bind_log_context(
                        run_id=str(claim.run_id),
                        request_id=claim.request_id or "",
                        model=claim.model,
                        provider="openai",
                        prompt_version=PROMPT_VERSION,
                    )
                )
                canonical = self.runs.canonical_trace(claim.run_id)
                context = self.context_manager.build(canonical, run_id=claim.run_id)
                validate_context_source(context, canonical)
                logger.info("Analysis context generated", extra={"stage": "context"})
                request = build_attribution_request(
                    context,
                    canonical,
                    model=claim.model,
                    reasoning_effort=claim.reasoning_effort,
                )
                if not self.record_input(claim, context, input_digest(request)):
                    return
                logger.info("Attribution inference started", extra={"stage": "inference"})
                result = self.llm_client.attribute(request)
                result = LLMResult.model_validate(result.model_dump())
                if not self.record_usage(claim, result.usage):
                    return
                logger.info(
                    "Attribution response received",
                    extra={"stage": "response", **result.usage.model_dump(exclude_none=True)},
                )
                validate_attribution(result.attribution, context, canonical)
                logger.info("Attribution references validated", extra={"stage": "validation"})
                self.complete(claim, result, context, canonical)
            except (RunClaimedError, AttributionNotFoundError):
                raise
            except (OperationalError, TimeoutError, LLMTransientError) as exc:
                logger.exception("Transient attribution failure")
                code = exc.code if isinstance(exc, LLMTransientError) else "PROCESSING_UNAVAILABLE"
                self.release(attribution_id, claim, code if final_attempt else None)
                raise ProcessingRetryError from exc
            except (
                LLMProviderError,
                AttributionInputError,
                AttributionValidationError,
                ContextBudgetError,
                RunNotNormalizedError,
            ) as exc:
                logger.exception("Attribution failed", extra={"status": "failed"})
                if claim is not None and isinstance(exc, LLMOutputError) and exc.usage is not None:
                    try:
                        self.record_usage(claim, exc.usage)
                    except (OperationalError, TimeoutError) as database_error:
                        self.release(
                            attribution_id,
                            claim,
                            "PROCESSING_UNAVAILABLE" if final_attempt else None,
                        )
                        raise ProcessingRetryError from database_error
                self.release(attribution_id, claim, exc.code)
            except ValidationError:
                logger.exception("Invalid structured attribution")
                self.release(attribution_id, claim, "LLM_OUTPUT_INVALID")
            except Exception:
                logger.exception("Unexpected attribution failure")
                self.release(attribution_id, claim, "ATTRIBUTION_FAILED")
                raise

    def claim(self, attribution_id: UUID) -> AttributionClaim | None:
        with self.sessions.begin() as session:
            row = session.execute(
                select(Attribution).where(Attribution.id == attribution_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise AttributionNotFoundError
            if row.status in {AttributionStatus.COMPLETED, AttributionStatus.FAILED}:
                logger.info("Attribution already terminal", extra={"status": row.status})
                return None
            now = self._now(session)
            if row.claim_expires_at is not None and row.claim_expires_at > now:
                raise RunClaimedError(math.ceil((row.claim_expires_at - now).total_seconds()))
            if row.processing_attempts >= MAX_ATTRIBUTION_ATTEMPTS:
                self._fail(row, "ATTRIBUTION_ATTEMPTS_EXHAUSTED", now)
                return None
            if row.prompt_version != PROMPT_VERSION:
                self._fail(row, "ATTRIBUTION_PROMPT_UNSUPPORTED", now)
                return None
            row.status = AttributionStatus.PROCESSING
            row.processing_attempts += 1
            row.claim_token = uuid4()
            row.claim_expires_at = now + timedelta(seconds=self.lease_seconds)
            # Reuse the validated public schema for configuration stored at submission time.
            effort = AttributionRecord.model_validate(row).reasoning_effort
            claim = AttributionClaim(
                row.id, row.run_id, row.claim_token, row.request_id, row.model, effort
            )
        logger.info(
            "Attribution claimed",
            extra={"stage": "claim", "run_id": str(claim.run_id), "request_id": claim.request_id},
        )
        return claim

    @staticmethod
    def _now(session: Session) -> datetime:
        now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now

    def _owned(self, session: Session, claim: AttributionClaim) -> Attribution | None:
        row = session.execute(
            select(Attribution).where(Attribution.id == claim.id).with_for_update()
        ).scalar_one_or_none()
        if (
            row is None
            or row.status != AttributionStatus.PROCESSING
            or row.claim_token != claim.token
        ):
            logger.info("Stale attribution worker ignored")
            return None
        if row.claim_expires_at is None or row.claim_expires_at <= self._now(session):
            raise RunClaimedError(1)
        return row

    def record_input(self, claim: AttributionClaim, context: AnalysisContext, digest: str) -> bool:
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            row.input_sha256 = digest
            row.context_manifest = context_manifest(context)
        return True

    def record_usage(self, claim: AttributionClaim, usage: ProviderUsage) -> bool:
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            for field, value in usage.model_dump().items():
                setattr(row, field, value)
        return True

    def complete(
        self,
        claim: AttributionClaim,
        result: LLMResult,
        context: AnalysisContext,
        canonical: CanonicalTrace,
    ) -> bool:
        validate_attribution(result.attribution, context, canonical)
        truncated = {s.sequence for s in context.steps if isinstance(s, TruncatedTraceStep)}
        with self.sessions.begin() as session:
            row = self._owned(session, claim)
            if row is None:
                return False
            for field, value in result.attribution.model_dump(exclude={"evidence"}).items():
                setattr(row, field, value)
            for position, evidence in enumerate(result.attribution.evidence):
                session.add(
                    AttributionEvidence(
                        attribution_id=row.id,
                        position=position,
                        **evidence.model_dump(),
                        contains_truncated=any(
                            evidence.start_sequence <= i <= evidence.end_sequence for i in truncated
                        ),
                    )
                )
            row.status = AttributionStatus.COMPLETED
            row.completed_at = self._now(session)
            row.claim_token = None
            row.claim_expires_at = None
        logger.info("Attribution completed", extra={"stage": "persistence", "status": "completed"})
        return True

    @staticmethod
    def _fail(row: Attribution, code: str, now: datetime) -> None:
        row.status = AttributionStatus.FAILED
        row.failure_code = code
        row.failed_at = now
        row.claim_token = None
        row.claim_expires_at = None

    def release(
        self, attribution_id: UUID, claim: AttributionClaim | None, code: str | None
    ) -> None:
        try:
            with self.sessions.begin() as session:
                row = session.execute(
                    select(Attribution).where(Attribution.id == attribution_id).with_for_update()
                ).scalar_one_or_none()
                if row is None or row.status in {
                    AttributionStatus.COMPLETED,
                    AttributionStatus.FAILED,
                }:
                    return
                if claim is not None:
                    if row.claim_token != claim.token:
                        return
                elif row.status == AttributionStatus.PROCESSING:
                    return
                if code is None:
                    row.status = AttributionStatus.QUEUED
                    row.claim_token = None
                    row.claim_expires_at = None
                else:
                    self._fail(row, code, self._now(session))
        except (OperationalError, TimeoutError) as exc:
            logger.exception("Could not persist attribution failure; lease retained for recovery")
            raise ProcessingRetryError from exc
