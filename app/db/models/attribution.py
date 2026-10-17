from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.attribution_status import AttributionStatus
from app.db.base import Base


class Attribution(Base):
    __tablename__ = "attributions"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_attributions_run_id"),
        ForeignKeyConstraint(
            ["run_id", "decisive_sequence"],
            ["trace_steps.run_id", "trace_steps.sequence"],
            name="fk_attributions_decisive_step",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'queued', 'processing', 'completed', 'failed')", name="status"
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name="claim_state",
        ),
        CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL AND failed_at IS NULL)",
            name="failure_state",
        ),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)", name="completion_state"
        ),
        CheckConstraint(
            "(status = 'completed' AND decision IS NOT NULL AND explanation IS NOT NULL "
            "AND confidence IS NOT NULL AND confidence IN ('low','medium','high') AND "
            "((decision = 'attributed' AND responsible_agent IS NOT NULL "
            "AND decisive_sequence IS NOT NULL AND abstention_reason IS NULL) OR "
            "(decision = 'abstain' AND responsible_agent IS NULL AND decisive_sequence IS NULL "
            "AND failure_category IS NULL AND abstention_reason IS NOT NULL))) OR "
            "(status <> 'completed' AND decision IS NULL AND responsible_agent IS NULL "
            "AND decisive_sequence IS NULL AND failure_category IS NULL AND explanation IS NULL "
            "AND confidence IS NULL AND abstention_reason IS NULL)",
            name="decision_state",
        ),
        CheckConstraint("processing_attempts >= 0 AND processing_attempts <= 4", name="attempts"),
        CheckConstraint("decisive_sequence IS NULL OR decisive_sequence >= 0", name="sequence"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="output_tokens"),
        CheckConstraint(
            "reasoning_tokens IS NULL OR reasoning_tokens >= 0", name="reasoning_tokens"
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency"),
        Index("ix_attributions_status_created_at", "status", "created_at"),
        Index("ix_attributions_responsible_agent", "responsible_agent"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(
        String(16), default=AttributionStatus.PENDING.value, server_default="pending"
    )
    decision: Mapped[str | None] = mapped_column(String(16))
    responsible_agent: Mapped[str | None] = mapped_column(String(200))
    decisive_sequence: Mapped[int | None]
    failure_category: Mapped[str | None] = mapped_column(String(100))
    explanation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(String(16))
    abstention_reason: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    reasoning_effort: Mapped[str] = mapped_column(String(16))
    prompt_version: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str | None] = mapped_column(String(64))
    response_id: Mapped[str | None] = mapped_column(String(200))
    response_model: Mapped[str | None] = mapped_column(String(200))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    reasoning_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    context_manifest: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB, nullable=True)
    output_diagnostics: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    claim_token: Mapped[UUID | None]
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_attempts: Mapped[int] = mapped_column(default=0, server_default="0")


class AttributionEvidence(Base):
    __tablename__ = "attribution_evidence"
    __table_args__ = (
        UniqueConstraint("attribution_id", "position", name="uq_attribution_evidence_position"),
        UniqueConstraint(
            "attribution_id", "start_sequence", "end_sequence", name="uq_attribution_evidence_range"
        ),
        CheckConstraint("position >= 0", name="position"),
        CheckConstraint("start_sequence >= 0 AND end_sequence >= start_sequence", name="range"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    attribution_id: Mapped[UUID] = mapped_column(ForeignKey("attributions.id", ondelete="CASCADE"))
    position: Mapped[int]
    start_sequence: Mapped[int]
    end_sequence: Mapped[int]
    reason: Mapped[str] = mapped_column(Text)
    contains_truncated: Mapped[bool]
