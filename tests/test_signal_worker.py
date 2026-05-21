import json
import time
from typing import Any

import aiosqlite
import httpx

from hermes.repository import conversations, messages
from hermes.signal.client import SignalClient
from hermes.signal.worker import SignalWorker

SELF_NUMBER = "+491701234567"
OTHER_NUMBER = "+491709999999"


def _make_signal_client(send_capture: list[dict[str, Any]] | None = None) -> SignalClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/send":
            if send_capture is not None:
                send_capture.append(json.loads(request.content))
            return httpx.Response(201, json={"timestamp": 1700000000000})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake-signal")
    return SignalClient(http, SELF_NUMBER)


def _envelope(
    source: str,
    message: str = "hi",
    ts_ms: int = 1700000000000,
    *,
    data_message: bool = True,
) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "source": source,
        "sourceNumber": source,
        "timestamp": ts_ms,
    }
    if data_message:
        inner["dataMessage"] = {"message": message, "timestamp": ts_ms}
    return {"envelope": inner, "account": SELF_NUMBER}


def _echoing_agent_runner():
    """Stand-in for run_agent: echoes the last user message back and persists it."""

    async def runner(db: aiosqlite.Connection, conversation_id: int) -> str:
        msgs = await messages.list_by_conversation(db, conversation_id)
        last_user = next((m for m in reversed(msgs) if m.role == "user"), None)
        reply = f"agent says: {last_user.content if last_user else ''}"
        await messages.append(
            db, conversation_id=conversation_id, role="assistant", content=reply
        )
        return reply

    return runner


def _never_called_agent_runner():
    async def runner(db: aiosqlite.Connection, conversation_id: int) -> str:
        raise AssertionError("agent runner should not have been called")

    return runner


async def test_process_envelope_persists_note_to_self_and_replies_via_agent(
    conn: aiosqlite.Connection,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = SignalWorker(
        _make_signal_client(sends),
        conn,
        SELF_NUMBER,
        agent_runner=_echoing_agent_runner(),
    )

    await worker.process_envelope(_envelope(SELF_NUMBER, "hi"))

    convos = await conversations.list_by_channel(conn, "signal")
    assert len(convos) == 1
    msgs = await messages.list_by_conversation(conn, convos[0].id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "agent says: hi"),
    ]
    assert sends == [
        {
            "message": "agent says: hi",
            "number": SELF_NUMBER,
            "recipients": [SELF_NUMBER],
        }
    ]


async def test_process_envelope_ignores_messages_from_other_numbers(
    conn: aiosqlite.Connection,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = SignalWorker(
        _make_signal_client(sends),
        conn,
        SELF_NUMBER,
        agent_runner=_never_called_agent_runner(),
    )

    await worker.process_envelope(_envelope(OTHER_NUMBER, "hi"))

    assert await conversations.list_by_channel(conn, "signal") == []
    assert sends == []


async def test_process_envelope_ignores_envelopes_without_text(
    conn: aiosqlite.Connection,
) -> None:
    sends: list[dict[str, Any]] = []
    worker = SignalWorker(
        _make_signal_client(sends),
        conn,
        SELF_NUMBER,
        agent_runner=_never_called_agent_runner(),
    )

    await worker.process_envelope(_envelope(SELF_NUMBER, data_message=False))

    assert await conversations.list_by_channel(conn, "signal") == []
    assert sends == []


async def test_process_envelope_appends_to_existing_conversation_within_6h(
    conn: aiosqlite.Connection,
) -> None:
    one_hour_ago = int(time.time()) - 3600
    existing = await conversations.create(conn, channel="signal", ts=one_hour_ago)

    sends: list[dict[str, Any]] = []
    worker = SignalWorker(
        _make_signal_client(sends),
        conn,
        SELF_NUMBER,
        agent_runner=_echoing_agent_runner(),
    )

    await worker.process_envelope(_envelope(SELF_NUMBER, "follow-up"))

    convos = await conversations.list_by_channel(conn, "signal")
    assert len(convos) == 1
    assert convos[0].id == existing.id
    msgs = await messages.list_by_conversation(conn, existing.id)
    assert [m.content for m in msgs] == ["follow-up", "agent says: follow-up"]


async def test_process_envelope_creates_new_conversation_when_gap_exceeds_6h(
    conn: aiosqlite.Connection,
) -> None:
    seven_hours_ago = int(time.time()) - 7 * 3600
    existing = await conversations.create(conn, channel="signal", ts=seven_hours_ago)

    sends: list[dict[str, Any]] = []
    worker = SignalWorker(
        _make_signal_client(sends),
        conn,
        SELF_NUMBER,
        agent_runner=_echoing_agent_runner(),
    )

    await worker.process_envelope(_envelope(SELF_NUMBER, "new thread"))

    convos = await conversations.list_by_channel(conn, "signal")
    assert len(convos) == 2
    newest = convos[0]
    assert newest.id != existing.id
    msgs = await messages.list_by_conversation(conn, newest.id)
    assert [m.content for m in msgs] == ["new thread", "agent says: new thread"]
