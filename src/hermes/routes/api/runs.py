"""`/runs` — persistent agent_runs history for diagnostics."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.repository import (
    runs,
)
from hermes.routes._helpers import validate_limit

router = APIRouter()


# Mirrors the enum in repository/runs.py — keep them in sync.
RunStatus = Literal["running", "success", "cancelled", "error"]


class AgentRunResponse(BaseModel):
    id: str
    conversation_id: int
    channel: str
    model: str
    started_at: int
    finished_at: int | None
    status: RunStatus
    error_code: str | None
    error_message: str | None
    error_trace: str | None
    input_tokens: int | None
    output_tokens: int | None


def _agent_run_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "conversation_id": r.conversation_id,
        "channel": r.channel,
        "model": r.model,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "status": r.status,
        "error_code": r.error_code,
        "error_message": r.error_message,
        "error_trace": r.error_trace,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
    }


@router.get("/runs", response_model=list[AgentRunResponse])
async def api_list_runs(
    request: Request,
    conversation_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Newest-first listing of agent_runs rows for diagnostics.

    `status` accepts the same enum the table itself uses; an unknown
    value returns 400 rather than silently widening to "all rows".
    """
    limit = validate_limit(limit)
    if offset < 0:
        raise HTTPException(
            status_code=400, detail=ErrorCode.REQUEST_INVALID_OFFSET.value
        )
    if status is not None and status not in runs.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.REQUEST_INVALID_STATUS.value,
                "params": {"allowed": sorted(runs.VALID_STATUSES)},
            },
        )
    db: AsyncEngine = request.app.state.db
    rows = await runs.list_runs(
        db,
        user_id=current_user_id(request),
        conversation_id=conversation_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [_agent_run_to_dict(r) for r in rows]
