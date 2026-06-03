"""Tests for the `mcp_servers` repository (Plan 32).

Slug validation, transport-specific constraints (http needs `url`, stdio
needs `command_argv`), UNIQUE name → IntegrityError, env-json + encrypted
credentials roundtrip, and the `set_last_error` lifecycle hook.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from hermes.crypto import EncryptedBlob
from hermes.repository import mcp_servers as repo
from hermes.repository.mcp_servers import validate_slug

# `asyncio_mode = "auto"` in pyproject already runs async tests; no module
# pytestmark — that would also apply to the sync slug-validation tests.


# --- slug validation -------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["fs", "filesystem", "github-mcp", "a1", "01-prefixed", "abc-123-def"],
)
def test_validate_slug_accepts(slug: str) -> None:
    validate_slug(slug)  # no raise


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a",
        "-leading",
        "trailing-",
        "Has-Caps",
        "with_underscore",
        "with space",
        "a" * 33,
    ],
)
def test_validate_slug_rejects(slug: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(slug)


# --- create + get ----------------------------------------------------------


async def test_create_http_server_persists_url(conn) -> None:
    blob = EncryptedBlob(iv="aa" * 12, tag="bb" * 16, data="cc" * 30)
    row = await repo.create(
        conn,
        name="my-http",
        display_name="My HTTP",
        transport="http",
        url="https://mcp.example.com/sse",
        ciphertext=blob,
    )
    assert row.id > 0
    assert row.name == "my-http"
    assert row.transport == "http"
    assert row.url == "https://mcp.example.com/sse"
    assert row.command_argv is None
    assert row.env_keys == []
    assert row.credentials_iv == blob.iv
    assert row.enabled is True
    assert row.created_at > 0


async def test_create_stdio_server_persists_argv_and_env_keys(conn) -> None:
    row = await repo.create(
        conn,
        name="fs",
        display_name="Filesystem",
        transport="stdio",
        command_argv=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        env={"FOO": "bar", "TOKEN": "secret-value"},
    )
    assert row.transport == "stdio"
    assert row.command_argv == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert sorted(row.env_keys) == ["FOO", "TOKEN"]
    assert row.url is None
    assert row.credentials_iv is None


async def test_create_rejects_invalid_slug(conn) -> None:
    with pytest.raises(ValueError):
        await repo.create(
            conn,
            name="Bad Slug",
            display_name="x",
            transport="http",
            url="https://x",
        )


async def test_create_rejects_unknown_transport(conn) -> None:
    with pytest.raises(ValueError):
        await repo.create(
            conn,
            name="x",
            display_name="x",
            transport="websocket",  # type: ignore[arg-type]
            url="ws://x",
        )


async def test_create_http_requires_url(conn) -> None:
    with pytest.raises(ValueError):
        await repo.create(
            conn, name="x", display_name="x", transport="http"
        )


async def test_create_stdio_requires_argv(conn) -> None:
    with pytest.raises(ValueError):
        await repo.create(
            conn, name="x", display_name="x", transport="stdio"
        )


async def test_create_http_rejects_command_argv(conn) -> None:
    """HTTP transport refuses an argv field — the caller is mixing shapes."""
    with pytest.raises(ValueError):
        await repo.create(
            conn,
            name="x",
            display_name="x",
            transport="http",
            url="https://x",
            command_argv=["bad"],
        )


async def test_create_stdio_rejects_url(conn) -> None:
    with pytest.raises(ValueError):
        await repo.create(
            conn,
            name="x",
            display_name="x",
            transport="stdio",
            command_argv=["bin"],
            url="https://x",
        )


async def test_unique_name_raises(conn) -> None:
    await repo.create(
        conn, name="dup", display_name="A", transport="http", url="https://a"
    )
    with pytest.raises(IntegrityError):
        await repo.create(
            conn, name="dup", display_name="B", transport="http", url="https://b"
        )


# --- list + get ------------------------------------------------------------


async def test_list_all_and_list_enabled(conn) -> None:
    a = await repo.create(
        conn, name="ena", display_name="EnA", transport="http", url="https://a"
    )
    b = await repo.create(
        conn, name="enb", display_name="EnB", transport="http", url="https://b"
    )
    await repo.update(conn, b.id, enabled=False)
    all_rows = await repo.list_all(conn)
    enabled_rows = await repo.list_enabled(conn)
    assert {r.name for r in all_rows} == {"ena", "enb"}
    assert {r.name for r in enabled_rows} == {"ena"}
    assert (await repo.get(conn, a.id)).name == "ena"
    assert (await repo.get_by_name(conn, "ena")).id == a.id
    assert await repo.get(conn, 99999) is None
    assert await repo.get_by_name(conn, "missing") is None


# --- update ----------------------------------------------------------------


async def test_update_replaces_env_and_credentials(conn) -> None:
    blob_old = EncryptedBlob(iv="01" * 12, tag="02" * 16, data="03" * 16)
    row = await repo.create(
        conn,
        name="up",
        display_name="Up",
        transport="http",
        url="https://x",
        ciphertext=blob_old,
    )
    blob_new = EncryptedBlob(iv="99" * 12, tag="88" * 16, data="77" * 16)
    updated = await repo.update(
        conn,
        row.id,
        display_name="Up v2",
        url="https://y",
        ciphertext=blob_new,
        env={"NEW": "1"},
    )
    assert updated is not None
    assert updated.display_name == "Up v2"
    assert updated.url == "https://y"
    assert updated.credentials_iv == blob_new.iv
    assert updated.env_keys == ["NEW"]


async def test_update_clear_credentials(conn) -> None:
    blob = EncryptedBlob(iv="01" * 12, tag="02" * 16, data="03" * 16)
    row = await repo.create(
        conn,
        name="clr",
        display_name="Clr",
        transport="http",
        url="https://x",
        ciphertext=blob,
    )
    updated = await repo.update(conn, row.id, clear_credentials=True)
    assert updated is not None
    assert updated.credentials_iv is None
    assert updated.credentials_tag is None
    assert updated.credentials_data is None


async def test_update_unknown_returns_none(conn) -> None:
    assert await repo.update(conn, 9999, display_name="x") is None


# --- delete ----------------------------------------------------------------


async def test_delete_is_idempotent(conn) -> None:
    row = await repo.create(
        conn, name="del", display_name="Del", transport="http", url="https://x"
    )
    assert await repo.delete(conn, row.id) is True
    assert await repo.get(conn, row.id) is None
    assert await repo.delete(conn, row.id) is False


# --- set_last_error --------------------------------------------------------


async def test_set_last_error_truncates_and_clears(conn) -> None:
    row = await repo.create(
        conn, name="err", display_name="Err", transport="http", url="https://x"
    )
    long_err = "x" * 1000
    await repo.set_last_error(conn, row.id, long_err)
    updated = await repo.get(conn, row.id)
    assert updated is not None
    assert updated.last_error is not None
    assert len(updated.last_error) <= 256
    # Clearing the error sets it back to None.
    await repo.set_last_error(conn, row.id, None)
    cleared = await repo.get(conn, row.id)
    assert cleared is not None
    assert cleared.last_error is None
