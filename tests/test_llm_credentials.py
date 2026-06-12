import pytest

from hermes.crypto import EncryptedBlob
from hermes.repository import llm_credentials as repo

pytestmark = pytest.mark.asyncio


async def test_create_api_key_persists_ciphertext(conn) -> None:
    blob = EncryptedBlob(iv="aa" * 12, tag="bb" * 16, data="cc" * 30)
    cred = await repo.create_api_key(
        conn,
        user_id=1,
        provider="openai",
        display_name="Martin OpenAI",
        base_url=None,
        ciphertext=blob,
    )
    assert cred.id > 0
    assert cred.provider == "openai"
    assert cred.mode == "api_key"
    assert cred.is_active is False
    assert cred.api_key_iv == blob.iv
    assert cred.api_key_tag == blob.tag
    assert cred.api_key_data == blob.data
    assert cred.oauth_status is None
    assert cred.created_at > 0
    assert cred.updated_at == cred.created_at


async def test_list_all_orders_by_created_desc(conn) -> None:
    blob = EncryptedBlob(iv="01" * 12, tag="02" * 16, data="03" * 16)
    a = await repo.create_api_key(
        conn,
        user_id=1,
        provider="anthropic",
        display_name="A",
        base_url=None,
        ciphertext=blob,
        ts=1000,
    )
    b = await repo.create_api_key(
        conn,
        user_id=1,
        provider="openai",
        display_name="B",
        base_url="https://api.openai.com",
        ciphertext=blob,
        ts=2000,
    )
    rows = await repo.list_all(conn, user_id=1)
    assert [r.id for r in rows] == [b.id, a.id]


async def test_delete_removes_row(conn) -> None:
    blob = EncryptedBlob(iv="01" * 12, tag="02" * 16, data="03" * 16)
    cred = await repo.create_api_key(
        conn,
        user_id=1,
        provider="openai",
        display_name="tmp",
        base_url=None,
        ciphertext=blob,
    )
    assert await repo.delete(conn, cred.id, user_id=1) is True
    assert await repo.get(conn, cred.id, user_id=1) is None
    # Idempotency: second delete is a no-op.
    assert await repo.delete(conn, cred.id, user_id=1) is False


async def test_activate_clears_other_active_rows(conn) -> None:
    blob = EncryptedBlob(iv="01" * 12, tag="02" * 16, data="03" * 16)
    a = await repo.create_api_key(
        conn, user_id=1, provider="openai", display_name="A", base_url=None, ciphertext=blob
    )
    b = await repo.create_api_key(
        conn, user_id=1, provider="anthropic", display_name="B", base_url=None, ciphertext=blob
    )
    await repo.activate(conn, a.id, user_id=1)
    active = await repo.get_active(conn, user_id=1)
    assert active is not None and active.id == a.id

    # Activating B atomically deactivates A — otherwise the partial unique
    # index on `llm_credentials` would reject the second activate with
    # IntegrityError.
    await repo.activate(conn, b.id, user_id=1)
    active2 = await repo.get_active(conn, user_id=1)
    assert active2 is not None and active2.id == b.id
    a_after = await repo.get(conn, a.id, user_id=1)
    assert a_after is not None and a_after.is_active is False


async def test_get_active_returns_none_when_no_active(conn) -> None:
    blob = EncryptedBlob(iv="01" * 12, tag="02" * 16, data="03" * 16)
    await repo.create_api_key(
        conn, user_id=1, provider="openai", display_name="A", base_url=None, ciphertext=blob
    )
    # Created but not activated → nothing active.
    assert await repo.get_active(conn, user_id=1) is None


async def test_create_oauth_pending_row(conn) -> None:
    cred = await repo.create_oauth_pending(
        conn,
        user_id=1,
        display_name="Martin Claude Max",
    )
    assert cred.mode == "oauth_claude"
    assert cred.provider == "anthropic"
    assert cred.oauth_status == "pending"
    assert cred.api_key_iv is None
    assert cred.is_active is False


async def test_update_oauth_authorized_persists_ciphertext(conn) -> None:
    cred = await repo.create_oauth_pending(conn, user_id=1, display_name="x")
    blob = EncryptedBlob(iv="ee" * 12, tag="ff" * 16, data="11" * 100)
    updated = await repo.update_oauth_authorized(
        conn,
        user_id=1,
        cred_id=cred.id,
        ciphertext=blob,
        authorized_at=12345,
    )
    assert updated is not None
    assert updated.oauth_status == "authorized"
    assert updated.oauth_authorized_at == 12345
    assert updated.oauth_iv == blob.iv
    assert updated.oauth_tag == blob.tag
    assert updated.oauth_data == blob.data
    assert updated.updated_at >= cred.updated_at
