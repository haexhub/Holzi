"""Plan 32-A: agent self-inventory + self-provisioning meta-tools.

Four meta-tools let the agent inspect and (with user approval) extend its
own tool / MCP surface, so "configure Holzi via Holzi" works conversationally
as well as via /settings/skills:

- ``list_tools``  — read-only mirror of ``app.state.tool_catalog`` (answers
  "what can you do?" / "do you have tool X?"). No approval.
- ``mcp_status``  — runtime status of every *configured* MCP server (merges
  the DB rows with the lifecycle manager's live handles). No approval.
- ``mcp_install`` — register + start a new MCP server. **Requires approval**;
  ``credentials`` / ``env`` values are secrets (see the redaction contract
  below).
- ``mcp_restart`` — restart a configured MCP server by name. No approval — it
  only re-launches existing config, mirroring the /settings/skills restart
  button.

All four carry ``source="builtin"`` (the ``Tool`` default; the catalog merge
in :mod:`hermes.tool_catalog` leaves it untouched), even though they operate
over MCP subjects.

Redaction contract (``mcp_install``): :func:`redact_mcp_install_params` is the
single source of truth for masking the secret parameters. It is wired onto the
tool as ``Tool.redact_arguments`` so the agent loop applies it to every
approval card, tool-call event, and persisted ``messages.meta_json`` record,
and the handler calls it directly for its ``meta_tool_invoked`` log line. The
raw plaintext only ever flows through the repo / manager write path.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.crypto import Encryptor
from hermes.logging import logger
from hermes.repository import mcp_servers as mcp_repo

if TYPE_CHECKING:  # pragma: no cover — avoid an import cycle at runtime
    from hermes.mcp_manager import McpServerManager

# Synchronous boot ceiling for install / restart. `start_server` already
# awaits the lifecycle task's terminal state internally, but a server that
# hangs during the handshake would block the agent forever without this.
_START_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Redaction contract
# ---------------------------------------------------------------------------


def redact_mcp_install_params(params: dict[str, Any]) -> dict[str, Any]:
    """Mask the secret-bearing ``mcp_install`` parameters for display / logs.

    ``credentials`` becomes ``"[redacted, N chars]"`` (so the length is
    visible without the value); every value in ``env`` becomes
    ``"[redacted]"`` while the keys stay visible (the user should see that
    ``GITHUB_TOKEN`` is being set without seeing its value). ``name``,
    ``display_name``, ``transport``, ``url`` and ``command_argv`` are shown
    verbatim. Non-dict / unexpected shapes are passed through untouched.
    """
    if not isinstance(params, dict):
        return params
    out = dict(params)
    creds = out.get("credentials")
    if isinstance(creds, str):
        out["credentials"] = f"[redacted, {len(creds)} chars]"
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {str(k): "[redacted]" for k in env}
    return out


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_meta_tools(
    *,
    db: AsyncEngine,
    mcp_manager: McpServerManager | None,
    encryptor: Encryptor | None,
    tool_catalog_provider: Callable[[], list[Tool]],
) -> list[Tool]:
    """Assemble the four meta-tools.

    ``tool_catalog_provider`` is a callable returning the *current*
    ``app.state.tool_catalog`` — read at ``list_tools`` call time so the
    answer reflects servers installed since boot, never a stale closure
    snapshot.
    """
    return [
        _list_tools(tool_catalog_provider),
        _mcp_status(db, mcp_manager),
        _mcp_install(db, mcp_manager, encryptor),
        _mcp_restart(db, mcp_manager),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _server_summary(row: Any, handle: Any) -> dict[str, Any]:
    """Public, secret-free projection of an installed server for the handler
    return value — mirrors what ``GET /api/mcp/servers`` exposes (``env_keys``
    only, never raw ``env`` values or credentials)."""
    return {
        "id": row.id,
        "name": row.name,
        "display_name": row.display_name,
        "transport": row.transport,
        "url": row.url,
        "command_argv": row.command_argv,
        "env_keys": row.env_keys,
        "enabled": row.enabled,
        "status": handle.status if handle is not None else "unknown",
    }


async def _cleanup_failed_install(
    mcp_manager: McpServerManager, db: AsyncEngine, server_id: int
) -> None:
    """Best-effort teardown so a failed install leaves no zombie row. Cleanup
    failures are logged rather than raised — the caller still surfaces the
    original startup error — but a stranded row/lifecycle task won't disappear
    silently; the warning lands in /settings/logs for follow-up."""
    try:
        await mcp_manager.stop_server(server_id)
    except Exception as exc:  # noqa: BLE001 — log, never mask the startup error
        logger.warning(
            "mcp_install_cleanup_stop_failed", server_id=server_id, error=str(exc)
        )
    try:
        await mcp_repo.delete(db, server_id)
    except Exception as exc:  # noqa: BLE001 — log, never mask the startup error
        logger.warning(
            "mcp_install_cleanup_delete_failed", server_id=server_id, error=str(exc)
        )


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


def _list_tools(provider: Callable[[], list[Tool]]) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        raw_filter = args.get("source_filter")
        source_filter = str(raw_filter).strip() if raw_filter else None
        items = []
        for t in provider() or []:
            if source_filter and t.source != source_filter:
                continue
            items.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "source": t.source,
                    "requires_approval": t.requires_approval,
                }
            )
        return json.dumps({"tools": items})

    return Tool(
        name="list_tools",
        description=(
            "List all tools currently available to this agent, grouped by "
            "source (built-in vs. mcp:<server-name>). Use when the user asks "
            "'what can you do?', 'do you have X?', or 'which tools are "
            "available?'."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "source_filter": {
                    "type": "string",
                    "description": (
                        "Optional exact source match: 'builtin', "
                        "'mcp:<name>', or omit for all."
                    ),
                }
            },
        },
        handler=handler,
        requires_approval=False,
    )


# ---------------------------------------------------------------------------
# mcp_status
# ---------------------------------------------------------------------------


def _mcp_status(db: AsyncEngine, mcp_manager: McpServerManager | None) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        rows = await mcp_repo.list_all(db)
        handles = {
            h.id: h
            for h in (mcp_manager.list_handles() if mcp_manager is not None else [])
        }
        servers = []
        for row in rows:
            handle = handles.get(row.id)
            status: str
            if handle is not None:
                status = handle.status
                tool_count = len(handle.tools)
                last_error = handle.last_error or row.last_error
                last_checked_at = handle.last_checked_at or None
            else:
                # No live handle: a disabled row (stop_server pops the
                # handle) or a row the manager hasn't booted.
                status = "disabled" if not row.enabled else "unknown"
                tool_count = 0
                last_error = row.last_error
                last_checked_at = None
            servers.append(
                {
                    "name": row.name,
                    "status": status,
                    "tool_count": tool_count,
                    "last_error": last_error,
                    "last_checked_at": last_checked_at,
                }
            )
        return json.dumps({"servers": servers})

    return Tool(
        name="mcp_status",
        description=(
            "Report the runtime status of all configured MCP servers (ready / "
            "starting / crashed / disabled / unknown), including last error. "
            "Use when the user asks why an MCP-backed tool is unavailable or "
            "whether server X is running."
        ),
        parameters_schema={"type": "object", "properties": {}},
        handler=handler,
        requires_approval=False,
    )


# ---------------------------------------------------------------------------
# mcp_install
# ---------------------------------------------------------------------------


def _mcp_install(
    db: AsyncEngine,
    mcp_manager: McpServerManager | None,
    encryptor: Encryptor | None,
) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        # Defense-in-depth log: redacted params + the structlog redaction
        # processor (Plan 27) both run before anything is persisted.
        logger.info(
            "meta_tool_invoked",
            tool="mcp_install",
            params=redact_mcp_install_params(args),
        )

        name = str(args.get("name", "")).strip()
        transport = str(args.get("transport", "")).strip()
        display_name = str(args.get("display_name") or name).strip()
        url = args.get("url")
        command_argv = args.get("command_argv")
        env = args.get("env")
        credentials = args.get("credentials")

        try:
            mcp_repo.validate_slug(name)
        except ValueError as exc:
            return _err(str(exc))
        if transport not in ("http", "stdio"):
            return _err("transport must be 'http' or 'stdio'")
        if transport == "http":
            if not url:
                return _err("http transport requires `url`")
            if command_argv:
                return _err("http transport must not set `command_argv`")
            if env:
                return _err("http transport must not set `env`")
        else:  # stdio
            if not command_argv:
                return _err("stdio transport requires `command_argv`")
            if url:
                return _err("stdio transport must not set `url`")

        if mcp_manager is None:
            return _err("MCP server manager not available")
        if credentials and encryptor is None:
            return _err("encryptor not available — cannot store credentials")

        ciphertext = (
            encryptor.encrypt(credentials)
            if credentials and encryptor is not None
            else None
        )

        try:
            row = await mcp_repo.create(
                db,
                name=name,
                display_name=display_name,
                transport=transport,  # type: ignore[arg-type]
                url=url,
                command_argv=command_argv,
                env=env,
                ciphertext=ciphertext,
                enabled=True,
            )
        except ValueError as exc:
            return _err(str(exc))
        except IntegrityError:
            return _err(f"an mcp server named {name!r} already exists")

        try:
            handle = await asyncio.wait_for(
                mcp_manager.start_server(row.id), timeout=_START_TIMEOUT_S
            )
        except TimeoutError:
            # wait_for cancels the start_server coroutine (blocked on the
            # handle's ready-event), but the detached lifecycle task keeps
            # running — _cleanup_failed_install's stop_server sets its
            # stop-event and awaits it, so nothing is left dangling.
            await _cleanup_failed_install(mcp_manager, db, row.id)
            return _err(
                "server startup timed out (15s) — try mcp_restart or check "
                "/settings/skills"
            )
        except Exception as exc:  # noqa: BLE001 — surface as error JSON
            await _cleanup_failed_install(mcp_manager, db, row.id)
            return _err(f"{type(exc).__name__}: {exc}")

        if handle.status != "ready":
            error = handle.last_error or "server failed to reach ready state"
            await _cleanup_failed_install(mcp_manager, db, row.id)
            return _err(error)

        return json.dumps(
            {
                "success": True,
                "server": _server_summary(row, handle),
                "tools_added": [t.name for t in handle.tools],
            }
        )

    return Tool(
        name="mcp_install",
        description=(
            "Register and start a new MCP server. Requires user approval. Use "
            "when the user asks to add/install/connect an MCP server, "
            "providing transport ('http' or 'stdio'), URL (http) or command + "
            "args (stdio), and a short slug name."
        ),
        parameters_schema={
            "type": "object",
            "required": ["name", "transport"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "kebab-case slug, 2-32 chars",
                },
                "display_name": {"type": "string"},
                "transport": {"type": "string", "enum": ["http", "stdio"]},
                "url": {"type": "string", "description": "http only"},
                "command_argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "stdio only",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "credentials": {
                    "type": "string",
                    "description": "optional bearer token (http)",
                },
            },
        },
        handler=handler,
        requires_approval=True,
        risk_reason=(
            "Installs an external MCP server that will run with Holzi's user "
            "permissions and expose its tools to the agent."
        ),
        redact_arguments=redact_mcp_install_params,
    )


# ---------------------------------------------------------------------------
# mcp_restart
# ---------------------------------------------------------------------------


def _mcp_restart(db: AsyncEngine, mcp_manager: McpServerManager | None) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        logger.info("meta_tool_invoked", tool="mcp_restart", params={"name": name})
        if not name:
            return _err("`name` is required")
        if mcp_manager is None:
            return _err("MCP server manager not available")
        row = await mcp_repo.get_by_name(db, name)
        if row is None:
            return _err(f"no mcp server named {name!r}")
        try:
            handle = await asyncio.wait_for(
                mcp_manager.restart_server(row.id), timeout=_START_TIMEOUT_S
            )
        except TimeoutError:
            return _err("server restart timed out (15s) — check /settings/skills")
        except Exception as exc:  # noqa: BLE001 — surface as error JSON
            return _err(f"{type(exc).__name__}: {exc}")

        result: dict[str, Any] = {
            "success": handle.status == "ready",
            "server": _server_summary(row, handle),
            "tools_added": [t.name for t in handle.tools],
        }
        if handle.status != "ready" and handle.last_error:
            result["error"] = handle.last_error
        return json.dumps(result)

    return Tool(
        name="mcp_restart",
        description=(
            "Restart a configured MCP server by name (e.g. after it crashed). "
            "Tools from that server are briefly unavailable while it restarts."
        ),
        parameters_schema={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        handler=handler,
        requires_approval=False,
    )


# Re-export the handler type for callers that want to annotate.
_Handler = Callable[[dict[str, Any]], Awaitable[str]]
