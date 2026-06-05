"""Built-in skill tools: skill_load + skill_search (Plan 37 Task 4).

`skill_load` retrieves the full body of a skill by slug.
`skill_search` performs FTS5 full-text search over all enabled skills.
"""
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.errors import ErrorCode
from hermes.logging import logger


def build_skill_tools(db: AsyncEngine) -> list[Tool]:
    return [_skill_load(db), _skill_search(db)]


def _skill_load(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        slug = args["slug"]
        async with db.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT slug, name, description, when_to_use, "
                        "       body_markdown, enabled "
                        "FROM skills WHERE slug = :slug"
                    ),
                    {"slug": slug},
                )
            ).first()
        if row is None or not row.enabled:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": ErrorCode.SKILL_NOT_FOUND.value,
                    "params": {"slug": slug},
                },
            )
        return json.dumps(
            {
                "slug": row.slug,
                "name": row.name,
                "description": row.description,
                "when_to_use": row.when_to_use,
                "body_markdown": row.body_markdown,
            }
        )

    return Tool(
        name="skill_load",
        description=(
            "Load the full body of a skill by its slug. Use this when "
            "the catalog index suggests a relevant skill for the "
            "current task."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
        handler=handler,
        requires_approval=False,
        source="builtin",
    )


def _skill_search(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"results": []})
        try:
            async with db.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT s.slug, s.name, s.description, s.when_to_use, "
                            "       snippet(skills_fts, 4, '«', '»', '…', 12) AS snippet "
                            "FROM skills_fts "
                            "JOIN skills s ON s.id = skills_fts.rowid "
                            "WHERE skills_fts MATCH :q AND s.enabled = 1 "
                            "ORDER BY rank LIMIT 5"
                        ),
                        {"q": query},
                    )
                ).all()
        except OperationalError:
            logger.info("skill_search FTS5 query error, returning empty", query=query)
            return json.dumps({"results": []})
        return json.dumps({"results": [dict(r._mapping) for r in rows]})

    return Tool(
        name="skill_search",
        description=(
            "Search all enabled skills by free-text query. Returns up "
            "to 5 matches with a snippet of the matching body."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=handler,
        requires_approval=False,
        source="builtin",
    )
