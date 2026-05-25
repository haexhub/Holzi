from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Receive, Scope, Send

from hermes import __version__
from hermes.agent import run_agent
from hermes.auth import bearer_auth_middleware
from hermes.config import settings
from hermes.crypto import Encryptor, resolve_master_key
from hermes.db import init_db
from hermes.logging import configure_logging, logger
from hermes.mcp_server import mcp_session_manager, tool_manifest
from hermes.oauth import ClaudeOAuthDriver
from hermes.repository import llm_credentials as llm_credentials_repo
from hermes.routes.api import router as api_router
from hermes.routes.chat import router as chat_router
from hermes.routes.llm import router as llm_router
from hermes.routes.messenger import router as messenger_router
from hermes.scheduler import ReminderScheduler
from hermes.signal.lifecycle import rebuild_signal_worker_from_db
from hermes.tool_catalog import build_tool_catalog
from hermes.upstream import build_fallback_client, rebuild_upstream_from_db

configure_logging()

SIGNAL_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Martin, reached via Signal "
    "Note-to-Self. Be concise — usually one to three short sentences. Match "
    "Martin's preference for terse, technical communication."
)


def build_upstream_client(llm_url: str, llm_api_key: str) -> httpx.AsyncClient:
    """Back-compat shim around `upstream.build_fallback_client` —
    test modules import this name."""
    return build_fallback_client(llm_url=llm_url, llm_api_key=llm_api_key)


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
    app.state.encryptor = None
    app.state.oauth_driver = None
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
        # Master key lives next to the DB so backups capture both. Pure
        # in-memory DBs (test path) fall back to a temp file under cwd.
        key_file = (
            Path(settings.db_path).resolve().parent
            if settings.db_path != ":memory:"
            else Path.cwd()
        ) / "master.key"
        master = resolve_master_key(
            secret_key_env=settings.secret_key, key_file_path=key_file
        )
        app.state.encryptor = Encryptor(master)
        app.state.oauth_driver = ClaudeOAuthDriver()
        # Initial upstream from env vars; rebuild_upstream_from_db then
        # promotes to the active DB credential if one exists.
        app.state.upstream = build_upstream_client(settings.llm_url, settings.llm_api_key)
        await rebuild_upstream_from_db(
            app,
            db=app.state.db,
            encryptor=app.state.encryptor,
            fallback_llm_url=settings.llm_url,
            fallback_llm_api_key=settings.llm_api_key,
        )

        # Always create the signal-cli-rest-api http client — the /link
        # endpoints under /api/messenger need it even when no number is
        # linked yet. The actual worker only spins up once a row in
        # messenger_accounts is marked active.
        app.state.signal_http = httpx.AsyncClient(base_url=settings.signal_url, timeout=60.0)

        async def signal_agent_runner(
            db: AsyncEngine, conversation_id: int
        ) -> str:
            model = (
                await llm_credentials_repo.get_active_model(db)
            ) or settings.model
            return await run_agent(
                upstream=app.state.upstream,
                db=db,
                conversation_id=conversation_id,
                system_prompt=SIGNAL_SYSTEM_PROMPT,
                model=model,
            )

        # Registered on app.state so the hot-reload path in
        # signal/lifecycle.py can rebuild the worker on activate/delete.
        app.state.signal_agent_runner_factory = signal_agent_runner

        # Start the worker if an active signal account already exists in
        # the DB. Legacy fallback: if HERMES_SIGNAL_NUMBER is set but no
        # DB row exists, materialise one — keeps existing env-driven
        # local-dev setups working until the UI link flow takes over.
        if settings.signal_number:
            from hermes.repository import messenger as _messenger_repo

            existing = await _messenger_repo.get_by_phone(
                app.state.db, settings.signal_number
            )
            if existing is None:
                created = await _messenger_repo.create_signal(
                    app.state.db, settings.signal_number
                )
                await _messenger_repo.activate(app.state.db, created.id)
                logger.info(
                    "signal_env_account_seeded", phone_number=settings.signal_number
                )
        await rebuild_signal_worker_from_db(app)

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
        if app.state.oauth_driver is not None:
            await app.state.oauth_driver.cancel_all()
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
            await app.state.db.dispose()
        logger.info("hermes_stopping")


app = FastAPI(title="Hermes", version=__version__, lifespan=lifespan)
app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth_middleware)
app.include_router(chat_router)
app.include_router(api_router)
app.include_router(llm_router)
app.include_router(messenger_router)


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
