"""Persist original failed traces and derived steps.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("failure_signal", sa.Text(), nullable=False),
        sa.Column("original_trace", sa.LargeBinary(), nullable=False),
        sa.Column("trace_content_type", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "failed",
                name="run_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "octet_length(original_trace) > 0", name=op.f("ck_runs_original_trace_nonempty")
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND failure_code IS NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL)",
            name=op.f("ck_runs_failure_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index("ix_runs_status_created_at", "runs", ["status", "created_at"])
    op.create_table(
        "trace_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(200), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("tool_name", sa.String(200), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("output", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("tool_arguments", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("tool_result", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("step_metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_trace_steps_sequence_nonnegative")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_trace_steps_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trace_steps")),
        sa.UniqueConstraint("run_id", "sequence", name="uq_trace_steps_run_sequence"),
    )


def downgrade() -> None:
    op.drop_table("trace_steps")
    op.drop_index("ix_runs_status_created_at", table_name="runs")
    op.drop_table("runs")
