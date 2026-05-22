from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Receive, Scope, Send

from hermes import __version__
from hermes.agent import run_agent
from hermes.auth import bearer_auth_middleware
from hermes.config import settings
from hermes.db import init_db
from hermes.logging import configure_logging, logger
from hermes.mcp_server import mcp_session_manager, tool_manifest
from hermes.routes.chat import router as chat_router
from hermes.signal.client import SignalClient
from hermes.signal.worker import SignalWorker
from hermes.tools.cross_channel import build_cross_channel_tools
from hermes.tools.memory import build_memory_tools

configure_logging()

SIGNAL_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Marko, reached via Signal "
    "Note-to-Self. Be concise — usually one to three short sentences. Match "
    "Marko's preference for terse, technical communication."
)


def build_upstream_client(llm_url: str, llm_api_key: str) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {llm_api_key}"} if llm_api_key else None
    return httpx.AsyncClient(base_url=llm_url, headers=headers, timeout=60.0)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "hermes_starting",
        version=__version__,
        db_path=settings.db_path,
        llm_url=settings.llm_url,
        signal_enabled=bool(settings.signal_number),
        model=settings.model,
    )
    app.state.db = await init_db(settings.db_path)
    app.state.upstream = build_upstream_client(settings.llm_url, settings.llm_api_key)
    app.state.signal_http = None
    app.state.signal_worker = None
    signal_client_for_tools: SignalClient | None = None

    if settings.signal_number:
        app.state.signal_http = httpx.AsyncClient(base_url=settings.signal_url, timeout=60.0)
        signal_client_for_tools = SignalClient(app.state.signal_http, settings.signal_number)

        async def signal_agent_runner(
            db: aiosqlite.Connection, conversation_id: int
        ) -> str:
            return await run_agent(
                upstream=app.state.upstream,
                db=db,
                conversation_id=conversation_id,
                system_prompt=SIGNAL_SYSTEM_PROMPT,
                model=settings.model,
            )

        app.state.signal_worker = SignalWorker(
            signal_client_for_tools,
            app.state.db,
            settings.signal_number,
            agent_runner=signal_agent_runner,
        )
        await app.state.signal_worker.start()

    # Tool catalog: shared between MCP exposure and (later) the internal agent.
    app.state.tool_catalog = build_memory_tools(app.state.db) + build_cross_channel_tools(
        app.state.db,
        signal_client_for_tools,
        settings.signal_number or None,
    )

    try:
        async with mcp_session_manager(app.state.tool_catalog) as mcp_mgr:
            app.state.mcp_manager = mcp_mgr
            yield
    finally:
        if app.state.signal_worker is not None:
            await app.state.signal_worker.stop()
        if app.state.signal_http is not None:
            await app.state.signal_http.aclose()
        await app.state.upstream.aclose()
        await app.state.db.close()
        logger.info("hermes_stopping")


app = FastAPI(title="Hermes", version=__version__, lifespan=lifespan)
app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth_middleware)
app.include_router(chat_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ping")
def ping() -> dict[str, bool]:
    return {"pong": True}


@app.get("/mcp/manifest")
def mcp_manifest(request: Request) -> dict:
    return tool_manifest(request.app.state.tool_catalog)


async def _mcp_endpoint(scope: Scope, receive: Receive, send: Send) -> None:
    await app.state.mcp_manager.handle_request(scope, receive, send)


app.mount("/mcp", _mcp_endpoint)
