"""Curated starter skill library (Plan 38 Wave A3).

Re-exports `STARTER_SKILLS` (from `starter_skills_data`) and provides
`ensure_starter_skills_seeded()` which inserts the 8 rows idempotently
via `INSERT ... ON CONFLICT (slug) DO NOTHING`.

User-edited bodies are never overwritten on re-boot: if the slug row
already exists, the INSERT is silently skipped.
"""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.starter_skills_data import STARTER_SKILLS

__all__ = ["STARTER_SKILLS", "ensure_starter_skills_seeded"]


async def ensure_starter_skills_seeded(engine: AsyncEngine) -> None:
    """Idempotent: insert all 8 starter skill rows.

    Called once per boot. If a skill's slug row already exists (because
    the user edited the body or a previous boot seeded it), the INSERT is
    silently skipped — user edits are never overwritten. `skills` is a
    global table (no RLS), so a direct `engine.begin()` is fine.
    """
    now = int(time.time())
    async with engine.begin() as conn:
        for skill in STARTER_SKILLS:
            await conn.execute(
                text(
                    "INSERT INTO skills"
                    "(slug, name, description, when_to_use, body_markdown,"
                    " enabled, created_at, updated_at) "
                    "VALUES (:slug, :name, :description, :when_to_use,"
                    " :body_markdown, true, :now, :now) "
                    "ON CONFLICT (slug) DO NOTHING"
                ),
                {
                    "slug": skill["slug"],
                    "name": skill["name"],
                    "description": skill["description"],
                    "when_to_use": skill["when_to_use"],
                    "body_markdown": skill["body_markdown"],
                    "now": now,
                },
            )
