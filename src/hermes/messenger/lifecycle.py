"""Aggregate hot-reload entry point for all messenger workers.

CRUD on `messenger_accounts` (activate/delete/create) calls this once;
it fans out to per-provider rebuilds. Each underlying rebuild is itself
idempotent and a no-op when nothing changed, so calling this on every
mutation is cheap.

The per-provider lifecycles (`signal/lifecycle.py`, `telegram/lifecycle.py`)
stay as the source of truth for their respective worker state — the
aggregator here only sequences them, it doesn't share state.
"""
from __future__ import annotations

from fastapi import FastAPI

from hermes.signal.lifecycle import rebuild_signal_worker_from_db
from hermes.telegram.lifecycle import rebuild_telegram_worker_from_db


async def rebuild_messenger_workers_from_db(app: FastAPI) -> None:
    await rebuild_signal_worker_from_db(app)
    await rebuild_telegram_worker_from_db(app)
