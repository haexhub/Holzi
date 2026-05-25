"""Thin async wrapper around the Telegram Bot HTTP API.

The bot token sits in the URL path (`/bot<token>/<method>`). The client
takes an `httpx.AsyncClient` (any base_url — paths are built fully
qualified) plus the token and the API base URL. That lets the lifecycle
reuse the shared `app.state.external_http` instead of standing up a
dedicated client per worker — tests then need to mock only one place.
"""
from __future__ import annotations

from typing import Any

import httpx

DEFAULT_API_BASE = "https://api.telegram.org"


class TelegramAuthError(RuntimeError):
    """getMe / getUpdates returned 401 — the bot token is invalid or
    revoked. Surfaced separately so the create-account route can map it
    to a clean 400 instead of a generic 502."""


async def fetch_bot_username(
    http: httpx.AsyncClient, bot_token: str, *, api_base: str = DEFAULT_API_BASE
) -> str:
    """Call `getMe` and return the bot's @username.

    Used by the create-telegram-account route to validate a freshly
    pasted token before persisting it. Raises `TelegramAuthError` on 401
    (bad token), `RuntimeError` on any other API failure.
    """
    response = await http.get(f"{api_base}/bot{bot_token}/getMe", timeout=15.0)
    payload = _parse_response(response)
    result = payload.get("result") or {}
    username = result.get("username")
    if not isinstance(username, str) or not username:
        # Bots without a username can't be DM'd by users, so accepting one
        # would let the user persist a useless row. Reject early.
        raise RuntimeError("Telegram getMe returned no username")
    return username


class TelegramClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        bot_token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self.http = http
        self.bot_token = bot_token
        self.api_base = api_base

    async def get_updates(
        self, *, offset: int, timeout: int = 25
    ) -> list[dict[str, Any]]:
        """Long-poll for new updates past `offset`.

        Telegram's recommended pattern: pass `offset = last_update_id + 1`
        to ack everything seen so far. `timeout` is the long-poll budget
        in seconds — keep it below the worker's outer cancellation
        timeout so a stop request doesn't have to wait the full window.
        """
        response = await self.http.get(
            f"{self.api_base}/bot{self.bot_token}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            # +5s grace so the http client doesn't time out before the
            # long-poll naturally returns from the Telegram side.
            timeout=timeout + 5,
        )
        payload = _parse_response(response)
        result = payload.get("result")
        if not isinstance(result, list):
            raise RuntimeError(
                f"Telegram getUpdates returned non-list result: {type(result).__name__}"
            )
        return result

    async def send_message(self, *, chat_id: int, text: str) -> None:
        response = await self.http.post(
            f"{self.api_base}/bot{self.bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15.0,
        )
        _parse_response(response)


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    """Common Telegram envelope handling.

    Telegram returns HTTP 200 even on logical failures and signals them
    via `ok: false` + `error_code` + `description`. Map 401 to
    `TelegramAuthError` so callers can distinguish a bad token from a
    transient outage.
    """
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Telegram returned non-dict envelope: {type(payload).__name__}"
        )
    if not payload.get("ok"):
        code = payload.get("error_code")
        desc = payload.get("description") or "unknown error"
        if code == 401:
            raise TelegramAuthError(desc)
        raise RuntimeError(f"Telegram API error {code}: {desc}")
    return payload
