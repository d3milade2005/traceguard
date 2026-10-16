"""Persist product-level analysis progress."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("reasoning_effort", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("attribution_id", sa.Uuid(), sa.ForeignKey("attributions.id")),
        sa.Column("regression_spec_id", sa.Uuid(), sa.ForeignKey("regression_specs.id")),
        sa.Column("generated_test_id", sa.Uuid(), sa.ForeignKey("generated_tests.id")),
        sa.Column("validation_id", sa.Uuid(), sa.ForeignKey("regression_validations.id")),
        sa.Column("stop_reason", sa.String(100)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active','completed','review_required','failed')", name="status"
        ),
        sa.CheckConstraint(
            "stage IN ('attribution','regression_spec','compilation','validation')", name="stage"
        ),
        sa.CheckConstraint(
            "(status = 'active' AND finished_at IS NULL AND stop_reason IS NULL) OR "
            "(status <> 'active' AND finished_at IS NOT NULL AND stop_reason IS NOT NULL)",
            name="terminal_state",
        ),
        sa.CheckConstraint("status <> 'completed' OR stop_reason = 'valid'", name="success"),
    )


def downgrade() -> None:
    op.drop_table("analyses")
