"""`/approvals/{approval_id}` POST and Plan-21 standing-approvals surface.

Resolves paused tool calls and exposes the standing-approval read/revoke
endpoints. Granting still goes through the approval card (POST above) — a
dedicated grant endpoint would just duplicate that flow."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import ApprovalDecision
from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.events import ApprovalDecisionLiteral
from hermes.repository import (
    approvals as approvals_repo,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /api/approvals/{approval_id} — resolve a paused, approval-gated tool.
# ---------------------------------------------------------------------------


class ApprovalDecisionRequest(BaseModel):
    # Plan 21: four-decision union. Backend interprets `allow_session` /
    # `allow_always` to upsert the standing lists; the agent loop only
    # cares about `deny` vs. not-deny.
    decision: ApprovalDecisionLiteral
    # Optional note shown to the LLM on deny so it can adapt its next turn.
    # Length-capped at 500 to keep tool errors bounded and to stop a stray
    # paste from blowing the request body up; the UI hint matches.
    reason: str | None = Field(default=None, max_length=500)


@router.post(
    "/approvals/{approval_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Unknown approval_id"},
        409: {"description": "Approval already resolved"},
    },
)
async def api_resolve_approval(
    request: Request, approval_id: str, body: ApprovalDecisionRequest
) -> Response:
    """Deliver the user's decision for a paused tool call.

    Resolves the `asyncio.Future` the agent task is awaiting; the agent then
    either runs the tool (`allow_once`) or feeds a denied result back to the
    LLM (`deny`). Unknown ids → 404; an id whose decision already landed →
    409 (the UI disables the buttons after one click, so this only fires on a
    genuine double-submit / stale tab). Single-worker invariant makes the
    in-process registry safe.
    """
    approvals: dict[str, asyncio.Future[ApprovalDecision]] = (
        request.app.state.approvals
    )
    future = approvals.get(approval_id)
    if future is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.APPROVAL_NOT_FOUND.value
        )
    if future.done():
        raise HTTPException(
            status_code=409, detail=ErrorCode.APPROVAL_ALREADY_RESOLVED.value
        )
    future.set_result(
        ApprovalDecision(decision=body.decision, reason=body.reason)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Plan 21 standing-approvals surface: read + revoke the two non-`allow_once`
# scopes. Granting still goes through the approval card (POST above) — a
# dedicated grant endpoint would just duplicate that flow.
# ---------------------------------------------------------------------------


class StandingAlwaysEntry(BaseModel):
    tool: str
    granted_at: int
    last_used_at: int | None


class StandingSessionEntry(BaseModel):
    conversation_id: int
    tool: str


class StandingApprovalsResponse(BaseModel):
    always: list[StandingAlwaysEntry]
    session: list[StandingSessionEntry]


@router.get("/approvals/standing", response_model=StandingApprovalsResponse)
async def api_list_standing_approvals(request: Request) -> StandingApprovalsResponse:
    """Read the active standing-approval lists.

    Returns persisted `allow_always` rows plus the in-memory
    `allow_session` entries currently cached on `app.state`. The
    web UI doesn't have a dedicated settings page for these yet
    (Plan 21 Non-Goal); the data shape is here so a future page
    only has to render it.
    """
    db: AsyncEngine = request.app.state.db
    always_rows = await approvals_repo.list_always(db, user_id=current_user_id(request))
    session_state: dict[int, set[str]] = request.app.state.session_approvals
    session_entries = sorted(
        (
            StandingSessionEntry(conversation_id=conv_id, tool=tool)
            for conv_id, tools in session_state.items()
            for tool in tools
        ),
        key=lambda e: (e.conversation_id, e.tool),
    )
    return StandingApprovalsResponse(
        always=[
            StandingAlwaysEntry(
                tool=row.tool_name,
                granted_at=row.granted_at,
                last_used_at=row.last_used_at,
            )
            for row in always_rows
        ],
        session=session_entries,
    )


StandingScope = Literal["always", "session"]


@router.delete(
    "/approvals/standing/{tool_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Tool not in the standing list for that scope"}},
)
async def api_revoke_standing_approval(
    request: Request, tool_name: str, scope: StandingScope
) -> Response:
    """Drop a standing-approval grant so the next call re-prompts.

    `scope=always` deletes the `tool_approvals` row; `scope=session`
    removes the tool from every conversation's in-memory set (single-user
    deployment — there's only one user's app.state to scrub). Missing
    tools 404 so the UI can tell a stale revoke from a successful one.
    """
    if scope == "always":
        db: AsyncEngine = request.app.state.db
        removed = await approvals_repo.revoke_always(
            db, tool_name, user_id=current_user_id(request)
        )
        if not removed:
            raise HTTPException(
                status_code=404, detail=ErrorCode.TOOL_NOT_IN_ALWAYS_ALLOWED.value
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # scope == "session": single user / single-worker deployment, so we
    # walk every conversation's set. Conversations whose set becomes empty
    # get dropped to keep the dict tidy.
    session_state: dict[int, set[str]] = request.app.state.session_approvals
    touched = False
    for conv_id in list(session_state.keys()):
        tools = session_state[conv_id]
        if tool_name in tools:
            tools.discard(tool_name)
            touched = True
            if not tools:
                session_state.pop(conv_id, None)
    if not touched:
        raise HTTPException(
            status_code=404, detail=ErrorCode.TOOL_NOT_IN_SESSION_ALLOWED.value
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
