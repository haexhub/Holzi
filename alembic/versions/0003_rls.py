"""enable RLS + per-user USING policies on personal tables
Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"

PERSONAL_TABLES = [
    "sessions",
    "conversations", "messages", "attachments", "agent_runs",
    "notes", "agent_tasks", "personas", "persona_history",
    "tool_approvals", "llm_credentials",
]


def upgrade() -> None:
    # The GUC must exist for `current_setting(..., true)` to return NULL
    # when unset (instead of raising). Define it at the database level so
    # every connection sees it; `SET LOCAL` will override per-transaction.
    op.execute("ALTER DATABASE holzi SET app.user_id TO '0';")

    for t in PERSONAL_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
        # FORCE makes the policy apply even to the table's owner (holzi_owner).
        # Without FORCE, the owner sees everything — the policy would only
        # protect holzi_app, and any future code path that opens an owner
        # connection (Alembic, ad-hoc psql) would bypass isolation.
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY {t}_user_isolation ON {t}
                USING (user_id = current_setting('app.user_id', true)::bigint)
                WITH CHECK (user_id = current_setting('app.user_id', true)::bigint);
        """)


def downgrade() -> None:
    for t in reversed(PERSONAL_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {t}_user_isolation ON {t};")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER DATABASE holzi RESET app.user_id;")
