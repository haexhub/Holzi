"""Persistence layer for `mcp_servers` (Plan 32).

Stores registered external MCP servers. Two transports today:

- `http`: StreamableHTTP. `url` is the full endpoint; an optional bearer
  token / API key lives in the AES-GCM `credentials_*` tripel.
- `stdio`: local subprocess. `command_argv` is the argv list, `env_json`
  is an opaque JSON map of environment variables that may carry secrets.

Secret-bearing fields never appear in `McpServer` (the public dataclass).
The repository exposes `env_keys` (names only) and the raw ciphertext
columns; the API layer projects further to drop the ciphertext.

A small helper `read_secrets` returns the *full* decrypted env-map +
plaintext credentials for the lifecycle manager — that path bypasses the
public dataclass on purpose.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import EncryptedBlob
from hermes.repository.models import McpServer
from hermes.schema import mcp_servers as t

# kebab-case slug: 2..32 chars, starts with [a-z0-9], may contain inner
# dashes, never trailing dash. Plan 32 schema note: shorter cap than
# workspaces because the slug appears in every `mcp:<slug>` source tag.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")

_LAST_ERROR_MAX = 256

Transport = Literal["http", "stdio"]
_TRANSPORTS = ("http", "stdio")


def validate_slug(slug: str) -> None:
    """Raise `ValueError` if `slug` isn't a valid MCP server name."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            "mcp server name must be kebab-case ASCII (a-z, 0-9, -), 2..32 "
            "chars, no leading/trailing dash"
        )


def _serialise_argv(argv: list[str] | None) -> str | None:
    if argv is None:
        return None
    if not isinstance(argv, list) or not all(isinstance(p, str) for p in argv):
        raise ValueError("command_argv must be a list of strings")
    if not argv:
        raise ValueError("command_argv must contain at least one entry")
    return json.dumps(argv)


def _serialise_env(env: dict[str, str] | None) -> str | None:
    if env is None:
        return None
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError("env must be a dict of str -> str")
    return json.dumps(env, sort_keys=True)


def _parse_argv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
        return parsed
    return None


