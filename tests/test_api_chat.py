import json

import httpx
import pytest

from hermes.main import app
from hermes.repository import conversations, messages
from tests._chat_sse import (
    assistant_oneshot as _assistant_oneshot,
)
from tests._chat_sse import (
    install_upstream_responses as _install_upstream_responses,
)
from tests._chat_sse import (
    parse_sse as _parse_sse,
)

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


async def test_api_chat_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 401


async def test_api_chat_creates_new_conversation_when_none_provided(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("hello back")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    event_names = [name for name, _ in events]
    assert event_names == ["session", "run", "text", "done"]

    session_evt = events[0][1]
    run_evt = events[1][1]
    text_evt = events[2][1]
    assert isinstance(session_evt["conversation_id"], int)
    assert isinstance(run_evt["run_id"], str) and run_evt["run_id"]
    assert text_evt["content"] == "hello back"

    conv_id = session_evt["conversation_id"]
    convo = await conversations.get(app.state.db, conv_id, user_id=1)
    assert convo is not None
    assert convo.channel == "web"
    assert convo.title == "hi"
    msgs = await messages.list_by_conversation(app.state.db, conv_id, user_id=1)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "hello back"),
    ]


async def test_api_chat_passes_composed_persona_channel_system_prompt(
    client: httpx.AsyncClient,
) -> None:
    """Plan 29-A end-to-end: /api/chat builds its system prompt via
    `get_effective_system_prompt("web", db)`. Two states verified:
    (a) fresh backfill → default Hermes + default web prompt,
    (b) customised via the public preferences endpoints → composition
    reflects the new persona + channel prompt."""
    from hermes.personas import (
        _BOOTSTRAP_HINT,
        BOOTSTRAP_SKILL_DESCRIPTION,
        BOOTSTRAP_SKILL_WHEN_TO_USE,
        CHANNEL_REGISTRY,
        DEFAULT_PERSONA_AGENTS,
        DEFAULT_PERSONA_IDENTITY,
        DEFAULT_PERSONA_SOUL,
    )
    from hermes.starter_skills import STARTER_SKILLS

    # Plan 37+38: the lifespan seeds the bootstrap-first-chat skill and the
    # 8 curated starter skills. Build the expected catalog index (alphabetical
    # by slug: bootstrap-first-chat comes first, then the 8 starter skills).
    _all_skills = [
        {
            "slug": "bootstrap-first-chat",
            "description": BOOTSTRAP_SKILL_DESCRIPTION,
            "when_to_use": BOOTSTRAP_SKILL_WHEN_TO_USE,
        },
        *STARTER_SKILLS,
    ]
    _all_skills.sort(key=lambda s: s["slug"])
    _skill_lines = ["## Available skills"]
    for s in _all_skills:
        line = f"- {s['slug']} — {s['description']}"
        if s.get("when_to_use"):
            line += f" (use when: {s['when_to_use']})"
        _skill_lines.append(line)
    _catalog_line = "\n".join(_skill_lines)

    # (a) Default composition. Backfill seeds all three fragments
    # (Plan 36), so the resolver emits Soul → Identity → Agents
    # sections before the channel prompt. Plan 37: catalog index is
    # included, bootstrap hint is appended because the fresh lifespan
    # seeds users with bootstrap_completed=0.
    seen = _install_upstream_responses([_assistant_oneshot("a")])
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "first"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
    sys_a = seen[0]["messages"][0]
    assert sys_a["role"] == "system"
    assert sys_a["content"] == (
        f"## Soul\n{DEFAULT_PERSONA_SOUL}\n\n"
        f"## Identity\n{DEFAULT_PERSONA_IDENTITY}\n\n"
        f"## Agents\n{DEFAULT_PERSONA_AGENTS}\n\n"
        f"{_catalog_line}\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}\n\n"
        f"{_BOOTSTRAP_HINT}"
    )

    # (b) Customised: new persona (identity-only) + custom channel prompt.
    new_persona = await client.post(
        "/api/personas",
        headers=AUTH,
        json={
            "name": "Reviewer",
            "identity": "Be merciless about types.",
        },
    )
    pid = new_persona.json()["id"]
    await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={
            "prompt": "Custom web prompt.",
            "default_persona_id": pid,
        },
    )

    seen = _install_upstream_responses([_assistant_oneshot("b")])
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "second"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
    sys_b = seen[0]["messages"][0]
    assert sys_b["role"] == "system"
    assert sys_b["content"] == (
        "## Identity\nBe merciless about types.\n\n"
        f"{_catalog_line}\n\n"
        f"Custom web prompt.\n\n"
        f"{_BOOTSTRAP_HINT}"
    )


