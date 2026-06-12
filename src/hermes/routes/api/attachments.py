"""Attachment upload endpoint (Plan 11).

The `AttachmentResponse` model and helpers live in `conversations.py`
because they're embedded in `MessageResponse` — pulling them here would
create a cycle. This module owns just the upload endpoint."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import attachments as attachments_mod
from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.repository import (
    attachments,
    conversations,
)

from .conversations import AttachmentResponse, _attachment_to_dict

router = APIRouter()


@router.post(
    "/conversations/{conv_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_upload_attachment(
    request: Request,
    conv_id: int,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency default
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    uid = current_user_id(request)
    convo = await conversations.get(db, conv_id, user_id=uid)
    if convo is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )

    content_type = file.content_type or "application/octet-stream"
    if not attachments_mod.is_allowed(content_type):
        raise HTTPException(
            status_code=415,
            detail={
                "code": ErrorCode.ATTACHMENT_UNSUPPORTED_TYPE.value,
                "params": {"type": content_type},
            },
        )

    # Read in chunks so an oversized body is rejected without buffering the
    # whole thing in memory (Starlette already spools to disk past 1 MB).
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > attachments_mod.MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": ErrorCode.ATTACHMENT_TOO_LARGE.value,
                    "params": {
                        "max_bytes": attachments_mod.MAX_ATTACHMENT_BYTES,
                    },
                },
            )

    # On-disk name is an opaque token, so the user-supplied filename can
    # never influence the path (no traversal possible).
    token = uuid.uuid4().hex
    target_dir = attachments_mod.attachment_dir(conv_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / token
    target_path.write_bytes(bytes(data))

    # The blob is on disk before the metadata row exists; if the insert fails
    # we must remove it, otherwise every partial failure orphans a file with
    # no row pointing at it.
    try:
        att = await attachments.create(
            db,
            user_id=uid,
            conversation_id=conv_id,
            filename=attachments_mod.safe_display_filename(file.filename),
            content_type=content_type,
            size=len(data),
            storage_path=token,
        )
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    return _attachment_to_dict(att)
