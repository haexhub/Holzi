import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import conversations, messages

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


async def test_api_conversations_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/conversations")
    assert response.status_code == 401


async def test_api_conversations_list_returns_recent_with_counts(
    client: httpx.AsyncClient,
) -> None:
    c1 = await conversations.create(app.state.db, channel="signal", ts=1000)
    c2 = await conversations.create(app.state.db, channel="web", ts=2000)
    await messages.append(
        app.state.db, conversation_id=c1.id, role="user", content="hi", ts=1001
    )
    await messages.append(
        app.state.db, conversation_id=c1.id, role="assistant", content="hi back", ts=1002
    )
    await messages.append(
        app.state.db, conversation_id=c2.id, role="user", content="hello", ts=2001
    )

    response = await client.get("/api/conversations", headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    # newest first
    assert [c["id"] for c in data] == [c2.id, c1.id]
    counts = {c["id"]: c["message_count"] for c in data}
    assert counts[c1.id] == 2
    assert counts[c2.id] == 1


async def test_api_conversations_list_filters_by_channel(
    client: httpx.AsyncClient,
) -> None:
    c1 = await conversations.create(app.state.db, channel="signal", ts=1000)
    await conversations.create(app.state.db, channel="web", ts=2000)

    response = await client.get("/api/conversations?channel=signal", headers=AUTH)
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()]
    assert ids == [c1.id]


async def test_api_conversation_detail_returns_messages_in_order(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="m1", ts=1001
    )
    await messages.append(
        app.state.db, conversation_id=convo.id, role="assistant", content="m2", ts=1002
    )

    response = await client.get(f"/api/conversations/{convo.id}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["conversation"]["id"] == convo.id
    assert body["conversation"]["channel"] == "web"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "m1"),
        ("assistant", "m2"),
    ]


async def test_api_conversation_detail_unknown_id_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/conversations/99999", headers=AUTH)
    assert response.status_code == 404


async def test_api_conversation_patch_renames_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(
        app.state.db, channel="web", title="old title", ts=1000
    )

    response = await client.patch(
        f"/api/conversations/{convo.id}",
        headers=AUTH,
        json={"title": "  new\n  title  "},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == convo.id
    assert body["title"] == "new title"
    stored = await conversations.get(app.state.db, convo.id)
    assert stored is not None
    assert stored.title == "new title"
    assert stored.updated_at >= convo.updated_at


async def test_api_conversation_patch_rejects_blank_title(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)

    response = await client.patch(
        f"/api/conversations/{convo.id}", headers=AUTH, json={"title": "   "}
    )

    assert response.status_code == 400


async def test_api_conversation_patch_unknown_id_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.patch(
        "/api/conversations/99999", headers=AUTH, json={"title": "missing"}
    )
    assert response.status_code == 404


async def test_api_conversation_delete_removes_conversation_and_messages(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="bye", ts=1001
    )

    response = await client.delete(f"/api/conversations/{convo.id}", headers=AUTH)

    assert response.status_code == 204
    assert await conversations.get(app.state.db, convo.id) is None
    assert await messages.list_by_conversation(app.state.db, convo.id) == []


async def test_api_conversation_delete_unknown_id_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/conversations/99999", headers=AUTH)
    assert response.status_code == 404


async def test_api_conversations_rejects_invalid_limit(
    client: httpx.AsyncClient,
) -> None:
    # Negative LIMIT disables limiting in SQLite — must be rejected at the API.
    for bad in ("-1", "0", "10000"):
        response = await client.get(f"/api/conversations?limit={bad}", headers=AUTH)
        assert response.status_code == 400, f"expected 400 for limit={bad}"


async def test_api_conversation_detail_rejects_invalid_limit(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    response = await client.get(
        f"/api/conversations/{convo.id}?limit=-5", headers=AUTH
    )
    assert response.status_code == 400
