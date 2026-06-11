import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Receive, Scope, Send

from hermes import __version__
from hermes.agent import Tool
from hermes.auth import bearer_auth_middleware
from hermes.config import conversation_scratch_root, get_data_dir, settings
from hermes.crypto import Encryptor, resolve_master_key
from hermes.db import init_db, make_owner_engine
from hermes.identity import SessionResolver
from hermes.logging import configure_logging, logger
from hermes.mcp_manager import McpServerManager
from hermes.mcp_server import mcp_session_manager, tool_manifest
from hermes.oauth import ClaudeOAuthDriver
from hermes.personas import ensure_backfill as ensure_personas_backfill
from hermes.personas import ensure_bootstrap_skill_seeded
from hermes.repository import sandbox_crashes as sandbox_crashes_repo
from hermes.repository import workspaces as workspaces_repo
from hermes.routes.api import router as api_router
from hermes.routes.auth import router as auth_router
from hermes.routes.chat import router as chat_router
from hermes.routes.diagnostics import router as diagnostics_router
from hermes.routes.insights import router as insights_router
from hermes.routes.llm import router as llm_router
from hermes.routes.logs import router as logs_router
from hermes.routes.mcp_health import router as mcp_health_router
from hermes.routes.mcp_servers import router as mcp_servers_router
from hermes.routes.preferences import router as preferences_router
from hermes.routes.sandbox import router as sandbox_router
from hermes.routes.skills import router as skills_router
from hermes.routes.tools import router as tools_router
from hermes.routes.workspace import router as workspace_router
from hermes.routes.workspaces import router as workspaces_router
from hermes.routes.ws_agent import router as ws_agent_router
from hermes.sandbox import WorkspaceCrash
from hermes.sandbox.factory import build_sandbox_manager
from hermes.scheduler import AgentTaskScheduler, ConversationSweepScheduler
from hermes.starter_skills import ensure_starter_skills_seeded
from hermes.tool_catalog import build_tool_catalog
from hermes.upstream import build_fallback_client, rebuild_upstream_from_db
from hermes.users import ensure_platform_admin_seeded

