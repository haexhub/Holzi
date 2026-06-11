"""Built-in skill tools: skill_load + skill_search (Plan 37 Task 4).

`skill_load` retrieves the full body of a skill by slug.
`skill_search` performs Postgres tsvector full-text search over all
enabled skills (uses the `search_tsv` generated column + GIN index).
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
            "properties": {
                "slug": {"type": "string", "minLength": 1, "maxLength": 64},
            },
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
        # `to_tsquery` rejects raw user input (operator chars like `&`,
        # `|`, `!`, `:` are syntax). Strip everything that isn't alnum
        # or `_`, emit each surviving token as a prefix match, OR-join.
        # Matches the tokenisation in `routes/api.py::_tsquery`.
        tokens: list[str] = []
        for raw_token in query.split():
            cleaned = "".join(c for c in raw_token if c.isalnum() or c == "_")
            if cleaned:
                tokens.append(f"{cleaned}:*")
        if not tokens:
            return json.dumps({"results": []})
        tsq = " | ".join(tokens)
        try:
            async with db.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT s.slug, s.name, s.description, s.when_to_use, "
                            "       ts_headline('simple', s.body_markdown, "
                            "                   to_tsquery('simple', :q), "
                            "                   'StartSel=«,StopSel=»,MaxWords=12,"
                            "MinWords=5,ShortWord=2') AS snippet "
                            "FROM skills s "
                            "WHERE s.search_tsv @@ to_tsquery('simple', :q) "
                            "  AND s.enabled = TRUE "
                            "ORDER BY ts_rank(s.search_tsv, to_tsquery('simple', :q)) "
                            "         DESC "
                            "LIMIT 5"
                        ),
                        {"q": tsq},
                    )
                ).all()
        except OperationalError as exc:
            logger.warning(
                "skill_search tsvector query error, returning empty",
                query=query,
                error=str(exc),
            )
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
