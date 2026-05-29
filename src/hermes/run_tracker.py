"""Run-record lifecycle around a single agent turn.

Wrap the call to `run_agent` (or any equivalent agent-loop entry point)
with `track_run` so the persistent `agent_runs` row is created on entry
and finalised on exit — regardless of which terminal path the run takes
(success / ChatRunCancelled / asyncio.CancelledError / generic
Exception). Also binds structured-log contextvars so every log line the
agent loop emits is correlated by `run_id`.

Token-usage metrics are captured into the `metrics` dict the caller
passes to `run_agent` (the agent layer mutates it in place when the
upstream provider includes a `usage` block). We read the dict after the
run and persist whatever is there — NULL columns when the upstream
didn't say.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import ChatRunCancelled
from hermes.logging import logger
from hermes.repository import runs as runs_repo


def classify_run_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (error_code, error_message).

    Mirrors the codes used by routes/api.py's SSE `error` event payloads
    so a frontend that has learned the SSE codes can also read them out
    of `agent_runs`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        upstream_status = exc.response.status_code
        return "upstream_http_error", f"upstream returned {upstream_status}"
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout", str(exc) or "upstream timed out"
    if isinstance(exc, httpx.RequestError):
        return "upstream_unreachable", f"could not reach upstream: {exc}"
    return "agent_error", str(exc) or exc.__class__.__name__


@asynccontextmanager
async def track_run(
    db: AsyncEngine,
    *,
    run_id: str,
    conversation_id: int,
    channel: str,
    model: str,
    metrics: dict[str, Any] | None = None,
    agent_task_id: int | None = None,
) -> AsyncIterator[None]:
    """Insert a 'running' row, finalise it once the context exits.

    `metrics` is the dict the caller will hand to `run_agent`'s
    `metrics=` parameter. We read `input_tokens` / `output_tokens` out of
    it on exit if the agent populated them. `agent_task_id` is set by the
    scheduler so the run row can be linked back to the originating task
    (Plan 16).

    Re-raises whatever exception the wrapped code raised — this is a
    pure side-effect helper, never a swallower.
    """
    started_at = int(time.time())
    await runs_repo.insert(
        db,
        run_id=run_id,
        conversation_id=conversation_id,
        channel=channel,
        model=model,
        started_at=started_at,
        agent_task_id=agent_task_id,
    )
    structlog.contextvars.bind_contextvars(
        run_id=run_id, conversation_id=conversation_id, channel=channel
    )
    logger.info(
        "agent_run_started",
        run_id=run_id,
        conversation_id=conversation_id,
        channel=channel,
        model=model,
    )
    status: str = "error"
    error_code: str | None = "agent_error"
    error_message: str | None = "agent_run did not finalise"
    error_trace: str | None = None
    try:
        yield
        status = "success"
        error_code = None
        error_message = None
    except ChatRunCancelled:
        status = "cancelled"
        error_code = None
        error_message = None
        raise
    except asyncio.CancelledError:
        # Client disconnect / outer task.cancel(). Treat as cancelled —
        # the user (or runtime) chose to stop this turn, not a failure.
        status = "cancelled"
        error_code = None
        error_message = None
        raise
    except BaseException as exc:  # noqa: BLE001 — re-raised below
        status = "error"
        code, message = classify_run_error(exc)
        error_code = code
        error_message = message
        error_trace = traceback.format_exc()
        raise
    finally:
        finished_at = int(time.time())
        elapsed_ms = max(0, (finished_at - started_at) * 1000)
        token_kwargs: dict[str, int] = {}
        if metrics is not None:
            it = metrics.get("input_tokens")
            ot = metrics.get("output_tokens")
            if isinstance(it, int):
                token_kwargs["input_tokens"] = it
            if isinstance(ot, int):
                token_kwargs["output_tokens"] = ot
        try:
            await runs_repo.finalize(
                db,
                run_id,
                status=status,
                finished_at=finished_at,
                error_code=error_code,
                error_message=error_message,
                error_trace=error_trace,
                **token_kwargs,
            )
        except Exception:  # noqa: BLE001 — never crash the caller's finally
            logger.exception(
                "agent_run_finalize_failed",
                run_id=run_id,
                conversation_id=conversation_id,
                channel=channel,
                status=status,
            )
        logger.info(
            "agent_run_finished",
            run_id=run_id,
            conversation_id=conversation_id,
            channel=channel,
            status=status,
            error_code=error_code,
            elapsed_ms=elapsed_ms,
        )
        structlog.contextvars.unbind_contextvars(
            "run_id", "conversation_id", "channel"
        )
