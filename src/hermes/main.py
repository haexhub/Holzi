from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from hermes import __version__
from hermes.agent import run_agent
from hermes.auth import bearer_auth_middleware
from hermes.config import settings
from hermes.db import init_db
from hermes.logging import configure_logging, logger
from hermes.routes.chat import router as chat_router
from hermes.signal.client import SignalClient
from hermes.signal.worker import SignalWorker

configure_logging()

SIGNAL_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Marko, reached via Signal "
    "Note-to-Self. Be concise — usually one to three short sentences. Match "
    "Marko's preference for terse, technical communication."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "hermes_starting",
        version=__version__,
        db_path=settings.db_path,
        proxy_url=settings.proxy_url,
        signal_enabled=bool(settings.signal_number),
        model=settings.model,
    )
    app.state.db = await init_db(settings.db_path)
    app.state.upstream = httpx.AsyncClient(base_url=settings.proxy_url, timeout=60.0)
    app.state.signal_http = None
    app.state.signal_worker = None

    if settings.signal_number:
        app.state.signal_http = httpx.AsyncClient(base_url=settings.signal_url, timeout=60.0)
        signal_client = SignalClient(app.state.signal_http, settings.signal_number)

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
            signal_client,
            app.state.db,
            settings.signal_number,
            agent_runner=signal_agent_runner,
        )
        await app.state.signal_worker.start()

    try:
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
