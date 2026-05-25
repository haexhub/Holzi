"""TelegramClient is a thin wrapper around the Bot API. Tests use
httpx.MockTransport — no live api.telegram.org calls."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hermes.telegram.client import (
    TelegramAuthError,
    TelegramClient,
    fetch_bot_username,
)

BOT_TOKEN = "12345:test-token"


def _make_client(handler) -> TelegramClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.telegram.org")
    return TelegramClient(http, BOT_TOKEN)


@pytest.mark.asyncio
async def test_get_updates_returns_result_list() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 42,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 7, "type": "private"},
                            "text": "hi",
                        },
                    }
                ],
            },
        )

    client = _make_client(handler)
    updates = await client.get_updates(offset=10, timeout=25)

    assert len(updates) == 1
    assert updates[0]["update_id"] == 42
    # Token must be embedded in the path, never sent as a header or query param.
    assert f"/bot{BOT_TOKEN}/getUpdates" in captured["url"]
    assert captured["params"]["offset"] == "10"
    assert captured["params"]["timeout"] == "25"


@pytest.mark.asyncio
async def test_get_updates_raises_on_telegram_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    client = _make_client(handler)
    with pytest.raises(TelegramAuthError):
        await client.get_updates(offset=0)


@pytest.mark.asyncio
async def test_get_updates_non_auth_failure_raises_generic() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 502, "description": "Bad Gateway"}
        )

    client = _make_client(handler)
    with pytest.raises(RuntimeError, match="Bad Gateway"):
        await client.get_updates(offset=0)


@pytest.mark.asyncio
async def test_send_message_posts_chat_id_and_text() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    client = _make_client(handler)
    await client.send_message(chat_id=42, text="hello")

    assert f"/bot{BOT_TOKEN}/sendMessage" in captured["url"]
    assert captured["body"] == {"chat_id": 42, "text": "hello"}


@pytest.mark.asyncio
async def test_send_message_raises_on_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 400, "description": "chat not found"}
        )

    client = _make_client(handler)
    with pytest.raises(RuntimeError, match="chat not found"):
        await client.send_message(chat_id=42, text="hello")


@pytest.mark.asyncio
async def test_fetch_bot_username_returns_username() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "holzi_bot"}},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
    )
    username = await fetch_bot_username(http, BOT_TOKEN)

    assert username == "holzi_bot"
    assert f"/bot{BOT_TOKEN}/getMe" in captured["url"]


@pytest.mark.asyncio
async def test_fetch_bot_username_unauthorized_raises_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
    )
    with pytest.raises(TelegramAuthError):
        await fetch_bot_username(http, BOT_TOKEN)


@pytest.mark.asyncio
async def test_fetch_bot_username_missing_username_field_raises() -> None:
    """getMe returns `ok: true` but without a username for unconfigured bots."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"id": 1, "is_bot": True}}
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
    )
    with pytest.raises(RuntimeError, match="username"):
        await fetch_bot_username(http, BOT_TOKEN)
