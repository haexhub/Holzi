"""`/tasks` (Plan 16) — scheduled and one-shot agent runs."""

from __future__ import annotations

import asyncio
import zoneinfo
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.logging import logger
from hermes.repository import (
    agent_tasks,
)
from hermes.routes._helpers import validate_limit

router = APIRouter()


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1)
    due_at: int | None = None
    schedule: str | None = None
    timezone: str = "UTC"
    enabled: bool = True


class TaskUpdate(BaseModel):
    # Every field is optional — only sent fields are patched. `due_at` /
    # `schedule` use the explicit "set to null" semantics via separate
    # `clear_*` flags so a missing key on the wire can't accidentally clear
    # the other half of the (exactly-one) invariant.
    title: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1)
    due_at: int | None = None
    clear_due_at: bool = False
    schedule: str | None = None
    clear_schedule: bool = False
    timezone: str | None = None
    enabled: bool | None = None


class TaskResponse(BaseModel):
    id: int
    title: str
    prompt: str
    due_at: int | None
    schedule: str | None
    timezone: str
    enabled: bool
    last_run_at: int | None
    last_status: str | None
    last_run_id: str | None
    created_at: int
    updated_at: int


class TaskRunResponse(BaseModel):
    """Returned from POST /api/tasks/{id}/run.

    The run is fire-and-forget: by the time this returns 202, the scheduler
    background task is queued but the `agent_runs` row may not exist yet.
    Clients see the resulting `last_run_id` via the next `GET /api/tasks/{id}`
    once the run is recorded. We don't pre-allocate a run id here because the
    scheduler mints its own (and we'd have to thread it through three layers
    just so the response could carry a string the UI could already poll for).
    """

    task_id: int
    status: Literal["queued"]


def _task_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "prompt": t.prompt,
        "due_at": t.due_at,
        "schedule": t.schedule,
        "timezone": t.timezone,
        "enabled": t.enabled,
        "last_run_at": t.last_run_at,
        "last_status": t.last_status,
        "last_run_id": t.last_run_id,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def _validate_timezone(tz: str) -> None:
    """Surface unknown IANA tz names as a 400 instead of a 500. `zoneinfo`
    raises `ZoneInfoNotFoundError` (a subclass of KeyError) deep inside
    cron evaluation; without this guard the user sees an opaque server
    error for a perfectly client-side mistake."""
    try:
        zoneinfo.ZoneInfo(tz)
    except zoneinfo.ZoneInfoNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.TASK_UNKNOWN_TIMEZONE.value,
                "params": {"tz": tz},
            },
        ) from exc


def _validate_task_schedule_payload(
    *, due_at: int | None, schedule: str | None
) -> None:
    """Enforce the exactly-one-of invariant at the API boundary so the
    repository layer's ValueError surfaces as a 400 instead of a 500.
    """
    if (due_at is None) == (schedule is None):
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.TASK_DUE_OR_SCHEDULE_REQUIRED.value,
        )
    if schedule is not None:
        try:
            agent_tasks.validate_schedule(schedule)
        except ValueError as exc:
            raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc


@router.get("/tasks", response_model=list[TaskResponse])
async def api_list_tasks(
    request: Request, limit: int = 200
) -> list[dict[str, Any]]:
    limit = validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    items = await agent_tasks.list_all(
        db, user_id=current_user_id(request), limit=limit
    )
    return [_task_to_dict(t) for t in items]


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_create_task(
    request: Request, body: TaskCreate
) -> dict[str, Any]:
    _validate_task_schedule_payload(due_at=body.due_at, schedule=body.schedule)
    _validate_timezone(body.timezone)
    db: AsyncEngine = request.app.state.db
    t = await agent_tasks.create(
        db,
        user_id=current_user_id(request),
        title=body.title,
        prompt=body.prompt,
        due_at=body.due_at,
        schedule=body.schedule,
        timezone=body.timezone,
        enabled=body.enabled,
    )
    return _task_to_dict(t)


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
async def api_patch_task(
    request: Request, task_id: int, body: TaskUpdate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    # Validate cron + tz up front so the repository's ValueError /
    # ZoneInfoNotFoundError surfaces as a useful 400 instead of a 500.
    if body.schedule is not None:
        try:
            agent_tasks.validate_schedule(body.schedule)
        except ValueError as exc:
            raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc
    if body.timezone is not None:
        _validate_timezone(body.timezone)
    try:
        updated = await agent_tasks.update(
            db,
            task_id,
            user_id=current_user_id(request),
            title=body.title,
            prompt=body.prompt,
            due_at=body.due_at,
            schedule=body.schedule,
            timezone=body.timezone,
            enabled=body.enabled,
            clear_due_at=body.clear_due_at,
            clear_schedule=body.clear_schedule,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc
    if updated is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.TASK_NOT_FOUND.value
        )
    return _task_to_dict(updated)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_task(request: Request, task_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await agent_tasks.delete(db, task_id, user_id=current_user_id(request)):
        raise HTTPException(
            status_code=404, detail=ErrorCode.TASK_NOT_FOUND.value
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tasks/{task_id}/run",
    response_model=TaskRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def api_run_task_now(
    request: Request, task_id: int
) -> dict[str, Any]:
    """Fire a task immediately as a background job; respond 202 so the
    client knows the run was accepted. The resulting `agent_runs` row id
    lands on the task's `last_run_id` once the scheduler records it —
    clients poll `GET /api/tasks/{id}` to pick it up. Does NOT advance the
    cron schedule — a manual run shouldn't skip the next due occurrence.
    """
    db: AsyncEngine = request.app.state.db
    task = await agent_tasks.get(db, task_id, user_id=current_user_id(request))
    if task is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.TASK_NOT_FOUND.value
        )

    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(
            status_code=503, detail=ErrorCode.TASK_SCHEDULER_NOT_CONFIGURED.value
        )

    asyncio.create_task(
        _run_task_background(scheduler, task_id),
        name=f"task-run-now-{task_id}",
    )
    return {"task_id": task_id, "status": "queued"}


async def _run_task_background(scheduler: Any, task_id: int) -> None:
    """Run a task in the background. Any error is logged but never raised
    — the API has already returned 202, so there's no caller to surface
    to. The user sees the failure via `last_status` on the next list refresh.
    """
    try:
        await scheduler.run_now(task_id)
    except LookupError:
        logger.warning("api_task_run_now_missing", task_id=task_id)
    except Exception:  # noqa: BLE001 — already persisted as last_status
        logger.exception("api_task_run_now_failed", task_id=task_id)
