"""`/notes` — key/value note storage with Postgres FTS over content+tags."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.repository import (
    notes,
)
from hermes.routes._helpers import validate_limit

router = APIRouter()


class NoteCreate(BaseModel):
    key: str = Field(min_length=1)
    content: str
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    content: str
    tags: list[str] = Field(default_factory=list)


class NoteResponse(BaseModel):
    id: int
    key: str
    content: str
    tags: str | None
    updated_at: int


def _note_to_dict(n: Any) -> dict[str, Any]:
    return {
        "id": n.id,
        "key": n.key,
        "content": n.content,
        "tags": n.tags,
        "updated_at": n.updated_at,
    }


def _tsquery(raw: str) -> str:
    # Postgres `to_tsquery` rejects raw user input — characters like `&`,
    # `|`, `!`, `(`, `)`, `:`, `<->` are operator syntax. Split on
    # whitespace, drop everything that isn't alnum or `_`, and emit each
    # surviving token as a `tok:*` prefix-match. Tokens are OR-joined
    # with `|` so multi-word queries widen recall (matches how chat
    # search elsewhere behaves).
    tokens: list[str] = []
    for raw_token in raw.split():
        cleaned = "".join(c for c in raw_token if c.isalnum() or c == "_")
        if cleaned:
            tokens.append(f"{cleaned}:*")
    return " | ".join(tokens)


@router.get("/notes", response_model=list[NoteResponse])
async def api_list_notes(
    request: Request, limit: int = 100, q: str | None = None
) -> list[dict[str, Any]]:
    limit = validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    user_id = current_user_id(request)
    # Whitespace-only `q` is treated the same as an absent `q` — falling
    # through to list_all keeps `?q=` and `?q=%20%20` symmetric.
    if q and q.strip():
        sanitised = _tsquery(q)
        if not sanitised:
            return []
        items = await notes.find(db, user_id=user_id, query=sanitised, limit=limit)
    else:
        items = await notes.list_all(db, user_id=user_id, limit=limit)
    return [_note_to_dict(n) for n in items]


@router.get("/notes/{key}", response_model=NoteResponse)
async def api_get_note(request: Request, key: str) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    n = await notes.get(db, key, user_id=current_user_id(request))
    if n is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.NOTE_NOT_FOUND.value
        )
    return _note_to_dict(n)


@router.post("/notes", response_model=NoteResponse)
async def api_create_note(request: Request, body: NoteCreate) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(
        db, user_id=current_user_id(request), key=body.key, content=body.content, tags=tags
    )
    return _note_to_dict(n)


@router.put("/notes/{key}", response_model=NoteResponse)
async def api_update_note(
    request: Request, key: str, body: NoteUpdate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(
        db, user_id=current_user_id(request), key=key, content=body.content, tags=tags
    )
    return _note_to_dict(n)


@router.delete("/notes/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_note(request: Request, key: str) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await notes.delete(db, key, user_id=current_user_id(request)):
        raise HTTPException(
            status_code=404, detail=ErrorCode.NOTE_NOT_FOUND.value
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
