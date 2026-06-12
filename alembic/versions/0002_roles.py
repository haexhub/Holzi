"""create holzi_app runtime role
Revision ID: 0002
Revises: 0001
"""

import os

from alembic import op

revision = "0002"
down_revision = "0001"

# Tables holzi_app needs DML on. Excludes nothing — RLS, not GRANT, is the
# isolation mechanism. The role is the *vehicle* RLS uses (NOBYPASSRLS).
# Names are interpolated into raw SQL unquoted: every entry must be lowercase
# ASCII and not a reserved word. Add quoting if you need anything else.
RUNTIME_TABLES = [
    "users", "sessions",
    "conversations", "messages", "attachments", "agent_runs",
    "notes", "agent_tasks", "personas", "persona_history",
    "channel_prompts", "llm_credentials", "skills", "mcp_servers",
    "tool_approvals", "workspaces", "sandbox_crashes",
]


def upgrade() -> None:
    # The holzi_app role password is operator-supplied via env — NEVER hardcoded.
    # The SAME var backs settings.runtime_role_password (config.py), so the role
    # created here and the DSN the app connects with stay in sync. CREATE/ALTER
    # ROLE is DDL and cannot bind parameters, so the value is embedded as a SQL
    # literal with single quotes escaped (operator config, not user input).
    pw = os.getenv("HERMES_RUNTIME_ROLE_PASSWORD")
    if not pw:
        raise RuntimeError(
            "HERMES_RUNTIME_ROLE_PASSWORD must be set to run migration 0002 — "
            "it is the holzi_app role password; no default is baked in."
        )
    pw_lit = "'" + pw.replace("'", "''") + "'"
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'holzi_app') THEN
                CREATE ROLE holzi_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {pw_lit};
            END IF;
        END$$;
    """)
    # Sync the password on every apply — covers a pre-existing role and rotation.
    op.execute(f"ALTER ROLE holzi_app PASSWORD {pw_lit};")
    op.execute("GRANT CONNECT ON DATABASE holzi TO holzi_app;")
    op.execute("GRANT USAGE ON SCHEMA public TO holzi_app;")
    for t in RUNTIME_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO holzi_app;")
    # `ON ALL SEQUENCES` covers what exists today; the `ALTER DEFAULT
    # PRIVILEGES` lines below cover sequences/tables created by holzi_owner
    # AFTER this revision (e.g. future migrations).
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
