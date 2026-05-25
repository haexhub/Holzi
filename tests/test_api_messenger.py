"""HTTP-level tests for /api/messenger. signal-cli-rest-api is mocked via
a custom httpx transport hooked into app.state.signal_http."""
from __future__ import annotations

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
        if path.startswith("/v1/qrcodelink/"):
            self.link_calls.append(path.removeprefix("/v1/qrcodelink/"))
            return httpx.Response(200, content=self.qr_png, headers={"content-type": "image/png"})
        if path == "/v1/accounts":
            return httpx.Response(200, json=self.linked_numbers)
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
async def fake_signal_cli():
    return _FakeSignalCli()


@pytest.fixture
async def client(fake_signal_cli: _FakeSignalCli):
    """Mount the FastAPI app with the signal-cli httpx client patched to
    route into the in-memory fake. LifespanManager runs the real
    lifespan, then we swap the http client out before the first request."""
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        # Replace the lifespan-built signal_http with one that talks to
        # the fake. The worker (none active in tests) is unaffected.
        await app.state.signal_http.aclose()
        app.state.signal_http = httpx.AsyncClient(
            transport=httpx.MockTransport(fake_signal_cli.handle),
            base_url="http://signal-cli-fake",
            timeout=10.0,
        )
        try:
            yield c
        finally:
            await app.state.signal_http.aclose()


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
