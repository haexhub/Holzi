import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Receive, Scope, Send

from hermes import __version__
from hermes.agent import run_agent
from hermes.auth import bearer_auth_middleware
from hermes.config import conversation_scratch_root, settings
from hermes.crypto import Encryptor, resolve_master_key
from hermes.db import init_db
from hermes.logging import configure_logging, logger
from hermes.mcp_server import mcp_session_manager, tool_manifest
from hermes.oauth import ClaudeOAuthDriver
from hermes.repository import llm_credentials as llm_credentials_repo
from hermes.routes.api import router as api_router
from hermes.routes.chat import router as chat_router
from hermes.routes.diagnostics import router as diagnostics_router
from hermes.routes.llm import router as llm_router
from hermes.routes.messenger import router as messenger_router
from hermes.routes.workspace import router as workspace_router
from hermes.run_tracker import track_run
from hermes.sandbox.factory import build_sandbox_manager
from hermes.scheduler import AgentTaskScheduler, ConversationSweepScheduler
from hermes.signal.lifecycle import rebuild_signal_worker_from_db
from hermes.telegram.lifecycle import rebuild_telegram_worker_from_db
from hermes.tool_catalog import build_tool_catalog
from hermes.upstream import build_fallback_client, rebuild_upstream_from_db

configure_logging()

SIGNAL_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Martin, reached via Signal "
    "Note-to-Self. Be concise — usually one to three short sentences. Match "
    "Martin's preference for terse, technical communication."
)

TELEGRAM_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Martin, reached via a "
    "Telegram bot. Be concise — usually one to three short sentences. Match "
    "Martin's preference for terse, technical communication."
)


def build_upstream_client(llm_url: str, llm_api_key: str) -> httpx.AsyncClient:
    """Back-compat shim around `upstream.build_fallback_client` —
    test modules import this name."""
    return build_fallback_client(llm_url=llm_url, llm_api_key=llm_api_key)


