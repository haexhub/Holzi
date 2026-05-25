"""Worker-level tests. The Telegram client is faked via MockTransport so
no calls leak to api.telegram.org."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import conversations, messages
from hermes.telegram.client import TelegramClient
from hermes.telegram.worker import TelegramWorker

BOT_TOKEN = "12345:test-token"


def _make_client(
    updates_queue: list[list[dict[str, Any]]] | None = None,
    send_capture: list[dict[str, Any]] | None = None,
) -> TelegramClient:
    """`updates_queue` is a list of update-batches; each successive
    `get_updates` call pops the next batch (empty list when exhausted)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getUpdates"):
            batch = (
                updates_queue.pop(0)
                if updates_queue is not None and updates_queue
                else []
            )
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/sendMessage"):
            if send_capture is not None:
                send_capture.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404, json={"ok": False, "description": "not found"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.telegram.org")
    return TelegramClient(http, BOT_TOKEN)


def _update(
    *,
    update_id: int = 1,
    chat_id: int = 42,
    text: str | None = "hi",
    ts: int = 1700000000,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": update_id,
        "chat": {"id": chat_id, "type": "private"},
        "date": ts,
    }
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


def _echoing_agent_runner():
    async def runner(db: AsyncEngine, conversation_id: int) -> str:
        msgs = await messages.list_by_conversation(db, conversation_id)
        last_user = next((m for m in reversed(msgs) if m.role == "user"), None)
        reply = f"agent says: {last_user.content if last_user else ''}"
        await messages.append(
            db, conversation_id=conversation_id, role="assistant", content=reply
        )
        return reply

    return runner


def _never_called_agent_runner():
    async def runner(db: AsyncEngine, conversation_id: int) -> str:
        raise AssertionError("agent runner should not have been called")

    return runner


@pytest.mark.asyncio
async def test_process_update_persists_message_and_replies(
    conn: AsyncEngine,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_echoing_agent_runner(),
        allowed_chat_ids=None,
    )

    await worker.process_update(_update(chat_id=42, text="hi"))

    convos = await conversations.list_by_channel(conn, "telegram")
    assert len(convos) == 1
    assert convos[0].external_id == "tg:42"
    msgs = await messages.list_by_conversation(conn, convos[0].id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "agent says: hi"),
    ]
    assert sends == [{"chat_id": 42, "text": "agent says: hi"}]


@pytest.mark.asyncio
async def test_process_update_threads_per_chat_id(conn: AsyncEngine) -> None:
    """Two different chats must land in two distinct conversations even
    when they arrive in the same burst."""
    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_echoing_agent_runner(),
        allowed_chat_ids=None,
    )

    await worker.process_update(_update(update_id=1, chat_id=10, text="from-10"))
    await worker.process_update(_update(update_id=2, chat_id=20, text="from-20"))

    convos = await conversations.list_by_channel(conn, "telegram")
    assert {c.external_id for c in convos} == {"tg:10", "tg:20"}


@pytest.mark.asyncio
async def test_process_update_appends_to_existing_thread_within_6h(
    conn: AsyncEngine,
) -> None:
    one_hour_ago = int(time.time()) - 3600
    existing = await conversations.create(
        conn, channel="telegram", external_id="tg:42", ts=one_hour_ago
    )

    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_echoing_agent_runner(),
        allowed_chat_ids=None,
    )

    await worker.process_update(_update(chat_id=42, text="follow-up"))

    convos = await conversations.list_by_channel(conn, "telegram")
    assert len(convos) == 1
    assert convos[0].id == existing.id


@pytest.mark.asyncio
async def test_process_update_creates_new_thread_after_6h_gap(
    conn: AsyncEngine,
) -> None:
    seven_hours_ago = int(time.time()) - 7 * 3600
    existing = await conversations.create(
        conn, channel="telegram", external_id="tg:42", ts=seven_hours_ago
    )

    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_echoing_agent_runner(),
        allowed_chat_ids=None,
    )

    await worker.process_update(_update(chat_id=42, text="new thread"))

    convos = await conversations.list_by_channel(conn, "telegram")
    # Both rows exist; newest first.
    assert len(convos) == 2
    assert convos[0].id != existing.id


@pytest.mark.asyncio
async def test_process_update_ignores_non_text_messages(
    conn: AsyncEngine,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_never_called_agent_runner(),
        allowed_chat_ids=None,
    )

    await worker.process_update(_update(text=None))

    assert await conversations.list_by_channel(conn, "telegram") == []
    assert sends == []


@pytest.mark.asyncio
async def test_process_update_ignores_chats_not_in_allowlist(
    conn: AsyncEngine,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_never_called_agent_runner(),
        allowed_chat_ids=[42],
    )

    await worker.process_update(_update(chat_id=999, text="from-stranger"))

    assert await conversations.list_by_channel(conn, "telegram") == []
    assert sends == []


@pytest.mark.asyncio
async def test_process_update_accepts_chats_in_allowlist(
    conn: AsyncEngine,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=_echoing_agent_runner(),
        allowed_chat_ids=[42, 99],
    )

    await worker.process_update(_update(chat_id=42, text="allowed"))

    assert len(sends) == 1
    assert sends[0]["chat_id"] == 42


@pytest.mark.asyncio
async def test_process_update_touches_thread_even_when_agent_fails(
    conn: AsyncEngine,
) -> None:
    one_hour_ago = int(time.time()) - 3600
    existing = await conversations.create(
        conn, channel="telegram", external_id="tg:42", ts=one_hour_ago
    )

    async def failing_runner(db: AsyncEngine, conversation_id: int) -> str:
        raise RuntimeError("simulated agent failure")

    sends: list[dict[str, Any]] = []
    worker = TelegramWorker(
        _make_client(send_capture=sends),
        conn,
        agent_runner=failing_runner,
        allowed_chat_ids=None,
    )

    current_ts = int(time.time())
    with pytest.raises(RuntimeError, match="simulated agent failure"):
        await worker.process_update(
            _update(chat_id=42, text="boom"), now=current_ts
        )

    refreshed = await conversations.get(conn, existing.id)
    assert refreshed is not None
    assert refreshed.updated_at == current_ts
    assert sends == []
