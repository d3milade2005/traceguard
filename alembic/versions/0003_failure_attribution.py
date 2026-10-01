"""Add explicit failure attribution and canonical evidence references.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("responsible_agent", sa.String(length=200), nullable=True),
        sa.Column("decisive_sequence", sa.Integer(), nullable=True),
        sa.Column("failure_category", sa.String(length=100), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("abstention_reason", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("response_id", sa.String(length=200), nullable=True),
        sa.Column("response_model", sa.String(length=200), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
        sa.Column("context_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "(status = 'completed' AND decision IS NOT NULL AND explanation IS NOT NULL "
            "AND confidence IS NOT NULL AND confidence IN ('low','medium','high') AND "
            "((decision = 'attributed' AND responsible_agent IS NOT NULL "
            "AND decisive_sequence IS NOT NULL AND abstention_reason IS NULL) OR "
            "(decision = 'abstain' AND responsible_agent IS NULL AND decisive_sequence IS NULL "
            "AND failure_category IS NULL AND abstention_reason IS NOT NULL))) OR "
            "(status <> 'completed' AND decision IS NULL AND responsible_agent IS NULL "
            "AND decisive_sequence IS NULL AND failure_category IS NULL AND explanation IS NULL "
            "AND confidence IS NULL AND abstention_reason IS NULL)",
            name=op.f("ck_attributions_decision_state"),
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_attributions_completion_state"),
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL AND failed_at IS NULL)",
            name=op.f("ck_attributions_failure_state"),
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name=op.f("ck_attributions_claim_state"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'processing', 'completed', 'failed')",
            name=op.f("ck_attributions_status"),
        ),
        sa.CheckConstraint(
            "decisive_sequence IS NULL OR decisive_sequence >= 0",
            name=op.f("ck_attributions_sequence"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name=op.f("ck_attributions_input_tokens")
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name=op.f("ck_attributions_latency")
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_attributions_output_tokens"),
        ),
        sa.CheckConstraint(
            "processing_attempts >= 0 AND processing_attempts <= 4",
            name=op.f("ck_attributions_attempts"),
        ),
        sa.CheckConstraint(
            "reasoning_tokens IS NULL OR reasoning_tokens >= 0",
            name=op.f("ck_attributions_reasoning_tokens"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "decisive_sequence"],
            ["trace_steps.run_id", "trace_steps.sequence"],
            name="fk_attributions_decisive_step",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_attributions_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attributions")),
        sa.UniqueConstraint("run_id", name="uq_attributions_run_id"),
    )
    op.create_index(
        "ix_attributions_status_created_at", "attributions", ["status", "created_at"], unique=False
    )
    op.create_table(
        "attribution_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attribution_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_sequence", sa.Integer(), nullable=False),
        sa.Column("end_sequence", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("contains_truncated", sa.Boolean(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_attribution_evidence_position")),
        sa.CheckConstraint(
            "start_sequence >= 0 AND end_sequence >= start_sequence",
            name=op.f("ck_attribution_evidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["attribution_id"],
            ["attributions.id"],
            name=op.f("fk_attribution_evidence_attribution_id_attributions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attribution_evidence")),
        sa.UniqueConstraint("attribution_id", "position", name="uq_attribution_evidence_position"),
        sa.UniqueConstraint(
            "attribution_id", "start_sequence", "end_sequence", name="uq_attribution_evidence_range"
        ),
    )


def downgrade() -> None:
    op.drop_table("attribution_evidence")
    op.drop_index("ix_attributions_status_created_at", table_name="attributions")
    op.drop_table("attributions")
