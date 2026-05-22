import json
from collections.abc import AsyncIterator
from typing import Any

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from hermes.agent import run_agent
from hermes.config import settings
from hermes.logging import logger
from hermes.repository import (
    conversations,
    messages,
    notes,
    reminders,
    todos,
)
from hermes.tool_catalog import build_tool_catalog

router = APIRouter(prefix="/api")

WEB_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Marko, talking through the "
    "web UI. Be concise and direct."
)

WEB_CHANNEL = "web"

# Negative LIMIT disables LIMIT in SQLite — refuse non-positive values at the
# API boundary so an authenticated client can't trigger an unbounded scan.
MAX_LIST_LIMIT = 500


def _validate_limit(limit: int, *, max_limit: int = MAX_LIST_LIMIT) -> int:
    if limit < 1 or limit > max_limit:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {max_limit}",
        )
    return limit


# ---------------------------------------------------------------------------
# /api/chat
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: int | None = None


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@router.post("/chat")
async def api_chat(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    try:
        payload = ChatRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db: aiosqlite.Connection = request.app.state.db
    upstream: httpx.AsyncClient = request.app.state.upstream

    if payload.conversation_id is None:
        convo = await conversations.create(db, channel=WEB_CHANNEL)
    else:
        existing = await conversations.get(db, payload.conversation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown conversation_id")
        # /api/chat is the web surface — never let it append into a Signal or
        # VSCode thread, which would blur channel semantics and bypass the
        # per-channel system prompt + tool catalog.
        if existing.channel != WEB_CHANNEL:
            raise HTTPException(
                status_code=400,
                detail="conversation_id must reference a web conversation",
            )
        convo = existing

    await messages.append(
        db, conversation_id=convo.id, role="user", content=payload.message
    )

    tools = build_tool_catalog(
        db=db,
        signal_client=request.app.state.signal_client,
        signal_self_number=request.app.state.signal_self_number,
        external_http=request.app.state.external_http,
        brave_api_key=request.app.state.brave_api_key,
        current_channel=WEB_CHANNEL,
    )

    async def gen() -> AsyncIterator[bytes]:
        yield _sse_event("session", {"conversation_id": convo.id})
        try:
            reply = await run_agent(
                upstream=upstream,
                db=db,
                conversation_id=convo.id,
                system_prompt=WEB_SYSTEM_PROMPT,
                model=settings.model,
                tools=tools,
            )
        except Exception as exc:  # noqa: BLE001 — surface to client, don't crash worker
            logger.warning("api_chat_agent_error", error=str(exc))
            yield _sse_event("error", {"message": str(exc)})
            return
        await conversations.touch(db, convo.id)
        yield _sse_event("text", {"content": reply})
        yield _sse_event("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# /api/conversations
# ---------------------------------------------------------------------------


@router.get("/conversations")
async def api_list_conversations(
    request: Request, channel: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: aiosqlite.Connection = request.app.state.db
    convos = await conversations.list_all(db, channel=channel, limit=limit)
    out: list[dict[str, Any]] = []
    for c in convos:
        count = await conversations.message_count(db, c.id)
        out.append(
            {
                "id": c.id,
                "channel": c.channel,
                "title": c.title,
                "started_at": c.started_at,
                "updated_at": c.updated_at,
                "message_count": count,
            }
        )
    return out


@router.get("/conversations/{conv_id}")
async def api_get_conversation(
    request: Request, conv_id: int, limit: int = 200
) -> dict[str, Any]:
    limit = _validate_limit(limit)
    db: aiosqlite.Connection = request.app.state.db
    convo = await conversations.get(db, conv_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    msgs = await messages.list_by_conversation(db, conv_id, limit=limit)
    return {
        "conversation": {
            "id": convo.id,
            "channel": convo.channel,
            "title": convo.title,
            "started_at": convo.started_at,
            "updated_at": convo.updated_at,
        },
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "ts": m.ts}
            for m in msgs
        ],
    }


# ---------------------------------------------------------------------------
# /api/notes
# ---------------------------------------------------------------------------


class NoteCreate(BaseModel):
    key: str = Field(min_length=1)
    content: str
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    content: str
    tags: list[str] = Field(default_factory=list)


def _note_to_dict(n: Any) -> dict[str, Any]:
    return {
        "id": n.id,
        "key": n.key,
        "content": n.content,
        "tags": n.tags,
        "updated_at": n.updated_at,
    }


@router.get("/notes")
async def api_list_notes(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: aiosqlite.Connection = request.app.state.db
    items = await notes.list_all(db, limit=limit)
    return [_note_to_dict(n) for n in items]


@router.get("/notes/{key}")
async def api_get_note(request: Request, key: str) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    n = await notes.get(db, key)
    if n is None:
        raise HTTPException(status_code=404, detail="note not found")
    return _note_to_dict(n)


@router.post("/notes")
async def api_create_note(request: Request, body: NoteCreate) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(db, key=body.key, content=body.content, tags=tags)
    return _note_to_dict(n)


@router.put("/notes/{key}")
async def api_update_note(
    request: Request, key: str, body: NoteUpdate
) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(db, key=key, content=body.content, tags=tags)
    return _note_to_dict(n)


@router.delete("/notes/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_note(request: Request, key: str) -> Response:
    db: aiosqlite.Connection = request.app.state.db
    if not await notes.delete(db, key):
        raise HTTPException(status_code=404, detail="note not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /api/todos
# ---------------------------------------------------------------------------


class TodoCreate(BaseModel):
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class TodoUpdate(BaseModel):
    done: bool


def _todo_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "content": t.content,
        "tags": t.tags,
        "done_at": t.done_at,
        "created_at": t.created_at,
    }


@router.get("/todos")
async def api_list_todos(
    request: Request,
    only_open: bool = True,
    tag: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: aiosqlite.Connection = request.app.state.db
    items = await todos.list_all(db, only_open=only_open, tag=tag, limit=limit)
    return [_todo_to_dict(t) for t in items]


@router.post("/todos")
async def api_create_todo(request: Request, body: TodoCreate) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    t = await todos.add(db, content=body.content, tags=tags)
    return _todo_to_dict(t)


@router.patch("/todos/{todo_id}")
async def api_patch_todo(
    request: Request, todo_id: int, body: TodoUpdate
) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    if not body.done:
        raise HTTPException(
            status_code=400,
            detail="only marking todos as done is supported; pass {\"done\": true}",
        )
    if not await todos.mark_done(db, todo_id):
        # Either the row doesn't exist, or it was already done.
        existing = await todos.get(db, todo_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="todo not found")
        return _todo_to_dict(existing)
    t = await todos.get(db, todo_id)
    if t is None:
        raise HTTPException(status_code=404, detail="todo disappeared")
    return _todo_to_dict(t)


@router.delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_todo(request: Request, todo_id: int) -> Response:
    db: aiosqlite.Connection = request.app.state.db
    if not await todos.delete(db, todo_id):
        raise HTTPException(status_code=404, detail="todo not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /api/reminders
# ---------------------------------------------------------------------------


class ReminderCreate(BaseModel):
    due_at: int
    message: str = Field(min_length=1)
    channel: str = "signal"


def _reminder_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "due_at": r.due_at,
        "message": r.message,
        "channel": r.channel,
        "fired_at": r.fired_at,
        "created_at": r.created_at,
    }


@router.get("/reminders")
async def api_list_reminders(
    request: Request, include_fired: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: aiosqlite.Connection = request.app.state.db
    items = await reminders.list_all(db, include_fired=include_fired, limit=limit)
    return [_reminder_to_dict(r) for r in items]


@router.post("/reminders")
async def api_create_reminder(
    request: Request, body: ReminderCreate
) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    r = await reminders.create(
        db, due_at=body.due_at, message=body.message, channel=body.channel
    )
    return _reminder_to_dict(r)


@router.delete("/reminders/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_reminder(request: Request, reminder_id: int) -> Response:
    db: aiosqlite.Connection = request.app.state.db
    if not await reminders.delete(db, reminder_id):
        raise HTTPException(status_code=404, detail="reminder not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
