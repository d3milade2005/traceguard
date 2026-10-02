from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RegressionSpecOperation(Base):
    __tablename__ = "regression_specs"
    __table_args__ = (
        UniqueConstraint("attribution_id", name="uq_regression_specs_attribution_id"),
        CheckConstraint(
            "status IN ('pending','queued','processing','completed','failed')", name="status"
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL AND claim_expires_at IS "
            "NOT NULL) OR "
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
            "(status = 'completed' AND spec IS NOT NULL AND version IS NOT NULL "
            "AND version = 'regression-spec-v1' "
            "AND title IS NOT NULL AND disposition IS NOT NULL AND "
            "((disposition = 'synthesizable' AND target_agent IS NOT NULL AND "
            "review_reason IS NULL "
            "AND scenario_snapshot IS NOT NULL) OR (disposition = 'needs_review' "
            "AND review_reason IS NOT NULL AND scenario_snapshot IS NULL))) OR "
            "(status <> 'completed' AND spec IS NULL AND disposition IS NULL AND version IS NULL "
            "AND title IS NULL AND target_agent IS NULL AND review_reason IS NULL "
            "AND scenario_snapshot IS NULL)",
            name="result_state",
        ),
        CheckConstraint("processing_attempts >= 0 AND processing_attempts <= 4", name="attempts"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="output_tokens"),
        CheckConstraint(
            "reasoning_tokens IS NULL OR reasoning_tokens >= 0", name="reasoning_tokens"
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency"),
        Index("ix_regression_specs_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    attribution_id: Mapped[UUID] = mapped_column(ForeignKey("attributions.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    disposition: Mapped[str | None] = mapped_column(String(20))
    version: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(200))
    target_agent: Mapped[str | None] = mapped_column(String(200))
    review_reason: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    scenario_snapshot: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
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
