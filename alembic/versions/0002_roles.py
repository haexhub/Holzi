"""create holzi_app runtime role
Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"

# Tables holzi_app needs DML on. Excludes nothing — RLS, not GRANT, is the
# isolation mechanism. The role is the *vehicle* RLS uses (NOBYPASSRLS).
RUNTIME_TABLES = [
    "users", "sessions",
    "conversations", "messages", "attachments", "agent_runs",
    "notes", "agent_tasks", "personas", "persona_history",
    "channel_prompts", "llm_credentials", "skills", "mcp_servers",
    "tool_approvals", "workspaces", "sandbox_crashes",
]


def upgrade() -> None:
    # Idempotent role creation. Password comes from a settings-supplied
    # SQL parameter so dev/prod can differ. Alembic doesn't bind via psycopg
    # for DO blocks — embed the literal but keep the dev value low-trust.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'holzi_app') THEN
                CREATE ROLE holzi_app
                    LOGIN
                    NOSUPERUSER
                    NOBYPASSRLS
                    PASSWORD 'holzi_app_dev_pw';
            END IF;
        END$$;
    """)
    op.execute("GRANT CONNECT ON DATABASE holzi TO holzi_app;")
    op.execute("GRANT USAGE ON SCHEMA public TO holzi_app;")
    for t in RUNTIME_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO holzi_app;")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO holzi_app;")
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO holzi_app;
    """)
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO holzi_app;
    """)


def downgrade() -> None:
    # Mirror order of upgrade in reverse. Default privileges and sequence
    # grants must be revoked before DROP ROLE, otherwise PostgreSQL refuses
    # the drop with "objects depend on it".
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE USAGE, SELECT ON SEQUENCES FROM holzi_app;
    """)
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM holzi_app;
    """)
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM holzi_app;")
    for t in reversed(RUNTIME_TABLES):
        op.execute(f"REVOKE ALL ON {t} FROM holzi_app;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM holzi_app;")
    op.execute("REVOKE CONNECT ON DATABASE holzi FROM holzi_app;")
    op.execute("DROP ROLE IF EXISTS holzi_app;")
