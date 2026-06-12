"""Persona history endpoints (Plan 36).

GET  /personas/{id}/history                       — list snapshots.
POST /personas/{id}/history/{snapshot_id}/restore — apply a snapshot."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy.exc import IntegrityError

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.repository import persona_history as persona_history_repo
from hermes.repository import personas as personas_repo
from hermes.routes._helpers import http_error

from ._models import (
    PersonaHistoryListResponse,
    PersonaResponse,
    _db,
    _history_to_dict,
    _persona_to_dict,
)

router = APIRouter()


@router.get(
    "/personas/{persona_id}/history",
    response_model=PersonaHistoryListResponse,
)
async def list_persona_history(
    persona_id: int, request: Request
) -> dict[str, Any]:
    db = _db(request)
    persona = await personas_repo.get(db, persona_id, user_id=current_user_id(request))
    if persona is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )
    rows = await persona_history_repo.list_for_persona(
        db, persona_id, user_id=current_user_id(request)
    )
    return {"history": [_history_to_dict(r) for r in rows]}


@router.post(
    "/personas/{persona_id}/history/{snapshot_id}/restore",
    response_model=PersonaResponse,
)
async def restore_persona_history(
    persona_id: int, snapshot_id: int, request: Request
) -> dict[str, Any]:
    db = _db(request)
    uid = current_user_id(request)
    persona = await personas_repo.get(db, persona_id, user_id=uid)
    if persona is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )
    snapshot = await persona_history_repo.get(db, snapshot_id, user_id=uid)
    if snapshot is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_HISTORY_NOT_FOUND,
            params={"id": snapshot_id},
        )
    # Guard against cross-persona restore — the snapshot belongs to a
    # different persona, so applying it would silently rename + overwrite
    # the target. Refuse with a 422 the FE can surface as a routing error.
    if snapshot.persona_id != persona_id:
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorCode.PERSONA_HISTORY_PERSONA_MISMATCH,
            params={
                "persona_id": persona_id,
                "snapshot_id": snapshot_id,
            },
        )
    snap = json.loads(snapshot.snapshot_json)
    # Restore = update the persona with the snapshot's fields. The repo
    # `update()` auto-writes a NEW history row inside the same txn (the
    # audit trail of the restore action itself), so the Verlauf-Tab will
    # show snapshot-1, snapshot-2 (later state), snapshot-3 (restore-of-1).
    try:
        updated = await personas_repo.update(
            db,
            persona_id,
            user_id=uid,
            name=snap["name"],
            soul=snap["soul"],
            identity=snap["identity"],
            agents=snap["agents"],
        )
    except IntegrityError as exc:
        # The snapshot's `name` could now collide with a sibling persona
        # that was renamed after the snapshot was taken. Surface as the
        # same 409 PERSONA_NAME_CONFLICT shape as POST/PUT.
        raise http_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.PERSONA_NAME_CONFLICT,
            params={"name": snap["name"]},
        ) from exc
    if updated is None:
        # Race: persona was deleted between the get and the update.
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )
    return _persona_to_dict(updated)
