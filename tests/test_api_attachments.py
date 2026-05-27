import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes import attachments as attachments_mod
from hermes.main import app
from hermes.repository import attachments, conversations
from tests.test_api_chat import _assistant_oneshot, _install_upstream_responses

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


async def _new_web_conversation() -> int:
    convo = await conversations.create(app.state.db, channel="web")
    return convo.id


def _upload(client, conv_id, *, name="notes.txt", data=b"hello", ctype="text/plain"):
    return client.post(
        f"/api/conversations/{conv_id}/attachments",
        headers=AUTH,
        files={"file": (name, data, ctype)},
    )


async def test_upload_requires_auth(client: httpx.AsyncClient) -> None:
    conv_id = await _new_web_conversation()
    resp = await client.post(
        f"/api/conversations/{conv_id}/attachments",
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 401


async def test_upload_text_stores_metadata_and_file(
    client: httpx.AsyncClient,
) -> None:
    conv_id = await _new_web_conversation()
    resp = await _upload(client, conv_id, data=b"line one\nline two")
    assert resp.status_code == 201
    body = resp.json()
    assert body["filename"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert body["size"] == len(b"line one\nline two")
    assert body["conversation_id"] == conv_id
    assert body["message_id"] is None

    # Persisted and on disk under the conversation scratch dir.
    att = await attachments.get(app.state.db, body["id"])
    assert att is not None
    path = attachments_mod.file_path(att)
    assert path.read_bytes() == b"line one\nline two"


async def test_upload_unknown_conversation_404(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, 999_999)
    assert resp.status_code == 404


async def test_upload_unsupported_type_415(client: httpx.AsyncClient) -> None:
    conv_id = await _new_web_conversation()
    resp = await _upload(
        client, conv_id, name="a.bin", data=b"\x00\x01", ctype="application/octet-stream"
    )
    assert resp.status_code == 415


async def test_upload_oversized_413(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(attachments_mod, "MAX_ATTACHMENT_BYTES", 8)
    conv_id = await _new_web_conversation()
    resp = await _upload(client, conv_id, data=b"123456789")  # 9 > 8
    assert resp.status_code == 413


async def test_upload_image_allowed_metadata_only(
    client: httpx.AsyncClient,
) -> None:
    conv_id = await _new_web_conversation()
    resp = await _upload(client, conv_id, name="shot.png", data=b"\x89PNG", ctype="image/png")
    assert resp.status_code == 201
    assert resp.json()["content_type"] == "image/png"


async def test_upload_sanitizes_traversal_filename(
    client: httpx.AsyncClient,
) -> None:
    conv_id = await _new_web_conversation()
    resp = await _upload(client, conv_id, name="../../../etc/passwd", data=b"x")
    assert resp.status_code == 201
    att = await attachments.get(app.state.db, resp.json()["id"])
    assert att is not None
    # Display name reduced to a basename; on-disk name is an opaque token
    # inside the conversation's own attachments dir — no escape possible.
    assert att.filename == "passwd"
    path = attachments_mod.file_path(att)
    assert path.parent == attachments_mod.attachment_dir(conv_id)
    assert ".." not in att.storage_path


async def test_create_empty_conversation_with_derived_title(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/conversations",
        headers=AUTH,
        json={"message": "please read the attached log"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["channel"] == "web"
    assert body["title"] == "please read the attached log"
    # Created empty: detail has no messages yet.
    detail = await client.get(
        f"/api/conversations/{body['id']}", headers=AUTH
    )
    assert detail.json()["messages"] == []


async def test_create_conversation_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/conversations", json={})
    assert resp.status_code == 401


async def test_chat_links_attachments_and_detail_includes_metadata(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("ok")])
    conv_id = await _new_web_conversation()
    up = await _upload(client, conv_id, data=b"hello world")
    att_id = up.json()["id"]

    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={
            "message": "look at this",
            "conversation_id": conv_id,
            "attachment_ids": [att_id],
        },
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_bytes():
            pass

    # After reload the user message carries the attachment metadata.
    detail = await client.get(f"/api/conversations/{conv_id}", headers=AUTH)
    assert detail.status_code == 200
    user_msgs = [m for m in detail.json()["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    atts = user_msgs[0]["attachments"]
    assert len(atts) == 1
    assert atts[0]["id"] == att_id
    assert atts[0]["filename"] == "notes.txt"
    assert atts[0]["message_id"] == user_msgs[0]["id"]


async def test_chat_inlines_text_attachment_into_upstream_request(
    client: httpx.AsyncClient,
) -> None:
    seen = _install_upstream_responses([_assistant_oneshot("ok")])
    conv_id = await _new_web_conversation()
    up = await _upload(client, conv_id, data=b"SECRET_MARKER_42")
    att_id = up.json()["id"]

    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={
            "message": "summarise",
            "conversation_id": conv_id,
            "attachment_ids": [att_id],
        },
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    # The decoded file content reached the model inside the user turn, while
    # the stored message content stays clean.
    upstream_messages = seen[0]["messages"]
    user_turn = next(m for m in upstream_messages if m["role"] == "user")
    assert "SECRET_MARKER_42" in user_turn["content"]
    assert "summarise" in user_turn["content"]


async def test_chat_rejects_attachment_ids_without_conversation(
    client: httpx.AsyncClient,
) -> None:
    # attachment_ids can't pair with the implicit new-conversation path.
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hi", "attachment_ids": [1]},
    )
    assert resp.status_code == 400


async def test_chat_rejects_attachment_from_other_conversation(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("ok")])
    conv_a = await _new_web_conversation()
    conv_b = await _new_web_conversation()
    up = await _upload(client, conv_a)
    att_id = up.json()["id"]

    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={
            "message": "hi",
            "conversation_id": conv_b,
            "attachment_ids": [att_id],
        },
    )
    assert resp.status_code == 400


async def test_chat_rejects_already_sent_attachment(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [_assistant_oneshot("ok"), _assistant_oneshot("ok2")]
    )
    conv_id = await _new_web_conversation()
    up = await _upload(client, conv_id)
    att_id = up.json()["id"]

    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={"message": "first", "conversation_id": conv_id, "attachment_ids": [att_id]},
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    # Re-using the now-linked attachment id is rejected.
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "second", "conversation_id": conv_id, "attachment_ids": [att_id]},
    )
    assert resp.status_code == 400
