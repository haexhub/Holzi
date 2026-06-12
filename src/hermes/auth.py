from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from hermes.db import reset_current_user, set_current_user_token
from hermes.logging import logger

PUBLIC_PATHS: frozenset[str] = frozenset({"/healthz"})
BEARER_PREFIX = "Bearer "


async def bearer_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if not header.startswith(BEARER_PREFIX):
        return _unauthorized(request, reason="missing_or_malformed")

    provided = header[len(BEARER_PREFIX) :]
    identity = await request.app.state.identity_resolver.resolve(provided)
    if identity is None:
        return _unauthorized(request, reason="invalid_or_expired_session")

    request.state.user_id = identity.user_id
    request.state.role = identity.role

    # Populate the ContextVar so `tx_for_user(engine)` (no explicit user_id
    # kwarg) picks up the right user inside this request. The try/finally is
    # critical -- Starlette can reuse the same async task for the response
    # phase, and a leaked ContextVar would bleed across requests.
    token = set_current_user_token(identity.user_id)
    try:
        return await call_next(request)
    finally:
        reset_current_user(token)


def current_user_id(request: Request) -> int:
    return request.state.user_id


def current_role(request: Request) -> str:
    return request.state.role


def _unauthorized(request: Request, *, reason: str) -> JSONResponse:
    logger.warning(
        "auth_rejected",
        path=request.url.path,
        method=request.method,
        client=request.client.host if request.client else None,
        reason=reason,
    )
    return JSONResponse({"error": "unauthorized"}, status_code=401)
