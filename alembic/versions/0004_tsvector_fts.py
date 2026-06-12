"""tsvector + GIN replaces FTS5
Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    # messages.content
    op.execute("""
        ALTER TABLE messages
            ADD COLUMN content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED;
    """)
    op.execute("CREATE INDEX messages_content_tsv ON messages USING GIN (content_tsv);")

    # notes.key + content + tags (the old notes_fts indexed all three).
    op.execute("""
        ALTER TABLE notes
            ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(key, '') || ' ' ||
                    coalesce(content, '') || ' ' ||
                    coalesce(tags, ''))
            ) STORED;
    """)
    op.execute("CREATE INDEX notes_search_tsv ON notes USING GIN (search_tsv);")

    # skills (slug + name + description + when_to_use + body_markdown).
    op.execute("""
        ALTER TABLE skills
            ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(slug, '') || ' ' ||
                    coalesce(name, '') || ' ' ||
                    coalesce(description, '') || ' ' ||
                    coalesce(when_to_use, '') || ' ' ||
                    coalesce(body_markdown, ''))
            ) STORED;
    """)
    op.execute("CREATE INDEX skills_search_tsv ON skills USING GIN (search_tsv);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS skills_search_tsv;")
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS search_tsv;")
    op.execute("DROP INDEX IF EXISTS notes_search_tsv;")
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS search_tsv;")
    op.execute("DROP INDEX IF EXISTS messages_content_tsv;")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS content_tsv;")
