"""Persist isolated faulty/corrected validation operations.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regression_validations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generated_test_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("validator_version", sa.String(length=100), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("compiler_version", sa.String(length=100), nullable=False),
        sa.Column("harness_identifier", sa.String(length=100), nullable=False),
        sa.Column("harness_version", sa.String(length=100), nullable=False),
        sa.Column("image_reference", sa.String(length=300), nullable=False),
        sa.Column(
            "runtime_identity",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("verdict", sa.String(length=24), nullable=True),
        sa.Column("faulty_outcome", sa.String(length=24), nullable=True),
        sa.Column("corrected_outcome", sa.String(length=24), nullable=True),
        sa.Column(
            "faulty_execution",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "corrected_execution",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
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
            "(status = 'completed' AND verdict IS NOT NULL AND faulty_outcome IS NOT NULL AND"
            " corrected_outcome IS NOT NULL AND faulty_execution IS NOT NULL AND "
            "corrected_execution IS NOT NULL AND runtime_identity IS NOT NULL) OR (status <> "
            "'completed' AND verdict IS NULL AND faulty_outcome IS NULL AND corrected_outcome"
            " IS NULL AND faulty_execution IS NULL AND corrected_execution IS NULL)",
            name=op.f("ck_regression_validations_result_state"),
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_regression_validations_completion_state"),
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND failed_at IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL AND failed_at IS NULL)",
            name=op.f("ck_regression_validations_failure_state"),
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL AND claim_expires_at IS NOT "
            "NULL) OR (status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS"
            " NULL)",
            name=op.f("ck_regression_validations_claim_state"),
        ),
        sa.CheckConstraint(
            "corrected_outcome IS NULL OR corrected_outcome IN "
            "('passed','assertion_failed','execution_error','timeout','resource_limit')",
            name=op.f("ck_regression_validations_corrected_outcome"),
        ),
        sa.CheckConstraint(
            "faulty_outcome IS NULL OR faulty_outcome IN "
            "('passed','assertion_failed','execution_error','timeout','resource_limit')",
            name=op.f("ck_regression_validations_faulty_outcome"),
        ),
        sa.CheckConstraint(
            "source_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_regression_validations_source_digest")
        ),
        sa.CheckConstraint(
            "status IN ('pending','queued','processing','completed','failed')",
            name=op.f("ck_regression_validations_status"),
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict = CASE WHEN faulty_outcome NOT IN "
            "('passed','assertion_failed') OR corrected_outcome NOT IN "
            "('passed','assertion_failed') THEN 'inconclusive' WHEN faulty_outcome = 'passed'"
            " THEN 'not_reproduced' WHEN corrected_outcome = 'passed' THEN 'valid' ELSE "
            "'not_discriminating' END",
            name=op.f("ck_regression_validations_verdict_state"),
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN "
            "('valid','not_reproduced','not_discriminating','inconclusive')",
            name=op.f("ck_regression_validations_verdict"),
        ),
        sa.CheckConstraint(
            "processing_attempts >= 0 AND processing_attempts <= 3",
            name=op.f("ck_regression_validations_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["generated_test_id"],
            ["generated_tests.id"],
            name=op.f("fk_regression_validations_generated_test_id_generated_tests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regression_validations")),
        sa.UniqueConstraint(
            "generated_test_id", "validator_version", name="uq_regression_validations_operation"
        ),
    )

    op.create_index(
        "ix_regression_validations_status_created_at",
        "regression_validations",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_regression_validations_status_created_at", table_name="regression_validations"
    )
    op.drop_table("regression_validations")
