from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from hermes import __version__
from hermes.auth import bearer_auth_middleware
from hermes.config import settings
from hermes.db import init_db
from hermes.logging import configure_logging, logger
from hermes.routes.chat import router as chat_router

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "hermes_starting",
        version=__version__,
        db_path=settings.db_path,
        proxy_url=settings.proxy_url,
    )
    app.state.db = await init_db(settings.db_path)
    app.state.upstream = httpx.AsyncClient(base_url=settings.proxy_url, timeout=60.0)
    try:
        yield
    finally:
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
