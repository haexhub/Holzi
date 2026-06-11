from sqlalchemy import Boolean
from hermes.schema import (
    conversations, messages, notes, agent_tasks, personas, llm_credentials,
    sessions, users, attachments, agent_runs, persona_history, tool_approvals,
)

def test_boolean_columns_are_real_bool():
    for col in (conversations.c.bookmarked, agent_tasks.c.enabled,
                personas.c.is_default, llm_credentials.c.is_active,
                users.c.bootstrap_completed):
        assert isinstance(col.type, Boolean), f"{col} should be Boolean"

def test_personal_tables_carry_user_id():
    for table in (conversations, messages, notes, agent_tasks, personas,
                  attachments, agent_runs, persona_history, tool_approvals,
                  llm_credentials, sessions):
        assert "user_id" in table.c, f"{table.name} missing user_id"
