"""WebSocket agent endpoint for the VS Code extension (Plan 41).

Protocol:
  Client → Server: start_session | message | tool_result | update_permission_mode
  Server → Client: stream_chunk | tool_call | stream_done | permission_mode_ack | error

Auth: Authorization: Bearer <token> header  OR  ?token=<token> query param.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hermes.agent import run_agent
from hermes.config import settings
from hermes.db import reset_current_user, set_current_user_token
from hermes.logging import logger
from hermes.personas import get_effective_system_prompt
from hermes.repository import conversations, messages
from hermes.tools.plan_wrapper import PLAN_MODE_READ_ONLY, make_plan_wrapper
from hermes.tools.remote import make_remote_tool

router = APIRouter()

VSCODE_CHANNEL = "vscode"


@dataclass
class WsSession:
    """State for a single VS Code WebSocket connection."""

    ws: WebSocket
    conversation_id: int
    permission_mode: str
    _pending: dict[str, asyncio.Future[str]] = field(default_factory=dict)

    async def wait_for_result(self, call_id: str, *, timeout: float = 30.0) -> str:
        """Block until the client returns a tool_result for call_id."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[call_id] = future
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            return f"error: tool call {call_id!r} timed out after {timeout:.0f}s"
        finally:
            self._pending.pop(call_id, None)

    def resolve_tool_result(
        self, call_id: str, result: str | None, error: str | None
    ) -> None:
        future = self._pending.get(call_id)
        if future is None or future.done():
            return
        if error:
            future.set_result(f"error: {error}")
        else:
            future.set_result(result or "")


def _build_tools(tool_names: list[str], session: WsSession) -> list:
    """Map tool names to Tool instances, respecting the current permission_mode."""
    tools = []
    for name in tool_names:
        if session.permission_mode == "plan" and name not in PLAN_MODE_READ_ONLY:
            tools.append(make_plan_wrapper(name))
        else:
            tools.append(make_remote_tool(name, session))
    return tools


async def _handle_inner_msg(session: WsSession, msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")
    if msg_type == "tool_result":
        session.resolve_tool_result(
            msg.get("id", ""), msg.get("result"), msg.get("error")
        )
    elif msg_type == "update_permission_mode":
        new_mode = msg.get("mode", "ask")
        session.permission_mode = new_mode
        await session.ws.send_json({"type": "permission_mode_ack", "mode": new_mode})


@router.websocket("/ws/agent")
async def ws_agent(ws: WebSocket, token: str | None = None) -> None:
    # --- Auth ----------------------------------------------------------------
    # Wave C1: resolve the bearer/query token through the SessionResolver (same
    # path as the HTTP middleware) instead of comparing against a static HMAC
    # secret. This closes the Task-4 WS divergence: a revoked/expired session
    # can no longer authenticate over the WebSocket.
    auth_header = ws.headers.get("authorization", "")
    provided = token
    if auth_header.startswith("Bearer "):
        provided = auth_header[len("Bearer "):]

    identity = None
    if provided:
        identity = await ws.app.state.identity_resolver.resolve(provided)
    if identity is None:
        await ws.accept()
        await ws.close(code=4001)
        return

    await ws.accept()

    db = ws.app.state.db
    upstream = ws.app.state.upstream

    # The HTTP bearer_auth_middleware is BaseHTTPMiddleware -- it does NOT
    # intercept WebSocket connections. Populate the ContextVar explicitly so
    # `tx_for_user(engine)` picks up the right user for the lifetime of this
    # connection. The try/finally guarantees the previous value is restored
    # even on disconnect or unhandled exception.
    ctx_token = set_current_user_token(identity.user_id)
    try:
        try:
            # --- start_session -----------------------------------------------
            init_msg = await ws.receive_json()
            if init_msg.get("type") != "start_session":
                await ws.close(code=1002, reason="expected start_session")
                return

            model: str = init_msg.get("model") or settings.model
            permission_mode: str = init_msg.get("permission_mode") or "ask"
            tool_names: list[str] = init_msg.get("tools") or []

            conv = await conversations.create(
                db, user_id=identity.user_id, channel=VSCODE_CHANNEL
            )
            system_prompt = await get_effective_system_prompt(
                VSCODE_CHANNEL, db, user_id=identity.user_id
            )
            session = WsSession(ws=ws, conversation_id=conv.id, permission_mode=permission_mode)

            logger.info(
                "ws_agent_session_start",
                conversation_id=conv.id,
                model=model,
                permission_mode=permission_mode,
                tools=tool_names,
            )

            # --- main message loop -------------------------------------------
            while True:
                msg = await ws.receive_json()
                msg_type = msg.get("type")

                if msg_type == "message":
                    user_content = _build_user_content(msg)
                    await messages.append(
                        db,
                        user_id=identity.user_id,
                        conversation_id=conv.id,
                        role="user",
                        content=user_content,
                    )

                    tools = _build_tools(tool_names, session) or None

                    async def on_chunk(delta: str) -> None:
                        await ws.send_json({"type": "stream_chunk", "delta": delta})

                    agent_task = asyncio.create_task(
                        run_agent(
                            upstream=upstream,
                            db=db,
                            user_id=identity.user_id,
                            conversation_id=conv.id,
                            system_prompt=system_prompt,
                            model=model,
                            tools=tools,
                            on_chunk=on_chunk,
                        )
                    )

                    await _service_agent_turn(session, agent_task)
                    await ws.send_json({"type": "stream_done"})

                else:
                    # tool_result + update_permission_mode (and any future inner
                    # message types) share one handler with the in-turn loop in
                    # _service_agent_turn so behaviour stays identical regardless
                    # of whether the client sends them mid-turn or between turns.
                    await _handle_inner_msg(session, msg)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("ws_agent_error", error=str(exc))
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "code": "internal", "message": str(exc)})
    finally:
        reset_current_user(ctx_token)


async def _service_agent_turn(
    session: WsSession, agent_task: asyncio.Task[str]
) -> None:
    """Run the agent task while concurrently receiving tool_result messages.

    When the agent needs a tool it sends tool_call and awaits a Future.
    This loop picks up the matching tool_result from the WebSocket and
    resolves that Future so the agent can continue.
    """
    pending_receive: asyncio.Task | None = None
    try:
        while not agent_task.done():
            if pending_receive is None:
                pending_receive = asyncio.create_task(session.ws.receive_json())

            done, _ = await asyncio.wait(
                {agent_task, pending_receive},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if pending_receive in done:
                inner_msg = pending_receive.result()
                pending_receive = None
                await _handle_inner_msg(session, inner_msg)
    finally:
        if pending_receive and not pending_receive.done():
            pending_receive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_receive

    agent_task.result()  # re-raise any exception from the agent


def _build_user_content(msg: dict[str, Any]) -> str:
    """Compose user message text, appending VS Code context if present."""
    content: str = msg.get("content") or ""
    ctx = msg.get("context")
    if not ctx:
        return content

    parts: list[str] = []
    if ctx.get("file"):
        parts.append(f"File: {ctx['file']}")
    if ctx.get("selection"):
        parts.append(f"Selection: {ctx['selection']}")
    if ctx.get("selected_text"):
        parts.append(f"Selected text:\n{ctx['selected_text']}")

    if not parts:
        return content
    return f"{content}\n\n" + "\n".join(parts)