async def test_api_chat_continues_existing_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    _install_upstream_responses([_assistant_oneshot("ack")])

    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={"message": "round 2", "conversation_id": convo.id},
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    assert events[0][1]["conversation_id"] == convo.id
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "round 2"),
        ("assistant", "ack"),
    ]


async def test_api_chat_unknown_conversation_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hi", "conversation_id": 99999},
    )
    assert response.status_code == 404


async def test_api_chat_streams_text_chunks_incrementally(
    client: httpx.AsyncClient,
) -> None:
    """Each upstream streaming delta should surface as its own SSE `text` event."""
    deltas = ["Hello", " ", "world"]
    body = b""
    for d in deltas:
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "claude-opus-4-7",
            "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}],
        }
        body += f"data: {json.dumps(chunk)}\n\n".encode()
    body += b"data: [DONE]\n\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        out = b""
        async for chunk in response.aiter_bytes():
            out += chunk

    text_events = [d["content"] for name, d in _parse_sse(out) if name == "text"]

    assert text_events == ["Hello", " ", "world"]


async def test_api_chat_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    """Channel semantics: /api/chat is web-only and must not write into
    conversations belonging to other channels (e.g. scheduled-task runs)."""
    task_convo = await conversations.create(app.state.db, user_id=1, channel="task", ts=1000)
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hijack", "conversation_id": task_convo.id},
    )
    assert response.status_code == 400
    # Nothing should have been written into the task conversation.
    msgs = await messages.list_by_conversation(app.state.db, task_convo.id, user_id=1)
    assert msgs == []


