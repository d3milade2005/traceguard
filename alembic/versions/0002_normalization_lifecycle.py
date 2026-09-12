"""Add normalization lifecycle and fenced worker claims.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_failure_state"), "runs", type_="check")
    op.add_column(
        "runs",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("runs", sa.Column("task", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runs", sa.Column("processing_attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.create_check_constraint(
        "run_status",
        "runs",
        "status IN ('pending', 'queued', 'processing', 'normalized', 'failed')",
    )
    op.create_check_constraint(
        "failure_state",
        "runs",
        "(status <> 'failed' AND failure_code IS NULL AND failed_at IS NULL) OR "
        "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "claim_state",
        "runs",
        "(status = 'processing' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL) OR "
        "(status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "processing_attempts_nonnegative", "runs", "processing_attempts >= 0"
    )


def downgrade() -> None:
    # Stop workers before downgrade. Preserve raw traces and derived steps, but mark
    # phase-2 states failed because phase 1 cannot represent their processing state.
    op.drop_constraint(op.f("ck_runs_claim_state"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_failure_state"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_processing_attempts_nonnegative"), "runs", type_="check")
    op.execute(
        "UPDATE runs SET status = 'failed', failure_code = 'SCHEMA_DOWNGRADED', "
        "failed_at = CURRENT_TIMESTAMP WHERE status NOT IN ('pending', 'failed')"
    )
    for column in ("processing_attempts", "claim_expires_at", "claim_token", "task", "updated_at"):
        op.drop_column("runs", column)
    op.create_check_constraint("run_status", "runs", "status IN ('pending', 'failed')")
    op.create_check_constraint(
        "failure_state",
        "runs",
        "(status = 'pending' AND failure_code IS NULL AND failed_at IS NULL) OR "
        "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL)",
    )