def _parse_env(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return {str(k): str(v) for k, v in parsed.items()}
    return {}


def _row_to_server(row) -> McpServer:
    env_map = _parse_env(row.env_json)
    return McpServer(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        transport=row.transport,
        url=row.url,
        command_argv=_parse_argv(row.command_argv),
        env_keys=sorted(env_map.keys()),
        credentials_iv=row.credentials_iv,
        credentials_tag=row.credentials_tag,
        credentials_data=row.credentials_data,
        enabled=bool(row.enabled),
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_transport_shape(
    transport: str,
    *,
    url: str | None,
    command_argv: list[str] | None,
) -> None:
    if transport == "http":
        if not url:
            raise ValueError("http transport requires `url`")
        if command_argv is not None:
            raise ValueError("http transport must not set `command_argv`")
    elif transport == "stdio":
        if not command_argv:
            raise ValueError("stdio transport requires `command_argv`")
        if url is not None:
            raise ValueError("stdio transport must not set `url`")
    else:
        raise ValueError(f"unknown transport: {transport!r}")


async def create(
    engine: AsyncEngine,
    *,
    name: str,
    display_name: str,
    transport: Transport,
    url: str | None = None,
    command_argv: list[str] | None = None,
    env: dict[str, str] | None = None,
    ciphertext: EncryptedBlob | None = None,
    enabled: bool = True,
    ts: int | None = None,
) -> McpServer:
    """Insert a new MCP server row. Validates slug + transport shape.

    Raises `ValueError` on shape violations (caller maps to 400/422).
    Raises `sqlalchemy.exc.IntegrityError` on duplicate slug (caller maps
    to 409).
    """
    validate_slug(name)
    if transport not in _TRANSPORTS:
        raise ValueError(f"unknown transport: {transport!r}")
    if not display_name.strip():
        raise ValueError("display_name must not be empty")
    _validate_transport_shape(transport, url=url, command_argv=command_argv)

    argv_json = _serialise_argv(command_argv)
    env_json = _serialise_env(env)
    now = ts if ts is not None else int(time.time())
    stmt = (
        t.insert()
        .values(
            name=name,
            display_name=display_name.strip(),
            transport=transport,
            url=url,
            command_argv=argv_json,
            env_json=env_json,
            credentials_iv=ciphertext.iv if ciphertext else None,
            credentials_tag=ciphertext.tag if ciphertext else None,
            credentials_data=ciphertext.data if ciphertext else None,
            enabled=1 if enabled else 0,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        .returning(*t.c)
    )
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        row = result.first()
    if row is None:
        raise RuntimeError("insert into mcp_servers ... RETURNING returned no row")
    return _row_to_server(row)


async def get(engine: AsyncEngine, server_id: int) -> McpServer | None:
    async with engine.connect() as conn:
        result = await conn.execute(select(t).where(t.c.id == server_id))
        row = result.first()
    return _row_to_server(row) if row is not None else None


async def get_by_name(engine: AsyncEngine, name: str) -> McpServer | None:
    async with engine.connect() as conn:
        result = await conn.execute(select(t).where(t.c.name == name))
        row = result.first()
    return _row_to_server(row) if row is not None else None


async def list_all(engine: AsyncEngine) -> list[McpServer]:
    async with engine.connect() as conn:
        result = await conn.execute(select(t).order_by(asc(t.c.name)))
        rows = result.all()
    return [_row_to_server(r) for r in rows]


async def list_enabled(engine: AsyncEngine) -> list[McpServer]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t).where(t.c.enabled.is_(True)).order_by(asc(t.c.name))
        )
        rows = result.all()
    return [_row_to_server(r) for r in rows]


# Sentinel: the absence of a kwarg means "leave as-is"; passing `None`
# explicitly is the caller's intent (e.g. clear the field). We can't use
# `None` as the "unchanged" marker because it conflicts with "clear it".
_UNSET: Any = object()


async def update(
    engine: AsyncEngine,
    server_id: int,
    *,
    display_name: str | None = None,
    url: Any = _UNSET,
    command_argv: Any = _UNSET,
    env: Any = _UNSET,
    ciphertext: EncryptedBlob | None = None,
    clear_credentials: bool = False,
    enabled: bool | None = None,
    ts: int | None = None,
) -> McpServer | None:
    """Patch fields on an existing row. Returns None if no row matches.

    Credential update semantics:
      - `ciphertext` set → replace the credential.
      - `clear_credentials=True` → wipe iv/tag/data back to NULL.
      - neither → leave the credential as-is.
    """
    existing = await get(engine, server_id)
    if existing is None:
        return None

    values: dict[str, Any] = {}
    if display_name is not None:
        if not display_name.strip():
            raise ValueError("display_name must not be empty")
        values["display_name"] = display_name.strip()

    if url is not _UNSET:
        values["url"] = url
    if command_argv is not _UNSET:
        values["command_argv"] = _serialise_argv(command_argv)
    if env is not _UNSET:
        values["env_json"] = _serialise_env(env)
    if enabled is not None:
        values["enabled"] = 1 if enabled else 0

    if ciphertext is not None and clear_credentials:
        raise ValueError("cannot set ciphertext and clear_credentials together")
    if ciphertext is not None:
        values["credentials_iv"] = ciphertext.iv
        values["credentials_tag"] = ciphertext.tag
        values["credentials_data"] = ciphertext.data
    elif clear_credentials:
        values["credentials_iv"] = None
        values["credentials_tag"] = None
        values["credentials_data"] = None

    # Final transport-shape check: figure out the effective post-update
    # values for `transport`, `url`, `command_argv` and re-validate so we
    # never end up with e.g. an http row with a stdio argv hanging off it.
    effective_url = values.get("url", existing.url) if "url" in values else existing.url
    if "command_argv" in values:
        effective_argv = _parse_argv(values["command_argv"])
    else:
        effective_argv = existing.command_argv
    _validate_transport_shape(
        existing.transport, url=effective_url, command_argv=effective_argv
    )

    if not values:
        return existing  # nothing to write

    values["updated_at"] = ts if ts is not None else int(time.time())
    stmt = t.update().where(t.c.id == server_id).values(**values).returning(*t.c)
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        row = result.first()
    return _row_to_server(row) if row is not None else None


async def delete(engine: AsyncEngine, server_id: int) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(t.delete().where(t.c.id == server_id))
    return (result.rowcount or 0) > 0


async def set_last_error(
    engine: AsyncEngine, server_id: int, error: str | None
) -> None:
    """Persist (or clear) the last-start error for diagnostics surfaces.

    Truncates at 256 chars — the UI shows it inline, and pathological
    error messages from upstream MCP servers shouldn't blow up the row.
    """
    if error is not None:
        error = error[:_LAST_ERROR_MAX]
    async with engine.begin() as conn:
        await conn.execute(
            t.update().where(t.c.id == server_id).values(
                last_error=error, updated_at=int(time.time())
            )
        )


# ---------------------------------------------------------------------------
# Internal-only helper: hand decrypted secrets to the lifecycle manager.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpServerSecrets:
    """Decrypted env + credential for the lifecycle manager.

    Constructed by `read_secrets` and consumed inside the manager; never
    reaches a route layer (which only sees the public `McpServer`).
    """

    env: dict[str, str]
    credentials: str | None


async def read_secrets(
    engine: AsyncEngine,
    server_id: int,
    *,
    encryptor,
) -> McpServerSecrets | None:
    """Return the decrypted env + credentials for a server, or None when
    no row matches. Plaintext stays inside the manager; the public API
    layer never calls this."""
    async with engine.connect() as conn:
        result = await conn.execute(select(t).where(t.c.id == server_id))
        row = result.first()
    if row is None:
        return None
    env = _parse_env(row.env_json)
    creds: str | None = None
    if row.credentials_iv and row.credentials_tag and row.credentials_data:
        creds = encryptor.decrypt(
            EncryptedBlob(
                iv=row.credentials_iv,
                tag=row.credentials_tag,
                data=row.credentials_data,
            )
        )
    return McpServerSecrets(env=env, credentials=creds)
