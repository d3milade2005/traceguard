from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','completed','review_required','failed')", name="status"
        ),
        CheckConstraint(
            "stage IN ('attribution','regression_spec','compilation','validation')", name="stage"
        ),
        CheckConstraint(
            "(status = 'active' AND finished_at IS NULL AND stop_reason IS NULL) OR "
            "(status <> 'active' AND finished_at IS NOT NULL AND stop_reason IS NOT NULL)",
            name="terminal_state",
        ),
        CheckConstraint("status <> 'completed' OR stop_reason = 'valid'", name="success"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    stage: Mapped[str] = mapped_column(String(20), default="attribution")
    model: Mapped[str] = mapped_column(String(200))
    reasoning_effort: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(64))
    attribution_id: Mapped[UUID | None] = mapped_column(ForeignKey("attributions.id"))
    regression_spec_id: Mapped[UUID | None] = mapped_column(ForeignKey("regression_specs.id"))
    generated_test_id: Mapped[UUID | None] = mapped_column(ForeignKey("generated_tests.id"))
    validation_id: Mapped[UUID | None] = mapped_column(ForeignKey("regression_validations.id"))
    stop_reason: Mapped[str | None] = mapped_column(String(100))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
