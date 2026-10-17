"""Durable GitHub sessions, safe output diagnostics, and ledger indexes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("github_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.String(39), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("avatar_url", sa.String(2048)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_login_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("github_id > 0", name="github_id_positive"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_sha256", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("token_sha256 ~ '^[0-9a-f]{64}$'", name="token_digest"),
        sa.CheckConstraint("expires_at > created_at", name="expiry"),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "oauth_flows",
        sa.Column("state_sha256", sa.String(64), primary_key=True),
        sa.Column("browser_sha256", sa.String(64), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("next_path", sa.String(1024), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state_sha256 ~ '^[0-9a-f]{64}$'", name="state_digest"),
        sa.CheckConstraint("browser_sha256 ~ '^[0-9a-f]{64}$'", name="browser_digest"),
        sa.CheckConstraint("expires_at > created_at", name="expiry"),
    )
    op.create_index("ix_oauth_flows_expires_at", "oauth_flows", ["expires_at"])
    for table in ("attributions", "regression_specs"):
        op.add_column(table, sa.Column("output_diagnostics", postgresql.JSONB(none_as_null=True)))
    op.create_index("ix_runs_created_at_id", "runs", ["created_at", "id"])
    op.create_index("ix_analyses_status_deadline_at", "analyses", ["status", "deadline_at"])
    op.create_index("ix_attributions_responsible_agent", "attributions", ["responsible_agent"])


def downgrade() -> None:
    op.drop_index("ix_attributions_responsible_agent", table_name="attributions")
    op.drop_index("ix_analyses_status_deadline_at", table_name="analyses")
    op.drop_index("ix_runs_created_at_id", table_name="runs")
    for table in ("regression_specs", "attributions"):
        op.drop_column(table, "output_diagnostics")
    op.drop_index("ix_oauth_flows_expires_at", table_name="oauth_flows")
    op.drop_table("oauth_flows")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
