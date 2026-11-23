"""Persist deterministic statically validated PyTest compilation artifacts.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("regression_spec_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("framework", sa.String(length=16), nullable=False),
        sa.Column("test_name", sa.String(length=100), nullable=True),
        sa.Column("compiler_version", sa.String(length=100), nullable=False),
        sa.Column("harness_identifier", sa.String(length=100), nullable=False),
        sa.Column("harness_version", sa.String(length=100), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=True),
        sa.Column("source_digest", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "(status = 'completed' AND test_name IS NOT NULL AND source_code IS NOT NULL "
            "AND octet_length(source_code) > 0 AND source_digest IS NOT NULL AND "
            "source_digest ~ '^[0-9a-f]{64}$' AND completed_at IS NOT NULL AND "
            "failure_code IS NULL AND failed_at IS NULL) OR (status = 'failed' AND "
            "source_code IS NULL AND source_digest IS NULL AND completed_at IS NULL AND "
            "failure_code IS NOT NULL AND failed_at IS NOT NULL)",
            name=op.f("ck_generated_tests_result_state"),
        ),
        sa.CheckConstraint(
            "status IN ('completed','failed')", name=op.f("ck_generated_tests_status")
        ),
        sa.CheckConstraint(
            "test_name IS NULL OR test_name ~ '^test_[a-z][a-z0-9_]{0,79}$'",
            name=op.f("ck_generated_tests_test_name"),
        ),
        sa.CheckConstraint(
            "version = 'generated-test-v1' AND framework = 'pytest'",
            name=op.f("ck_generated_tests_format"),
        ),
        sa.ForeignKeyConstraint(
            ["regression_spec_id"],
            ["regression_specs.id"],
            name=op.f("fk_generated_tests_regression_spec_id_regression_specs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generated_tests")),
        sa.UniqueConstraint(
            "regression_spec_id",
            "compiler_version",
            "harness_identifier",
            "harness_version",
            name="uq_generated_tests_compilation",
        ),
    )
    op.create_index(
        "ix_generated_tests_status_created_at",
        "generated_tests",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generated_tests_status_created_at", table_name="generated_tests")
    op.drop_table("generated_tests")
