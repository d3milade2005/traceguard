from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RegressionValidation(Base):
    __tablename__ = "regression_validations"
    __table_args__ = (
        UniqueConstraint(
            "generated_test_id", "validator_version", name="uq_regression_validations_operation"
        ),
        CheckConstraint(
            "status IN ('pending','queued','processing','completed','failed')", name="status"
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL AND claim_expires_at IS NOT "
            "NULL) OR (status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS"
            " NULL)",
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
            "(status = 'completed' AND verdict IS NOT NULL AND faulty_outcome IS NOT NULL AND"
            " corrected_outcome IS NOT NULL AND faulty_execution IS NOT NULL AND "
            "corrected_execution IS NOT NULL AND runtime_identity IS NOT NULL) OR (status <> "
            "'completed' AND verdict IS NULL AND faulty_outcome IS NULL AND corrected_outcome"
            " IS NULL AND faulty_execution IS NULL AND corrected_execution IS NULL)",
            name="result_state",
        ),
        CheckConstraint(
            "verdict IS NULL OR verdict IN "
            "('valid','not_reproduced','not_discriminating','inconclusive')",
            name="verdict",
        ),
        CheckConstraint(
            "faulty_outcome IS NULL OR faulty_outcome IN "
            "('passed','assertion_failed','execution_error','timeout','resource_limit')",
            name="faulty_outcome",
        ),
        CheckConstraint(
            "corrected_outcome IS NULL OR corrected_outcome IN "
            "('passed','assertion_failed','execution_error','timeout','resource_limit')",
            name="corrected_outcome",
        ),
        CheckConstraint(
            "verdict IS NULL OR verdict = CASE WHEN faulty_outcome NOT IN "
            "('passed','assertion_failed') OR corrected_outcome NOT IN "
            "('passed','assertion_failed') THEN 'inconclusive' WHEN faulty_outcome = 'passed'"
            " THEN 'not_reproduced' WHEN corrected_outcome = 'passed' THEN 'valid' ELSE "
            "'not_discriminating' END",
            name="verdict_state",
        ),
        CheckConstraint("processing_attempts >= 0 AND processing_attempts <= 3", name="attempts"),
        CheckConstraint("source_digest ~ '^[0-9a-f]{64}$'", name="source_digest"),
        Index("ix_regression_validations_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    generated_test_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_tests.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    validator_version: Mapped[str] = mapped_column(String(100))
    source_digest: Mapped[str] = mapped_column(String(64))
    compiler_version: Mapped[str] = mapped_column(String(100))
    harness_identifier: Mapped[str] = mapped_column(String(100))
    harness_version: Mapped[str] = mapped_column(String(100))
    image_reference: Mapped[str] = mapped_column(String(300))
    runtime_identity: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    verdict: Mapped[str | None] = mapped_column(String(24))
    faulty_outcome: Mapped[str | None] = mapped_column(String(24))
    corrected_outcome: Mapped[str | None] = mapped_column(String(24))
    faulty_execution: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    corrected_execution: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    request_id: Mapped[str | None] = mapped_column(String(128))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    claim_token: Mapped[UUID | None]
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
