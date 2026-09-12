from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TraceStep(Base):
    """Derived framework-independent evidence; the source bytes remain on Run."""

    __tablename__ = "trace_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_trace_steps_run_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    sequence: Mapped[int]
    agent_id: Mapped[str | None] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(50))
    tool_name: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    output: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    tool_arguments: Mapped[JsonValue | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    tool_result: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    step_metadata: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