configure_logging()


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
        database_url=settings.database_url,
        llm_url=settings.llm_url,
        model=settings.model,
    )
    app.state.db = None
    app.state.owner_db = None
    app.state.identity_resolver = None
    # run_id → asyncio.Event for in-flight /api/chat turns. See
    # routes/api.py and hermes/agent.py for the contract.
    app.state.chat_runs = {}
    # approval_id → asyncio.Future[ApprovalDecision] for tool calls paused
    # awaiting user approval. Same single-worker invariant as chat_runs:
    # POST /api/approvals/{id} resolves the future the agent task is awaiting.
    app.state.approvals = {}
    # Plan 21: in-memory session-scope grants. `{conversation_id: {tool_name, ...}}`
    # — a conversation that resolves an approval with `allow_session` adds the
    # tool here so further calls in the same chat skip the gate. Always-scope
    # grants live in the `tool_approvals` table because they need to outlive
    # this dict (process restart).
    app.state.session_approvals = {}
    app.state.encryptor = None
    app.state.oauth_driver = None
    app.state.upstream = None
    app.state.external_http = None
    app.state.brave_api_key = None
    app.state.scheduler = None
    app.state.conversation_sweeper = None
    app.state.tool_catalog = []
    # Plan 32: external-MCP-server lifecycle manager. Distinct from
    # `app.state.mcp_manager` (the inbound StreamableHTTP server used by
    # Cline/HaexChat); this one drives outbound clients to registered
    # external MCP servers and merges their tools into the catalog.
    app.state.mcp_servers_manager = None
    # Sandbox runtime (Plan 11b-a). None when no sandbox socket is configured.
    app.state.sandbox_manager = None
    app.state.sandbox_backend = None

    try:
        # Alembic-managed schema (§1 Postgres+RLS). `init_db()` runs every
        # outstanding migration as the `holzi_owner` role, then returns an
        # engine bound to `holzi_app` (NOBYPASSRLS) for per-request flow.
        app.state.db = await init_db()
        # Separate engine connected as `holzi_owner` for lifespan seeding +
        # global sweepers (list_due / sweep_expired). Disposed in the finally.
        app.state.owner_db = await make_owner_engine()
        # Resolve sessions via the OWNER engine, not the holzi_app engine.
        # `sessions` is RLS-locked (0003), but the resolver maps a bearer to a
        # user_id *before* any user is known, so `app.user_id` is still unset
        # (GUC default '0') and an RLS-bound read would see zero rows -> every
        # request 401s. This is the pre-identity bootstrap read the design doc
        # describes ("resolve -> THEN SET LOCAL app.user_id").
        #
        # The bypass works because `holzi_owner` is a BYPASSRLS/superuser role
        # (the Postgres image makes POSTGRES_USER a superuser), so it ignores
        # the policies even though they are FORCEd. This is NOT the FORCE
        # mechanism — FORCE would in fact bite a NON-superuser owner. If
        # `holzi_owner` is ever hardened to NOSUPERUSER NOBYPASSRLS, this read
        # would see zero rows and every request would 401, so that hardening
        # must ship with a real resolver bypass (SECURITY DEFINER fn, or
        # dropping `sessions` from RLS). The lookup is a fixed `sessions JOIN
        # users` selecting (user_id, role) filtered by token_hash + session
        # expiry — see identity.SessionResolver.resolve.
        app.state.identity_resolver = SessionResolver(app.state.owner_db)

        # §1: seed the env-driven platform_admin row + a never-expiring
        # session keyed on HERMES_PLATFORM_ADMIN_TOKEN. Idempotent —
        # rotating the env token drops the previous bootstrap session so
        # the old token stops working. Runs against the OWNER engine
        # because at boot there is no resolved user yet.
        admin_user_id = await ensure_platform_admin_seeded(app.state.owner_db)

        # Plan 29-A: seed the admin's default persona + per-channel prompt
        # rows before anything that resolves system prompts can run
        # (workers, scheduler, /api/chat). Idempotent — re-runs on
        # existing DBs only insert what's missing. Scoped to the admin
        # (§1 single-org); per-user backfill on signup is a §2 concern.
        await ensure_personas_backfill(app.state.db, user_id=admin_user_id)

        # `skills` is a global table (no RLS) — no user_id needed.
        await ensure_bootstrap_skill_seeded(app.state.db)

        # Plan 38: seed the 8 curated starter skills. Idempotent — ON
        # CONFLICT (slug) DO NOTHING preserves user-edited bodies.
        await ensure_starter_skills_seeded(app.state.db)

        # Plan 25: backfill workspaces from HERMES_WORKSPACE_ROOTS. The env
        # is the bootstrap mechanism; the DB is the source of truth from
        # this point on. Idempotent — already-seeded slugs are skipped, so
        # operators can leave the env in place during the transition.
        env_slugs = [
            r.strip()
            for r in settings.workspace_roots.split(",")
            if r.strip()
        ]
        if env_slugs:
            inserted = await workspaces_repo.backfill_from_env(
                app.state.db, slugs=env_slugs
            )
            if inserted:
                logger.info(
                    "workspaces_backfilled_from_env",
                    count=len(inserted),
                    slugs=inserted,
                )

        # Master key lives in the data directory; backups should capture
        # `${HERMES_DATA_DIR}` to roll the key forward together with the
        # DB-side ciphertext.
        key_file = get_data_dir() / "master.key"
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
            user_id=admin_user_id,
            encryptor=app.state.encryptor,
            fallback_llm_url=settings.llm_url,
            fallback_llm_api_key=settings.llm_api_key,
        )

        app.state.external_http = httpx.AsyncClient(timeout=20.0)
        app.state.brave_api_key = settings.brave_api_key or None

        # Plan 32: spin up registered external MCP servers BEFORE the
        # catalog is assembled so their tools land in `app.state.tool_catalog`
        # on first read. Boot failures per server don't take the lifespan
        # down — the manager marks the offending server as "crashed" and
        # the catalog skips it.
        # Plan 32-A: keep `app.state.tool_catalog` fresh whenever the MCP
        # fleet changes. routes/mcp_servers.py rebuilds it on its own CRUD
        # path, but the agent-driven `mcp_install` / `mcp_restart` meta-tools
        # never touch a route — wiring the manager's change hook covers both.
        # The manager fires this after every start/stop/restart (single
        # worker; no locking). `list_tools` reads the result live.
        # Shared `list_tools` provider for every catalog build below. Reads
        # app.state.tool_catalog live at call time (not during a build), so
        # the self-reference inside _reassemble_catalog is safe — by the time
        # list_tools runs, the assignment has landed — and a freshly-installed
        # server shows up without a stale closure.
        def _live_catalog() -> list[Tool]:
            return app.state.tool_catalog

        def _reassemble_catalog() -> None:
            app.state.tool_catalog = build_tool_catalog(
                db=app.state.db,
                external_http=app.state.external_http,
                brave_api_key=app.state.brave_api_key,
                mcp_manager=app.state.mcp_servers_manager,
                encryptor=app.state.encryptor,
                tool_catalog_provider=_live_catalog,
            )

        app.state.mcp_servers_manager = McpServerManager(
            app.state.db,
            encryptor=app.state.encryptor,
            on_catalog_change=_reassemble_catalog,
        )
        await app.state.mcp_servers_manager.start_all_enabled()

        # Explicit build covers the zero-enabled-servers case, where
        # start_all_enabled never fires _reassemble_catalog.
        app.state.tool_catalog = build_tool_catalog(
            db=app.state.db,
            external_http=app.state.external_http,
            brave_api_key=app.state.brave_api_key,
            mcp_manager=app.state.mcp_servers_manager,
            encryptor=app.state.encryptor,
            tool_catalog_provider=_live_catalog,
        )

        # Plan 16: scheduler drives `agent_tasks` (replaces the old reminder
        # scheduler). Pull the live upstream + tool catalog through closures
        # so a credential rebuild during the process lifetime doesn't strand
        # the scheduler on a stale client.
        app.state.scheduler = AgentTaskScheduler(
            app.state.db,
            owner_db=app.state.owner_db,
            encryptor=app.state.encryptor,
            fallback_proxy_url=settings.llm_url,
            tool_factory=lambda: build_tool_catalog(
                db=app.state.db,
                external_http=app.state.external_http,
                brave_api_key=app.state.brave_api_key,
                mcp_manager=app.state.mcp_servers_manager,
                encryptor=app.state.encryptor,
                tool_catalog_provider=_live_catalog,
            ),
            fallback_model=settings.model,
        )
        await app.state.scheduler.start()

        # Scratch dir lives next to the DB by default; sweeper deletes
        # the dir alongside the conversation row when TTL hits.
        scratch_root = conversation_scratch_root()
        scratch_root.mkdir(parents=True, exist_ok=True)
        app.state.conversation_sweeper = ConversationSweepScheduler(
            app.state.db, scratch_root, owner_db=app.state.owner_db
        )
        await app.state.conversation_sweeper.start()

        # Sandbox manager: present only when a Podman socket is configured.
        # Workspace/ephemeral sandboxes are spawned lazily on first use.
        built = build_sandbox_manager(settings)
        if built is not None:
            app.state.sandbox_manager, app.state.sandbox_backend = built

            # Plan 20-A: persist every dead-transition the health watcher
            # fires. Subscribe BEFORE start_health_watcher() so the very
            # first watcher tick already has the persistence handler in
            # place. The per-chat SSE handler in routes/api.py stays —
            # the manager dedupes per (workspace_id, sandbox_id), so live
            # streams and the DB both receive exactly one event per crash.
            async def persist_crash(crash: WorkspaceCrash) -> None:
                # Clean shutdown order disposes the engine *after* the
                # health watcher stops, so this branch shouldn't fire on
                # the happy path. But the watcher catches all exceptions
                # in its loop, and the manager isolates per-handler
                # failures — keep this guard so a degraded boot (where
                # `app.state.db` was never set) doesn't spam warnings on
                # every tick.
                if app.state.db is None:
                    return
                try:
                    await sandbox_crashes_repo.insert(
                        app.state.db,
                        workspace_id=crash.workspace_id,
                        sandbox_id=crash.sandbox_id,
                        crashed_at=int(time.time()),
                        state=crash.state.value,
                        exit_code=crash.exit_code,
                    )
                except Exception as exc:  # noqa: BLE001 — handler isolation
                    logger.warning(
                        "sandbox_crash_persist_failed",
                        workspace_id=crash.workspace_id,
                        error=str(exc),
                    )

            app.state.sandbox_manager.add_crash_handler(persist_crash)

            # Plan 11b-b: poll workspace liveness so a crash surfaces as a
            # `sandbox_crashed` SSE event on any active chat stream. Watcher
            # never auto-restarts — surface-only by design.
            await app.state.sandbox_manager.start_health_watcher()
            logger.info("sandbox_manager_ready", network=settings.sandbox_network)

        # Bind the inbound /mcp server to the live catalog (not a snapshot) so
        # servers installed at runtime via the UI or the `mcp_install`
        # meta-tool show up without a process restart.
        async with mcp_session_manager(_live_catalog) as mcp_mgr:
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
        if app.state.mcp_servers_manager is not None:
            await app.state.mcp_servers_manager.stop_all()
        if app.state.external_http is not None:
            await app.state.external_http.aclose()
        if app.state.upstream is not None:
            await app.state.upstream.aclose()
        if app.state.db is not None:
            await app.state.db.dispose()
        if app.state.owner_db is not None:
            await app.state.owner_db.dispose()
        logger.info("hermes_stopping")


app = FastAPI(title="Hermes", version=__version__, lifespan=lifespan)
app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth_middleware)
# CORS for the VS Code webview, whose origin is vscode-webview://<random-uuid>.
# Added last so it sits outermost and answers the OPTIONS preflight before the
# bearer-auth middleware (which would 401 the header-less preflight). Auth stays
# the real gate — the bearer token lives in the extension secret; CORS only lets
# the webview read responses. No credentials/cookies are used.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"vscode-webview://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(chat_router)
app.include_router(ws_agent_router)
app.include_router(api_router)
app.include_router(auth_router)
app.include_router(llm_router)
app.include_router(workspace_router)
app.include_router(workspaces_router)
app.include_router(sandbox_router)
app.include_router(diagnostics_router)
app.include_router(insights_router)
app.include_router(logs_router)
app.include_router(preferences_router)
app.include_router(skills_router)
app.include_router(tools_router)
app.include_router(mcp_health_router)
app.include_router(mcp_servers_router)


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