async def test_api_chat_missing_message_returns_400(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/chat", headers=AUTH, json={})
    assert response.status_code == 400


async def test_api_chat_passes_tool_catalog_to_agent(
    client: httpx.AsyncClient,
) -> None:
    seen = _install_upstream_responses([_assistant_oneshot("done")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    # The agent should have included Hermes tools in the upstream request.
    sent = seen[0]
    tools = sent.get("tools")
    assert tools, "agent should pass tools to upstream"
    names = {t["function"]["name"] for t in tools}
    assert "recall_memory" in names
    assert "save_note" in names
    assert "task_create" in names


@pytest.mark.real_persona_context
async def test_api_chat_uses_active_credential_model(
    client: httpx.AsyncClient,
) -> None:
    """The per-credential model wins over settings.model when active."""
    from hermes.repository import llm_credentials as repo

    seen = _install_upstream_responses([_assistant_oneshot("ok")])
    # Insert + activate a credential with a distinctive model id.
    ct = app.state.encryptor.encrypt("sk-x")
    row = await repo.create_api_key(
        app.state.db,
        user_id=1,
        provider="openai",
        display_name="t",
        base_url=None,
        ciphertext=ct,
    )
    await repo.set_model(app.state.db, row.id, "gpt-99-custom", user_id=1)
    await repo.activate(app.state.db, row.id, user_id=1)

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
        assert response.status_code == 200

    assert seen[0]["model"] == "gpt-99-custom"


async def test_api_chat_falls_back_to_settings_model_when_no_active(
    client: httpx.AsyncClient,
) -> None:
    seen = _install_upstream_responses([_assistant_oneshot("ok")])
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
        assert response.status_code == 200
    # No active credential → settings.model (the test config defaults).
    from hermes.config import settings
    assert seen[0]["model"] == settings.model


async def test_api_chat_classifies_upstream_unreachable(
    client: httpx.AsyncClient,
) -> None:
    """ConnectError from upstream → `error` event with code=upstream_unreachable
    and status_code=502 inside the SSE payload."""
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = dict(_parse_sse(body))
    err = events["error"]
    assert err["code"] == "upstream_unreachable"
    assert err["status_code"] == 502
    assert "no route to host" in err["message"]


async def test_api_chat_classifies_upstream_timeout(
    client: httpx.AsyncClient,
) -> None:
    """ReadTimeout from upstream → code=upstream_timeout, status_code=504."""
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream too slow")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    err = dict(_parse_sse(body))["error"]
    assert err["code"] == "upstream_timeout"
    assert err["status_code"] == 504


async def test_api_chat_classifies_upstream_http_error(
    client: httpx.AsyncClient,
) -> None:
    """Non-2xx response from upstream → HTTPStatusError → code=upstream_http_error,
    status_code=502 (with the upstream status surfaced in the message)."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b""),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    err = dict(_parse_sse(body))["error"]
    assert err["code"] == "upstream_http_error"
    assert err["status_code"] == 500
    assert "500" in err["message"]


async def test_api_chat_classifies_agent_error_for_truncated_stream(
    client: httpx.AsyncClient,
) -> None:
    """Upstream stream ends without [DONE] / finish_reason → run_agent raises
    RuntimeError → code=agent_error, status_code=500."""
    truncated = (
        b'data: {"choices":[{"index":0,"delta":{"content":"start"},'
        b'"finish_reason":null}]}\n\n'
        # No [DONE], no finish_reason — agent must refuse to persist.
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(truncated),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    err = dict(_parse_sse(body))["error"]
    assert err["code"] == "agent_error"
    assert err["status_code"] == 500


async def test_api_chat_emits_run_event_with_run_id(
    client: httpx.AsyncClient,
) -> None:
    """Every /api/chat stream must emit a `run` event with a non-empty run_id
    *before* the first content delta — frontends need it to wire up Stop."""
    _install_upstream_responses([_assistant_oneshot("hi back")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    # `run` must precede `text` so the frontend has the run_id by the
    # time the first chunk lands.
    assert "run" in names
    assert names.index("run") < names.index("text")
    run_evt = dict(events)["run"]
    assert isinstance(run_evt["run_id"], str)
    assert run_evt["run_id"]


async def test_api_chat_run_registry_is_cleaned_up_after_completion(
    client: httpx.AsyncClient,
) -> None:
    """The registry on app.state must not retain run_ids past the terminal
    SSE event — otherwise long-running deployments leak entries."""
    _install_upstream_responses([_assistant_oneshot("ok")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = dict(_parse_sse(body))
    run_id = events["run"]["run_id"]

    chat_runs = app.state.chat_runs
    assert run_id not in chat_runs


async def test_api_chat_can_continue_cline_conversation(
    client: httpx.AsyncClient,
) -> None:
    from hermes.repository import conversations as conv_repo

    cline_conv = await conv_repo.create(app.state.db, user_id=1, channel="cline")
    _install_upstream_responses([_assistant_oneshot("hello from upstream")])

    # Should NOT return 400 CONVERSATION_NOT_WEB — cline is an interactive channel
    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={"message": "hello from web", "conversation_id": cline_conv.id},
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    event_names = [name for name, _ in events]
    assert "done" in event_names


async def test_api_chat_task_channel_still_blocked(
    client: httpx.AsyncClient,
) -> None:
    from hermes.repository import conversations as conv_repo

    task_conv = await conv_repo.create(app.state.db, user_id=1, channel="task")
    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={
            "message": "sneak into task",
            "conversation_id": task_conv.id,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "CONVERSATION_NOT_WEB"
