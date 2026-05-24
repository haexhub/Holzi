"""Hot-reload of the Signal worker based on the active messenger account.

Called from the messenger CRUD routes (activate/delete) and from the
app lifespan at startup. Mirrors the upstream.rebuild_upstream_from_db
pattern so the worker can be (re)started without restarting hermes.

Worker state lives on `app.state.signal_*`:
    signal_http         httpx.AsyncClient pointed at signal-cli-rest-api
                        — always present once lifespan runs, regardless
                        of whether a number is configured
    signal_self_number  phone number the current worker polls, or None
    signal_client       SignalClient bound to signal_self_number
    signal_worker       running SignalWorker, or None

The rebuild function compares the active DB row to `signal_self_number`
and only restarts the worker when the number actually changes — calls
on every CRUD mutation are cheap when nothing relevant changed.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.logging import logger
from hermes.repository import messenger as repo
from hermes.signal.client import SignalClient
from hermes.signal.worker import SignalWorker

AgentRunnerFactory = Callable[[AsyncEngine, int], Awaitable[str]]


async def rebuild_signal_worker_from_db(app: FastAPI) -> None:
    """Reconcile the running Signal worker with the active row in
    messenger_accounts. Idempotent — calling it when nothing changed is
    a no-op."""
    db: AsyncEngine | None = getattr(app.state, "db", None)
    if db is None:
        return  # lifespan hasn't initialised the engine yet — ignore
    http: httpx.AsyncClient | None = getattr(app.state, "signal_http", None)
    if http is None:
        # No signal-cli-rest-api client → we can't run a worker. The
        # lifespan creates this unconditionally these days, but be
        # defensive for tests that mount the router without lifespan.
        return

    active = await repo.get_active(db, "signal")
    current_number: str | None = getattr(app.state, "signal_self_number", None)
    desired_number = active.phone_number if active else None

    if desired_number == current_number:
        return  # already in sync — nothing to do

    # Stop the current worker first; activate-then-replace ordering keeps
    # us from double-polling the same number for a few ms.
    worker = getattr(app.state, "signal_worker", None)
    if worker is not None:
        await worker.stop()
        logger.info("signal_worker_stopped", phone_number=current_number)
        app.state.signal_worker = None
        app.state.signal_client = None
        app.state.signal_self_number = None

    if desired_number is None:
        return  # nothing active → stay stopped

    agent_runner_factory: AgentRunnerFactory | None = getattr(
        app.state, "signal_agent_runner_factory", None
    )
    if agent_runner_factory is None:
        # The lifespan registers this once at startup — if it's missing
        # the worker can't dispatch incoming messages to the agent loop,
        # so refuse to start rather than silently swallow them.
        logger.error("signal_worker_no_agent_runner")
        return

    client = SignalClient(http, desired_number)
    new_worker = SignalWorker(
        client,
        db,
        desired_number,
        agent_runner=agent_runner_factory,
    )
    await new_worker.start()
    app.state.signal_client = client
    app.state.signal_worker = new_worker
    app.state.signal_self_number = desired_number
    logger.info("signal_worker_started", phone_number=desired_number)
