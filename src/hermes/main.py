from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Receive, Scope, Send

from hermes import __version__
from hermes.agent import run_agent
from hermes.auth import bearer_auth_middleware
from hermes.config import settings
from hermes.db import init_db
from hermes.logging import configure_logging, logger
from hermes.mcp_server import mcp_session_manager, tool_manifest
from hermes.routes.api import router as api_router
from hermes.routes.chat import router as chat_router
from hermes.scheduler import ReminderScheduler
from hermes.signal.client import SignalClient
from hermes.signal.worker import SignalWorker
from hermes.tool_catalog import build_tool_catalog

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
    app.state.db = None
    app.state.upstream = None
    app.state.signal_http = None
    app.state.signal_client = None
    app.state.signal_self_number = None
    app.state.signal_worker = None
    app.state.external_http = None
    app.state.brave_api_key = None
    app.state.scheduler = None
    app.state.tool_catalog = []

    try:
        app.state.db = await init_db(settings.db_path)
        app.state.upstream = build_upstream_client(settings.llm_url, settings.llm_api_key)

        if settings.signal_number:
            app.state.signal_http = httpx.AsyncClient(base_url=settings.signal_url, timeout=60.0)
            app.state.signal_client = SignalClient(app.state.signal_http, settings.signal_number)
            app.state.signal_self_number = settings.signal_number

            async def signal_agent_runner(
                db: AsyncConnection, conversation_id: int
            ) -> str:
                return await run_agent(
                    upstream=app.state.upstream,
                    db=db,
                    conversation_id=conversation_id,
                    system_prompt=SIGNAL_SYSTEM_PROMPT,
                    model=settings.model,
                )

            app.state.signal_worker = SignalWorker(
                app.state.signal_client,
                app.state.db,
                settings.signal_number,
                agent_runner=signal_agent_runner,
            )
            await app.state.signal_worker.start()

        app.state.external_http = httpx.AsyncClient(timeout=20.0)
        app.state.brave_api_key = settings.brave_api_key or None

        # MCP and the /mcp/manifest surface use a current_channel=None catalog
        # — external callers (Cline, HaexChat) don't carry a single
        # "current channel" notion. /api/chat rebuilds per request with
        # current_channel="web" via build_tool_catalog() for the recursion
        # guard.
        app.state.tool_catalog = build_tool_catalog(
            db=app.state.db,
            signal_client=app.state.signal_client,
            signal_self_number=app.state.signal_self_number,
            external_http=app.state.external_http,
            brave_api_key=app.state.brave_api_key,
            current_channel=None,
        )

        app.state.scheduler = ReminderScheduler(
            app.state.db,
            app.state.signal_client,
            app.state.signal_self_number,
        )
        await app.state.scheduler.start()

        async with mcp_session_manager(app.state.tool_catalog) as mcp_mgr:
            app.state.mcp_manager = mcp_mgr
            yield
    finally:
        if app.state.scheduler is not None:
            await app.state.scheduler.stop()
        if app.state.signal_worker is not None:
            await app.state.signal_worker.stop()
        if app.state.signal_http is not None:
            await app.state.signal_http.aclose()
        if app.state.external_http is not None:
            await app.state.external_http.aclose()
        if app.state.upstream is not None:
            await app.state.upstream.aclose()
        if app.state.db is not None:
            engine = app.state.db.engine
            await app.state.db.close()
            await engine.dispose()
        logger.info("hermes_stopping")


app = FastAPI(title="Hermes", version=__version__, lifespan=lifespan)
app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth_middleware)
app.include_router(chat_router)
app.include_router(api_router)


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
