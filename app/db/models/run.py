from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Enum, Index, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.run_status import RunStatus
from app.db.base import Base


class Run(Base):
    """A reported failed execution; status describes diagnostic processing, not the source run."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("octet_length(original_trace) > 0", name="original_trace_nonempty"),
        CheckConstraint(
            "(status = 'pending' AND failure_code IS NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL)",
            name="failure_state",
        ),
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
