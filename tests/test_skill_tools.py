"""Tests for skill_load + skill_search built-in tools (Plan 37 Task 4)."""
import json

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import skills as skills_repo
from hermes.tools.skills import build_skill_tools


async def _make_skill(engine, *, slug, enabled=True, when_to_use="") -> None:
    await skills_repo.create(
        engine,
        slug=slug,
        name=slug.title(),
        description=f"{slug} description",
        when_to_use=when_to_use,
        body_markdown=f"# {slug}\nThis is the body.",
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# skill_load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_load_happy_path(conn: AsyncEngine) -> None:
    """skill_load returns the full skill body for an enabled skill."""
    await _make_skill(conn, slug="my-skill")
    tools = build_skill_tools(conn)
    skill_load = next(t for t in tools if t.name == "skill_load")

    result = await skill_load.handler({"slug": "my-skill"})
    data = json.loads(result)

    assert data["slug"] == "my-skill"
    assert data["body_markdown"] == "# my-skill\nThis is the body."
    assert "description" in data


@pytest.mark.asyncio
async def test_skill_load_disabled_returns_404(conn: AsyncEngine) -> None:
    """skill_load raises 404 SKILL_NOT_FOUND for disabled skills."""
    await _make_skill(conn, slug="disabled-skill", enabled=False)
    tools = build_skill_tools(conn)
    skill_load = next(t for t in tools if t.name == "skill_load")

    with pytest.raises(HTTPException) as exc_info:
        await skill_load.handler({"slug": "disabled-skill"})
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "SKILL_NOT_FOUND"


@pytest.mark.asyncio
async def test_skill_load_unknown_slug_returns_404(conn: AsyncEngine) -> None:
    """skill_load raises 404 SKILL_NOT_FOUND for unknown slugs."""
    tools = build_skill_tools(conn)
    skill_load = next(t for t in tools if t.name == "skill_load")

    with pytest.raises(HTTPException) as exc_info:
        await skill_load.handler({"slug": "no-such-skill"})
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "SKILL_NOT_FOUND"


# ---------------------------------------------------------------------------
# skill_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_search_happy_path(conn: AsyncEngine) -> None:
    """skill_search returns matching enabled skills."""
    await skills_repo.create(
        conn,
        slug="code-review",
        name="Code Review",
        description="Reviews code",
        when_to_use="",
        body_markdown="Review your diff carefully.",
    )
    tools = build_skill_tools(conn)
    skill_search = next(t for t in tools if t.name == "skill_search")

    result = await skill_search.handler({"query": "code review"})
    data = json.loads(result)

    assert len(data["results"]) >= 1
    slugs = [r["slug"] for r in data["results"]]
    assert "code-review" in slugs


@pytest.mark.asyncio
async def test_skill_search_empty_query_returns_empty(conn: AsyncEngine) -> None:
    """skill_search with empty query → empty results, no error."""
    await _make_skill(conn, slug="something")
    tools = build_skill_tools(conn)
    skill_search = next(t for t in tools if t.name == "skill_search")

    result = await skill_search.handler({"query": ""})
    data = json.loads(result)

    assert data == {"results": []}


@pytest.mark.asyncio
async def test_skill_search_fts5_syntax_error_returns_empty(
    conn: AsyncEngine,
) -> None:
    """FTS5 OperationalError → empty results, no crash."""
    await _make_skill(conn, slug="anything")
    tools = build_skill_tools(conn)
    skill_search = next(t for t in tools if t.name == "skill_search")

    # FTS5 raises OperationalError on invalid query syntax like bare 'AND OR AND'
    result = await skill_search.handler({"query": "AND OR AND"})

    data = json.loads(result)
    assert data == {"results": []}
