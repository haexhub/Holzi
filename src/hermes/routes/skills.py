"""HTTP API for skills (Plan 33).

Thin CRUD surface over the `skills` table. The slug is validated at the
route layer (kebab-case 2..64 chars); duplicate-slug inserts are mapped
from IntegrityError to a 409. `body_markdown` is capped at 16 KiB —
larger payloads almost always indicate a paste of an entire document and
would balloon the composed system prompt past any reasonable budget.

Persona-skill activation endpoints live in `routes/preferences.py`
because the resource shape lives under `/api/personas/{id}` and shares
the persona-lookup helpers there.
"""
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.errors import ErrorCode
from hermes.repository import skills as skills_repo
from hermes.repository.models import Skill

router = APIRouter(prefix="/api")


# `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$` — 1..64 chars, kebab-case.
# Matches the Plan 33 spec verbatim. The regex deliberately allows a
# single alphanumeric character: it's hard to read in a long list, but
# the user gets to pick their own slugs and an artificial 2-char floor
# would frustrate experimentation. UNIQUE(slug) at the DB layer still
# prevents collisions.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

_BODY_MAX_LEN = 16 * 1024


class SkillResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    when_to_use: str | None
    body_markdown: str
    enabled: bool
    created_at: int
    updated_at: int


class SkillListResponse(BaseModel):
    skills: list[SkillResponse]


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=500)
    when_to_use: str | None = Field(default=None, max_length=500)
    body_markdown: str = Field(min_length=1, max_length=_BODY_MAX_LEN)
    enabled: bool = True


class SkillUpdate(BaseModel):
    """Patch — slug is immutable, omit any field to leave it alone."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    when_to_use: str | None = Field(default=None, max_length=500)
    body_markdown: str | None = Field(
        default=None, min_length=1, max_length=_BODY_MAX_LEN
    )
    enabled: bool | None = None


def _db(request: Request) -> AsyncEngine:
    return request.app.state.db


def _skill_to_dict(s: Skill) -> dict[str, Any]:
    return {
        "id": s.id,
        "slug": s.slug,
        "name": s.name,
        "description": s.description,
        "when_to_use": s.when_to_use,
        "body_markdown": s.body_markdown,
        "enabled": s.enabled,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


@router.get("/skills", response_model=SkillListResponse)
async def list_skills(request: Request) -> dict[str, Any]:
    rows = await skills_repo.list_all(_db(request))
    return {"skills": [_skill_to_dict(s) for s in rows]}


@router.post(
    "/skills",
    status_code=status.HTTP_201_CREATED,
    response_model=SkillResponse,
)
async def create_skill(
    body: SkillCreate, request: Request
) -> dict[str, Any]:
    if not _SLUG_RE.match(body.slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ErrorCode.SKILL_INVALID_SLUG.value,
        )
    try:
        skill = await skills_repo.create(
            _db(request),
            slug=body.slug,
            name=body.name,
            description=body.description,
            when_to_use=body.when_to_use or "",
            body_markdown=body.body_markdown,
            enabled=body.enabled,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": ErrorCode.SKILL_SLUG_CONFLICT.value,
                "params": {"slug": body.slug},
            },
        ) from exc
    return _skill_to_dict(skill)


@router.put("/skills/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: int, body: SkillUpdate, request: Request
) -> dict[str, Any]:
    updated = await skills_repo.update(
        _db(request),
        skill_id,
        name=body.name,
        description=body.description,
        when_to_use=body.when_to_use,
        body_markdown=body.body_markdown,
        enabled=body.enabled,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.SKILL_NOT_FOUND.value,
                "params": {"id": skill_id},
            },
        )
    return _skill_to_dict(updated)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: int, request: Request) -> Response:
    deleted = await skills_repo.delete(_db(request), skill_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.SKILL_NOT_FOUND.value,
                "params": {"id": skill_id},
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
