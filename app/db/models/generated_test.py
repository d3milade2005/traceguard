from datetime import datetime
from uuid import UUID, uuid4

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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GeneratedTest(Base):
    __tablename__ = "generated_tests"
    __table_args__ = (
        UniqueConstraint(
            "regression_spec_id",
            "compiler_version",
            "harness_identifier",
            "harness_version",
            name="uq_generated_tests_compilation",
        ),
        CheckConstraint("status IN ('completed','failed')", name="status"),
        CheckConstraint("version = 'generated-test-v1' AND framework = 'pytest'", name="format"),
        CheckConstraint(
            "(status = 'completed' AND test_name IS NOT NULL AND source_code IS NOT NULL "
            "AND octet_length(source_code) > 0 AND source_digest IS NOT NULL "
            "AND source_digest ~ '^[0-9a-f]{64}$' AND completed_at IS NOT NULL "
            "AND failure_code IS NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND source_code IS NULL AND source_digest IS NULL "
            "AND completed_at IS NULL AND failure_code IS NOT NULL AND failed_at IS NOT NULL)",
            name="result_state",
        ),
        CheckConstraint(
            "test_name IS NULL OR test_name ~ '^test_[a-z][a-z0-9_]{0,79}$'", name="test_name"
        ),
        Index("ix_generated_tests_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    regression_spec_id: Mapped[UUID] = mapped_column(
        ForeignKey("regression_specs.id", ondelete="CASCADE")
    )
    version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16))
    framework: Mapped[str] = mapped_column(String(16))
    test_name: Mapped[str | None] = mapped_column(String(100))
    compiler_version: Mapped[str] = mapped_column(String(100))
    harness_identifier: Mapped[str] = mapped_column(String(100))
    harness_version: Mapped[str] = mapped_column(String(100))
    source_code: Mapped[str | None] = mapped_column(Text)
    source_digest: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
