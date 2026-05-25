"""HTTP-level tests for /api/messenger. signal-cli-rest-api and
api.telegram.org are both mocked via httpx.MockTransport hooked into
app.state.signal_http / app.state.external_http respectively — no live
network calls."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


class _FakeSignalCli:
    """In-memory stand-in for signal-cli-rest-api. Tests append phone
    numbers to `linked_numbers` to simulate a successful link-as-secondary
    flow; the mock transport then returns them from /v1/accounts."""

    def __init__(self) -> None:
        self.linked_numbers: list[str] = []
        self.qr_png = b"\x89PNG\r\n\x1a\nfake-qr"
        self.link_calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/qrcodelink":
            self.link_calls.append(request.url.params.get("device_name", ""))
            return httpx.Response(200, content=self.qr_png, headers={"content-type": "image/png"})
        if path == "/v1/accounts":
            return httpx.Response(200, json=self.linked_numbers)
        return httpx.Response(404, json={"error": "not_found"})


class _FakeTelegramApi:
    """In-memory stand-in for api.telegram.org. Tests configure
    `getme_response` to control whether the bot-token validation passes
    or fails — getUpdates always returns an empty batch so a worker
    rebuild triggered in-test doesn't busy-loop."""

    def __init__(self, *, username: str = "holzi_bot") -> None:
        # Default: any bot token resolves to @holzi_bot
        self.getme_response: dict[str, Any] = {
            "ok": True,
            "result": {"id": 42, "is_bot": True, "username": username},
        }
        self.getme_calls: list[str] = []
        self.send_calls: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # /bot<TOKEN>/<method>
        if "/getMe" in path:
            token = path.split("/bot", 1)[1].split("/getMe", 1)[0]
            self.getme_calls.append(token)
            return httpx.Response(200, json=self.getme_response)
        if "/getUpdates" in path:
            return httpx.Response(200, json={"ok": True, "result": []})
        if "/sendMessage" in path:
            self.send_calls.append(json.loads(request.content))
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1}}
            )
        return httpx.Response(404, json={"ok": False, "description": "not found"})


@pytest.fixture
async def fake_signal_cli():
    return _FakeSignalCli()


@pytest.fixture
async def fake_telegram_api():
    return _FakeTelegramApi()


@pytest.fixture
async def client(
    fake_signal_cli: _FakeSignalCli, fake_telegram_api: _FakeTelegramApi
):
    """Mount the FastAPI app with both upstream HTTP clients patched to
    route into in-memory fakes. LifespanManager runs the real lifespan,
    then we swap the clients before the first request."""
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        await app.state.signal_http.aclose()
        app.state.signal_http = httpx.AsyncClient(
            transport=httpx.MockTransport(fake_signal_cli.handle),
            base_url="http://signal-cli-fake",
            timeout=10.0,
        )
        # The Telegram create-route + worker both call api.telegram.org
        # through app.state.external_http — swap it for a MockTransport
        # so the test never leaves the process.
        await app.state.external_http.aclose()
        app.state.external_http = httpx.AsyncClient(
            transport=httpx.MockTransport(fake_telegram_api.handle),
            timeout=10.0,
        )
        try:
            yield c
        finally:
            await app.state.signal_http.aclose()
            await app.state.external_http.aclose()


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/messenger/accounts")
    assert response.status_code == 401


