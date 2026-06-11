import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.repository import conversations, messages
from hermes.repository.models import Conversation

router = APIRouter()

CHAT_PATH = "/v1/chat/completions"
CLINE_CHANNEL = "cline"
_DEFAULT_WORKSPACE = "default"


@router.post(CHAT_PATH)
async def chat_completions(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=ErrorCode.REQUEST_INVALID_JSON.value
        ) from exc
    is_stream = bool(body.get("stream", False))

    db: AsyncEngine = request.app.state.db
    upstream: httpx.AsyncClient = request.app.state.upstream
    user_id = current_user_id(request)

    convo = await _resolve_conversation(request, db, user_id)
    await _persist_last_user_message(db, body.get("messages", []), convo.id)

    response_headers = {"X-Hermes-Session": str(convo.id)}

    if is_stream:
        return await _stream_forward(
            upstream, body, response_headers, db, convo.id, user_id
        )
    return await _oneshot_forward(
        upstream, body, response_headers, db, convo.id, user_id
    )


async def _resolve_conversation(
    request: Request, db: AsyncEngine, user_id: int
) -> Conversation:
    header = request.headers.get("x-hermes-session")
    if header is not None:
        try:
            conv_id = int(header)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=ErrorCode.CHAT_INVALID_SESSION.value
            ) from exc
        convo = await conversations.get(db, conv_id, user_id=user_id)
        if convo is None:
            raise HTTPException(
                status_code=404, detail=ErrorCode.CHAT_SESSION_NOT_FOUND.value
            )
        return convo

    workspace = request.headers.get("x-holzi-workspace", _DEFAULT_WORKSPACE)
    existing = await conversations.find_latest_by_external_id(
        db, user_id=user_id, channel=CLINE_CHANNEL, external_id=workspace
    )
    if existing is not None:
        return existing
    return await conversations.create(
        db, user_id=user_id, channel=CLINE_CHANNEL, external_id=workspace
    )


async def _persist_last_user_message(
    db: AsyncEngine, msgs: list[dict[str, Any]], conv_id: int
) -> None:
    if not msgs:
        return
    last = msgs[-1]
    if last.get("role") != "user":
        return
    content = last.get("content", "")
    if isinstance(content, list):
        # OpenAI multi-modal: concatenate text parts, ignore others for now.
        content = "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    await messages.append(db, conversation_id=conv_id, role="user", content=str(content))


async def _oneshot_forward(
    upstream: httpx.AsyncClient,
    body: dict[str, Any],
    headers: dict[str, str],
    db: AsyncEngine,
    conv_id: int,
    user_id: int,
) -> Response:
    try:
        upstream_resp = await upstream.post(CHAT_PATH, json=body)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504, detail=ErrorCode.CHAT_UPSTREAM_TIMEOUT.value
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=ErrorCode.CHAT_UPSTREAM_UNREACHABLE.value
        ) from exc

    if upstream_resp.status_code >= 400:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "application/json"),
            headers=headers,
        )

    try:
        data = upstream_resp.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502, detail=ErrorCode.CHAT_UPSTREAM_NON_JSON.value
        ) from exc
    assistant_content = _extract_assistant_from_oneshot(data)
    if assistant_content:
        await messages.append(
            db, conversation_id=conv_id, role="assistant", content=assistant_content
        )
    await conversations.touch(db, conv_id, user_id=user_id)

    return Response(
        content=json.dumps(data),
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


async def _stream_forward(
    upstream: httpx.AsyncClient,
    body: dict[str, Any],
    headers: dict[str, str],
    db: AsyncEngine,
    conv_id: int,
    user_id: int,
) -> StreamingResponse:
    request = upstream.build_request("POST", CHAT_PATH, json=body)
    try:
        upstream_resp = await upstream.send(request, stream=True)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504, detail=ErrorCode.CHAT_UPSTREAM_TIMEOUT.value
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=ErrorCode.CHAT_UPSTREAM_UNREACHABLE.value
        ) from exc

    if upstream_resp.status_code >= 400:
        error_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        raise HTTPException(
            status_code=upstream_resp.status_code,
            detail={
                "code": ErrorCode.CHAT_UPSTREAM_ERROR.value,
                "params": {
                    "message": error_body.decode("utf-8", errors="replace"),
                },
            },
        )

    async def gen() -> AsyncIterator[bytes]:
        chunks: list[bytes] = []
        try:
            async for raw in upstream_resp.aiter_raw():
                chunks.append(raw)
                yield raw
        finally:
            await upstream_resp.aclose()
            content = _extract_assistant_from_sse(b"".join(chunks))
            if content:
                await messages.append(
                    db, conversation_id=conv_id, role="assistant", content=content
                )
            await conversations.touch(db, conv_id, user_id=user_id)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


def _extract_assistant_from_oneshot(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    return str(content) if content else ""


def _extract_assistant_from_sse(raw: bytes) -> str:
    parts: list[str] = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        payload = line[len(b"data: ") :]
        if payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            parts.append(str(content))
    return "".join(parts)