def _refuse_multi_worker_startup() -> None:
    """Holzi's in-memory cancel registry (and future approval registry)
    require that every request for a given run_id lands in the same
    process. Bail out loudly if the operator configured uvicorn /
    gunicorn for >1 worker, rather than silently miss cancels.

    Checked envs match what the supported deploy tooling actually sets:
    uvicorn reads UVICORN_WORKERS, gunicorn reads GUNICORN_WORKERS, and
    both honour the more generic WEB_CONCURRENCY.
    """
    for env in ("UVICORN_WORKERS", "GUNICORN_WORKERS", "WEB_CONCURRENCY"):
        raw = os.environ.get(env)
        if raw is None or raw.strip() == "":
            continue
        try:
            workers = int(raw)
        except ValueError:
            continue
        if workers > 1:
            raise RuntimeError(
                f"Holzi requires a single worker (got {env}={workers}). "
                "The in-memory chat-run registry assumes one process per "
                "user; scale by running more containers, not more workers."
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _refuse_multi_worker_startup()
    logger.info(
        "hermes_starting",
        version=__version__,
        db_path=settings.db_path,
        llm_url=settings.llm_url,
        signal_enabled=bool(settings.signal_number),
        model=settings.model,
    )
    app.state.db = None
    # run_id → asyncio.Event for in-flight /api/chat turns. See
    # routes/api.py and hermes/agent.py for the contract.
    app.state.chat_runs = {}
    # approval_id → asyncio.Future[ApprovalDecision] for tool calls paused
    # awaiting user approval. Same single-worker invariant as chat_runs:
    # POST /api/approvals/{id} resolves the future the agent task is awaiting.
    app.state.approvals = {}
    app.state.encryptor = None
    app.state.oauth_driver = None
    app.state.upstream = None
    app.state.signal_http = None
    app.state.signal_client = None
    app.state.signal_self_number = None
    app.state.signal_worker = None
    app.state.telegram_worker = None
    app.state.telegram_bot_username = None
    app.state.telegram_allowed_chat_ids = None
    app.state.external_http = None
    app.state.brave_api_key = None
    app.state.scheduler = None
    app.state.conversation_sweeper = None
    app.state.tool_catalog = []
    # Sandbox runtime (Plan 11b-a). None when no sandbox socket is configured.
    app.state.sandbox_manager = None
    app.state.sandbox_backend = None

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
            run_id = uuid.uuid4().hex
            metrics: dict[str, Any] = {}
            async with track_run(
                db,
                run_id=run_id,
                conversation_id=conversation_id,
                channel="signal",
                model=model,
                metrics=metrics,
            ):
                return await run_agent(
                    upstream=app.state.upstream,
                    db=db,
                    conversation_id=conversation_id,
                    system_prompt=SIGNAL_SYSTEM_PROMPT,
                    model=model,
                    metrics=metrics,
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

        async def telegram_agent_runner(
            db: AsyncEngine, conversation_id: int
        ) -> str:
            model = (
                await llm_credentials_repo.get_active_model(db)
            ) or settings.model
            run_id = uuid.uuid4().hex
            metrics: dict[str, Any] = {}
            async with track_run(
                db,
                run_id=run_id,
                conversation_id=conversation_id,
                channel="telegram",
                model=model,
                metrics=metrics,
            ):
                return await run_agent(
                    upstream=app.state.upstream,
                    db=db,
                    conversation_id=conversation_id,
                    system_prompt=TELEGRAM_SYSTEM_PROMPT,
                    model=model,
                    metrics=metrics,
                )

        # Telegram worker hot-reload depends on external_http, so this
        # has to come after the external_http create above.
        app.state.telegram_agent_runner_factory = telegram_agent_runner
        await rebuild_telegram_worker_from_db(app)

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

        # Plan 16: scheduler drives `agent_tasks` (replaces the old reminder
        # scheduler). Pull the live upstream + tool catalog through closures
        # so a credential rebuild during the process lifetime doesn't strand
        # the scheduler on a stale client.
        app.state.scheduler = AgentTaskScheduler(
            app.state.db,
            upstream_provider=lambda: app.state.upstream,
            tool_factory=lambda: build_tool_catalog(
                db=app.state.db,
                signal_client=app.state.signal_client,
                signal_self_number=app.state.signal_self_number,
                external_http=app.state.external_http,
                brave_api_key=app.state.brave_api_key,
                current_channel="task",
            ),
            fallback_model=settings.model,
        )
        await app.state.scheduler.start()

        # Scratch dir lives next to the DB by default; sweeper deletes
        # the dir alongside the conversation row when TTL hits.
        scratch_root = conversation_scratch_root()
        scratch_root.mkdir(parents=True, exist_ok=True)
        app.state.conversation_sweeper = ConversationSweepScheduler(
            app.state.db, scratch_root
        )
        await app.state.conversation_sweeper.start()

        # Sandbox manager: present only when a Podman socket is configured.
        # Workspace/ephemeral sandboxes are spawned lazily on first use.
        built = build_sandbox_manager(settings)
        if built is not None:
            app.state.sandbox_manager, app.state.sandbox_backend = built
            # Plan 11b-b: poll workspace liveness so a crash surfaces as a
            # `sandbox_crashed` SSE event on any active chat stream. Watcher
            # never auto-restarts — surface-only by design.
            await app.state.sandbox_manager.start_health_watcher()
            logger.info("sandbox_manager_ready", network=settings.sandbox_network)

        async with mcp_session_manager(app.state.tool_catalog) as mcp_mgr:
            app.state.mcp_manager = mcp_mgr
            yield
    finally:
        if app.state.oauth_driver is not None:
            await app.state.oauth_driver.cancel_all()
        if app.state.scheduler is not None:
            await app.state.scheduler.stop()
        if app.state.conversation_sweeper is not None:
            await app.state.conversation_sweeper.stop()
        if app.state.sandbox_manager is not None:
            await app.state.sandbox_manager.shutdown()
        if app.state.sandbox_backend is not None:
            await app.state.sandbox_backend.aclose()
        if app.state.signal_worker is not None:
            await app.state.signal_worker.stop()
        if app.state.signal_http is not None:
            await app.state.signal_http.aclose()
        if app.state.telegram_worker is not None:
            await app.state.telegram_worker.stop()
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
app.include_router(workspace_router)
app.include_router(diagnostics_router)


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