async def test_list_empty_when_no_rows(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/messenger/accounts", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"accounts": []}


async def test_signal_link_start_returns_png(
    client: httpx.AsyncClient, fake_signal_cli: _FakeSignalCli
) -> None:
    response = await client.post(
        "/api/messenger/accounts/signal/link/start", headers=AUTH
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == fake_signal_cli.qr_png
    # Device name should be unique per call (here: only one call).
    assert len(fake_signal_cli.link_calls) == 1
    assert fake_signal_cli.link_calls[0].startswith("holzi-")


async def test_signal_link_poll_materialises_new_numbers(
    client: httpx.AsyncClient, fake_signal_cli: _FakeSignalCli
) -> None:
    # Simulate the user scanning the QR and the primary phone confirming
    # the link — signal-cli now reports the number under /v1/accounts.
    fake_signal_cli.linked_numbers.append("+491701234567")

    response = await client.post(
        "/api/messenger/accounts/signal/link/poll", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["accounts"]) == 1
    new = body["accounts"][0]
    assert new["provider"] == "signal"
    assert new["phone_number"] == "+491701234567"
    assert new["is_active"] is False

    # Re-polling without any new number is a no-op (idempotent).
    response = await client.post(
        "/api/messenger/accounts/signal/link/poll", headers=AUTH
    )
    assert len(response.json()["accounts"]) == 1


async def test_activate_then_deactivate_via_delete(
    client: httpx.AsyncClient, fake_signal_cli: _FakeSignalCli
) -> None:
    fake_signal_cli.linked_numbers.append("+491701234567")
    await client.post("/api/messenger/accounts/signal/link/poll", headers=AUTH)
    accounts = (
        await client.get("/api/messenger/accounts", headers=AUTH)
    ).json()["accounts"]
    account_id = accounts[0]["id"]

    response = await client.patch(
        f"/api/messenger/accounts/{account_id}/activate", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["account"]["is_active"] is True

    response = await client.delete(
        f"/api/messenger/accounts/{account_id}", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True}

    accounts = (
        await client.get("/api/messenger/accounts", headers=AUTH)
    ).json()["accounts"]
    assert accounts == []


async def test_activate_unknown_id_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.patch(
        "/api/messenger/accounts/99999/activate", headers=AUTH
    )
    assert response.status_code == 404


async def test_response_omits_bot_token_ciphertext(
    client: httpx.AsyncClient,
) -> None:
    """The API surface must never echo back the AES-GCM token blob even
    once telegram support lands — assert the response shape stays tight."""
    from hermes.repository import messenger as repo

    account = await repo.create_telegram(
        app.state.db,
        bot_username="holzi_bot",
        bot_token_iv="aa" * 12,
        bot_token_tag="bb" * 16,
        bot_token_data="cc" * 24,
    )

    response = await client.get("/api/messenger/accounts", headers=AUTH)
    body = response.json()
    matching = [a for a in body["accounts"] if a["id"] == account.id]
    assert len(matching) == 1
    forbidden = {"bot_token_iv", "bot_token_tag", "bot_token_data"}
    assert forbidden.isdisjoint(matching[0].keys())


async def test_create_telegram_validates_token_and_persists_encrypted(
    client: httpx.AsyncClient, fake_telegram_api: _FakeTelegramApi
) -> None:
    response = await client.post(
        "/api/messenger/accounts/telegram",
        json={"bot_token": "12345:my-secret-token"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account"]["provider"] == "telegram"
    assert body["account"]["bot_username"] == "holzi_bot"
    assert body["account"]["is_active"] is False
    assert body["account"]["allowed_chat_ids"] is None
    forbidden = {"bot_token_iv", "bot_token_tag", "bot_token_data"}
    assert forbidden.isdisjoint(body["account"].keys())

    # The route MUST have called getMe with the user-supplied token.
    assert fake_telegram_api.getme_calls == ["12345:my-secret-token"]

    # Persisted ciphertext must round-trip back to the original token —
    # otherwise the worker can't talk to Telegram.
    from hermes.crypto import EncryptedBlob
    from hermes.repository import messenger as repo

    stored = await repo.get_active(app.state.db, "telegram")
    # get_active returns the active row only; we just created an inactive
    # one, so look it up by id from the API response instead.
    stored = await repo.get_by_id(app.state.db, body["account"]["id"])
    assert stored is not None
    decrypted = app.state.encryptor.decrypt(
        EncryptedBlob(
            iv=stored.bot_token_iv,
            tag=stored.bot_token_tag,
            data=stored.bot_token_data,
        )
    )
    assert decrypted == "12345:my-secret-token"


async def test_create_telegram_rejects_bad_token_with_400(
    client: httpx.AsyncClient, fake_telegram_api: _FakeTelegramApi
) -> None:
    fake_telegram_api.getme_response = {
        "ok": False,
        "error_code": 401,
        "description": "Unauthorized",
    }
    response = await client.post(
        "/api/messenger/accounts/telegram",
        json={"bot_token": "00:bad-token"},
        headers=AUTH,
    )
    assert response.status_code == 400
    # Nothing should have been persisted.
    accounts = (await client.get("/api/messenger/accounts", headers=AUTH)).json()
    assert accounts["accounts"] == []


async def test_create_telegram_persists_allowed_chat_ids_as_json_array(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/messenger/accounts/telegram",
        json={
            "bot_token": "12345:secret",
            "allowed_chat_ids": [42, 99],
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    # Round-trips as a JSON-encoded string in the column.
    body = response.json()
    assert json.loads(body["account"]["allowed_chat_ids"]) == [42, 99]


async def test_activating_telegram_account_starts_worker(
    client: httpx.AsyncClient,
) -> None:
    create = await client.post(
        "/api/messenger/accounts/telegram",
        json={"bot_token": "12345:secret"},
        headers=AUTH,
    )
    account_id = create.json()["account"]["id"]

    assert getattr(app.state, "telegram_worker", None) is None

    activate = await client.patch(
        f"/api/messenger/accounts/{account_id}/activate", headers=AUTH
    )
    assert activate.status_code == 200

    assert app.state.telegram_worker is not None
    assert app.state.telegram_bot_username == "holzi_bot"

    # Tear down so the test fixture's cleanup doesn't fight a running worker.
    delete = await client.delete(
        f"/api/messenger/accounts/{account_id}", headers=AUTH
    )
    assert delete.status_code == 200
    assert getattr(app.state, "telegram_worker", None) is None
