from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.run_status import RunStatus
from app.db.base import Base


class Run(Base):
    """A reported failed execution; status describes diagnostic processing, not the source run."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("octet_length(original_trace) > 0", name="original_trace_nonempty"),
        CheckConstraint(
            "(status <> 'failed' AND failure_code IS NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL)",
            name="failure_state",
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name="claim_state",
        ),
        CheckConstraint("processing_attempts >= 0", name="processing_attempts_nonnegative"),
        Index("ix_runs_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(100))
    failure_signal: Mapped[str] = mapped_column(Text)
    original_trace: Mapped[bytes] = mapped_column(LargeBinary)
    trace_content_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda states: [state.value for state in states],
            name="run_status",
            length=16,
        ),
        default=RunStatus.PENDING,
        server_default=RunStatus.PENDING.value,
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    task: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[UUID | None] = mapped_column()
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
