"""Channel-prompt endpoints (Plan 29-A → 37).

GET  /channels                  — list all known channels + their prompts.
PUT  /channels/{channel}        — patch prompt and/or default_persona_id.
POST /channels/{channel}/reset  — reset the prompt to the registry default."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.personas import CHANNEL_REGISTRY
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo
from hermes.routes._helpers import http_error

from ._models import (
    ChannelPromptListResponse,
    ChannelPromptResponse,
    ChannelUpdate,
    _channel_to_dict,
    _db,
)

router = APIRouter()


@router.get("/channels", response_model=ChannelPromptListResponse)
async def list_channels(request: Request) -> dict[str, Any]:
    rows = await channels_repo.list_all(_db(request))
    return {"channels": [_channel_to_dict(r) for r in rows]}


@router.put("/channels/{channel}", response_model=ChannelPromptResponse)
async def update_channel(
    channel: str, body: ChannelUpdate, request: Request
) -> dict[str, Any]:
    if channel not in CHANNEL_REGISTRY:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.CHANNEL_NOT_FOUND,
            params={"channel": channel},
        )
    db = _db(request)

    if body.default_persona_id is not None:
        target = await personas_repo.get(
            db, body.default_persona_id, user_id=current_user_id(request)
        )
        if target is None:
            raise http_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ErrorCode.PERSONA_REF_INVALID,
                params={"id": body.default_persona_id},
            )

    # Pydantic collapses "field omitted" and "field: null" into the same
    # None, but the resolver needs to distinguish. `model_fields_set`
    # surfaces only the keys the client actually sent.
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    if "prompt" in sent:
        kwargs["prompt"] = body.prompt
    if "default_persona_id" in sent:
        kwargs["default_persona_id"] = body.default_persona_id

    updated = await channels_repo.update(db, channel, **kwargs)
    if updated is None:
        # Should be unreachable now that we checked CHANNEL_REGISTRY above,
        # but covers the channel-row-missing case explicitly.
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.CHANNEL_ROW_MISSING,
            params={"channel": channel},
        )
    return _channel_to_dict(updated)


@router.post(
    "/channels/{channel}/reset", response_model=ChannelPromptResponse
)
async def reset_channel_prompt(
    channel: str, request: Request
) -> dict[str, Any]:
    if channel not in CHANNEL_REGISTRY:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.CHANNEL_NOT_FOUND,
            params={"channel": channel},
        )
    reset = await channels_repo.reset_prompt(_db(request), channel)
    if reset is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.CHANNEL_ROW_MISSING,
            params={"channel": channel},
        )
    return _channel_to_dict(reset)
