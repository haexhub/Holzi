"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-11 20:13:30.760626

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('mcp_servers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('transport', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('command_argv', sa.Text(), nullable=True),
    sa.Column('env_json', sa.Text(), nullable=True),
    sa.Column('credentials_iv', sa.Text(), nullable=True),
    sa.Column('credentials_tag', sa.Text(), nullable=True),
    sa.Column('credentials_data', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_index('idx_mcp_servers_enabled', 'mcp_servers', ['enabled'], unique=False)
    op.create_table('sandbox_crashes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=False),
    sa.Column('crashed_at', sa.Integer(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('last_message', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('sandbox_crashes_crashed_at', 'sandbox_crashes', [sa.literal_column('crashed_at DESC')], unique=False)
    op.create_table('skills',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('when_to_use', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('body_markdown', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('role', sa.Text(), server_default=sa.text("'member'"), nullable=False),
    sa.Column('parent_user_id', sa.Integer(), nullable=True),
    sa.Column('bootstrap_completed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.CheckConstraint("role IN ('platform_admin','member')", name='users_role_valid'),
    sa.ForeignKeyConstraint(['parent_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('workspaces',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('archived_at', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('due_at', sa.Integer(), nullable=True),
    sa.Column('schedule', sa.Text(), nullable=True),
    sa.Column('timezone', sa.Text(), server_default=sa.text("'UTC'"), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_run_at', sa.Integer(), nullable=True),
    sa.Column('last_status', sa.Text(), nullable=True),
    sa.Column('last_run_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('agent_tasks_enabled_due', 'agent_tasks', ['enabled', 'due_at'], unique=False)
    op.create_index('agent_tasks_user_enabled_due', 'agent_tasks', ['user_id', 'enabled', 'due_at'], unique=False)
    op.create_table('conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('external_id', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('started_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.Column('bookmarked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('expires_at', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('conv_channel_updated', 'conversations', ['channel', sa.literal_column('updated_at DESC')], unique=False)
    op.create_index('conv_user_updated', 'conversations', ['user_id', sa.literal_column('updated_at DESC')], unique=False)
    op.create_table('llm_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('mode', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('base_url', sa.Text(), nullable=True),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('api_key_iv', sa.Text(), nullable=True),
    sa.Column('api_key_tag', sa.Text(), nullable=True),
    sa.Column('api_key_data', sa.Text(), nullable=True),
    sa.Column('oauth_status', sa.Text(), nullable=True),
    sa.Column('oauth_authorized_at', sa.Integer(), nullable=True),
    sa.Column('oauth_iv', sa.Text(), nullable=True),
    sa.Column('oauth_tag', sa.Text(), nullable=True),
    sa.Column('oauth_data', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('llm_credentials_user', 'llm_credentials', ['user_id'], unique=False)
    op.create_table('notes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tags', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'key', name='notes_user_key')
    )
    op.create_index('notes_tags', 'notes', ['tags'], unique=False)
    op.create_index('notes_user', 'notes', ['user_id'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('last_used_at', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index('sessions_user', 'sessions', ['user_id'], unique=False)
    op.create_table('tool_approvals',
    sa.Column('tool_name', sa.Text(), nullable=False),
    sa.Column('granted_at', sa.Integer(), nullable=False),
    sa.Column('last_used_at', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'tool_name', name='tool_approvals_pk')
    )
    op.create_table('agent_runs',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('started_at', sa.Integer(), nullable=False),
    sa.Column('finished_at', sa.Integer(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('error_trace', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('agent_task_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_task_id'], ['agent_tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('agent_runs_conv_started', 'agent_runs', ['conversation_id', sa.literal_column('started_at DESC')], unique=False)
    op.create_index('agent_runs_status_started', 'agent_runs', ['status', sa.literal_column('started_at DESC')], unique=False)
    op.create_index('agent_runs_user_started', 'agent_runs', ['user_id', sa.literal_column('started_at DESC')], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('ts', sa.Integer(), nullable=False),
    sa.Column('meta_json', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('messages_user_ts', 'messages', ['user_id', sa.literal_column('ts DESC')], unique=False)
    op.create_index('msg_conv_ts', 'messages', ['conversation_id', 'ts'], unique=False)
    op.create_table('personas',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('soul', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('identity', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('agents', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.Column('llm_credential_id', sa.Integer(), nullable=True),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['llm_credential_id'], ['llm_credentials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'name', name='personas_user_name')
    )
    op.create_index('personas_user_default', 'personas', ['user_id', 'is_default'], unique=False)
    op.create_table('attachments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('filename', sa.Text(), nullable=False),
    sa.Column('content_type', sa.Text(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('storage_path', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('attachments_conversation', 'attachments', ['conversation_id'], unique=False)
    op.create_index('attachments_message', 'attachments', ['message_id'], unique=False)
    op.create_index('attachments_user', 'attachments', ['user_id'], unique=False)
    op.create_table('channel_prompts',
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('default_persona_id', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['default_persona_id'], ['personas.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('channel')
    )
    op.create_table('persona_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('persona_id', sa.Integer(), nullable=False),
    sa.Column('author', sa.Text(), server_default=sa.text("'user'"), nullable=False),
    sa.Column('snapshot_json', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['persona_id'], ['personas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_persona_history_persona', 'persona_history', ['persona_id'], unique=False)
    op.create_index('persona_history_user', 'persona_history', ['user_id'], unique=False)

    # Per-user partial unique on llm_credentials.user_id WHERE is_active=true.
    # Also declared in src/hermes/schema.py so future autogenerate runs see it
    # in metadata; kept here explicitly because this revision must produce the
    # index from a cold start.
    op.create_index(
        "llm_credentials_user_active_uq",
        "llm_credentials",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Partial unique on llm_credentials goes first — drop before its table.
    op.drop_index("llm_credentials_user_active_uq", table_name="llm_credentials")
    op.drop_index('persona_history_user', table_name='persona_history')
    op.drop_index('idx_persona_history_persona', table_name='persona_history')
    op.drop_table('persona_history')
    op.drop_table('channel_prompts')
    op.drop_index('attachments_user', table_name='attachments')
    op.drop_index('attachments_message', table_name='attachments')
    op.drop_index('attachments_conversation', table_name='attachments')
    op.drop_table('attachments')
    op.drop_index('personas_user_default', table_name='personas')
    op.drop_table('personas')
    op.drop_index('msg_conv_ts', table_name='messages')
    op.drop_index('messages_user_ts', table_name='messages')
    op.drop_table('messages')
    op.drop_index('agent_runs_user_started', table_name='agent_runs')
    op.drop_index('agent_runs_status_started', table_name='agent_runs')
    op.drop_index('agent_runs_conv_started', table_name='agent_runs')
    op.drop_table('agent_runs')
    op.drop_table('tool_approvals')
    op.drop_index('sessions_user', table_name='sessions')
    op.drop_table('sessions')
    op.drop_index('notes_user', table_name='notes')
    op.drop_index('notes_tags', table_name='notes')
    op.drop_table('notes')
    op.drop_index('llm_credentials_user', table_name='llm_credentials')
    op.drop_table('llm_credentials')
    op.drop_index('conv_user_updated', table_name='conversations')
    op.drop_index('conv_channel_updated', table_name='conversations')
    op.drop_table('conversations')
    op.drop_index('agent_tasks_user_enabled_due', table_name='agent_tasks')
    op.drop_index('agent_tasks_enabled_due', table_name='agent_tasks')
    op.drop_table('agent_tasks')
    op.drop_table('workspaces')
    op.drop_table('users')
    op.drop_table('skills')
    op.drop_index('sandbox_crashes_crashed_at', table_name='sandbox_crashes')
    op.drop_table('sandbox_crashes')
    op.drop_index('idx_mcp_servers_enabled', table_name='mcp_servers')
    op.drop_table('mcp_servers')
