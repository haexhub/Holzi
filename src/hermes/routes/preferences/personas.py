"""Personas CRUD endpoints (Plan 29-A → 36 → 37).

POST/GET/PUT/DELETE on `/personas`, plus `/personas/{id}/models` which
wraps the LLM-credential model list so the UI doesn't have to know
which credential a given persona is using."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.provider_models import ProviderModelsError, list_provider_models
from hermes.repository import llm_credentials as llm_credentials_repo
from hermes.repository import personas as personas_repo
from hermes.repository.models import LlmCredential
from hermes.repository.personas import _UNSET as REPO_UNSET
from hermes.routes._helpers import http_error

from ._models import (
    PersonaCreate,
    PersonaListResponse,
    PersonaResponse,
    PersonaUpdate,
    _db,
    _persona_to_dict,
)

router = APIRouter()


@router.get("/personas", response_model=PersonaListResponse)
async def list_personas(request: Request) -> dict[str, Any]:
    rows = await personas_repo.list_all(
        _db(request), user_id=current_user_id(request)
    )
    return {"personas": [_persona_to_dict(p) for p in rows]}


@router.post(
    "/personas",
    status_code=status.HTTP_201_CREATED,
    response_model=PersonaResponse,
)
async def create_persona(
    body: PersonaCreate, request: Request
) -> dict[str, Any]:
    # All-empty bodies aren't useful — without any fragment the persona
    # contributes nothing to the resolver output. Enforced here (not via
    # a `model_validator`) so the 422 detail shape stays `{code, params}`
    # consistent with the rest of the API.
    if not (
        body.soul.strip() or body.identity.strip() or body.agents.strip()
    ):
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY,
            params={},
        )
    try:
        persona = await personas_repo.create(
            _db(request),
            user_id=current_user_id(request),
            name=body.name,
            soul=body.soul,
            identity=body.identity,
            agents=body.agents,
            is_default=body.is_default,
        )
    except IntegrityError as exc:
        raise http_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.PERSONA_NAME_CONFLICT,
            params={"name": body.name},
        ) from exc
    return _persona_to_dict(persona)


@router.put("/personas/{persona_id}", response_model=PersonaResponse)
async def update_persona(
    persona_id: int, body: PersonaUpdate, request: Request
) -> dict[str, Any]:
    db = _db(request)
    uid = current_user_id(request)
    existing = await personas_repo.get(db, persona_id, user_id=uid)
    if existing is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )

    # Refuse to demote the only default — the resolver always needs one.
    if (
        body.is_default is False
        and existing.is_default
    ):
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorCode.PERSONA_DEFAULT_DEMOTE,
        )

    # All-empty check on the POST-merge state: fields with `None` keep
    # their existing value, otherwise the patch wins. If the result would
    # have all three fragments empty (after `.strip()`), refuse — the
    # resolver would compose an empty persona section, which is useless.
    merged_soul = existing.soul if body.soul is None else body.soul
    merged_identity = (
        existing.identity if body.identity is None else body.identity
    )
    merged_agents = existing.agents if body.agents is None else body.agents
    if not (
        merged_soul.strip()
        or merged_identity.strip()
        or merged_agents.strip()
    ):
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY,
            params={},
        )

    # Resolve new credential/model fields — only touch if they appear in the request body.
    new_cred_id = REPO_UNSET
    new_model = REPO_UNSET
    _fetched_cred = None  # cache to avoid double-fetch when both fields are set

    if "llm_credential_id" in body.model_fields_set:
        if body.llm_credential_id is not None:
            _fetched_cred = await llm_credentials_repo.get(
                db, body.llm_credential_id, user_id=uid
            )
            if _fetched_cred is None:
                raise http_error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    ErrorCode.PERSONA_INVALID_CREDENTIAL,
                    params={"id": body.llm_credential_id},
                )
        new_cred_id = body.llm_credential_id
        # Clearing the credential also orphans any pinned model — auto-clear it
        # unless the caller explicitly sets model in the same request.
        if new_cred_id is None and "model" not in body.model_fields_set:
            new_model = None

    if "model" in body.model_fields_set:
        if body.model is not None:
            # model can only be set when a credential is configured (new or existing)
            effective_cred_id = (
                body.llm_credential_id
                if "llm_credential_id" in body.model_fields_set
                else existing.llm_credential_id
            )
            if effective_cred_id is None:
                raise http_error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    ErrorCode.PERSONA_INVALID_MODEL,
                    params={"model": body.model, "reason": "no_credential"},
                )
            # Reuse the credential already fetched above when possible.
            cred_for_model: LlmCredential | None
            if _fetched_cred is not None and _fetched_cred.id == effective_cred_id:
                cred_for_model = _fetched_cred
            else:
                cred_for_model = await llm_credentials_repo.get(
                    db, effective_cred_id, user_id=uid
                )
                if cred_for_model is None:
                    raise http_error(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        ErrorCode.PERSONA_INVALID_CREDENTIAL,
                        params={"id": effective_cred_id},
                    )
            try:
                available_models = await list_provider_models(
                    cred_for_model,
                    http=request.app.state.external_http,
                    encryptor=request.app.state.encryptor,
                )
                if not any(m.id == body.model for m in available_models):
                    raise http_error(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        ErrorCode.PERSONA_INVALID_MODEL,
                        params={"model": body.model},
                    )
            except ProviderModelsError:
                pass  # provider unreachable; skip model validation
        new_model = body.model

    try:
        updated = await personas_repo.update(
            db,
            persona_id,
            user_id=uid,
            name=body.name,
            soul=body.soul,
            identity=body.identity,
            agents=body.agents,
            is_default=body.is_default,
            llm_credential_id=new_cred_id,
            model=new_model,
        )
    except IntegrityError as exc:
        raise http_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.PERSONA_NAME_CONFLICT,
            params={"name": body.name or ""},
        ) from exc
    if updated is None:
        # Race: someone deleted the row between the get and the update.
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )
    return _persona_to_dict(updated)


@router.delete(
    "/personas/{persona_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_persona(persona_id: int, request: Request) -> Response:
    db = _db(request)
    uid = current_user_id(request)
    existing = await personas_repo.get(db, persona_id, user_id=uid)
    if existing is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )
    if existing.is_default:
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorCode.PERSONA_DEFAULT_DELETE,
        )
    deleted = await personas_repo.delete(db, persona_id, user_id=uid)
    if not deleted:
        # Race or unexpected — surface as 404.
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/personas/{persona_id}/models")
async def list_persona_models(persona_id: int, request: Request) -> dict[str, Any]:
    """Return the model list for this persona's credential (or the active credential).

    Wraps GET /api/llm/credentials/{id}/models so the UI doesn't need to know
    which credential a persona uses.
    """
    db = _db(request)
    uid = current_user_id(request)
    persona = await personas_repo.get(db, persona_id, user_id=uid)
    if persona is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.PERSONA_NOT_FOUND,
            params={"id": persona_id},
        )

    cred = None
    if persona.llm_credential_id is not None:
        cred = await llm_credentials_repo.get(db, persona.llm_credential_id, user_id=uid)
    if cred is None:
        cred = await llm_credentials_repo.get_active(db, user_id=uid)
    if cred is None:
        raise http_error(503, ErrorCode.PERSONA_NO_CREDENTIAL)

    try:
        models = await list_provider_models(
            cred,
            http=request.app.state.external_http,
            encryptor=request.app.state.encryptor,
        )
    except ProviderModelsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"models": [{"id": m.id, "label": m.label} for m in models]}
