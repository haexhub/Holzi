import hmac
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from hermes.config import settings
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
    if not hmac.compare_digest(provided, settings.auth_token):
        return _unauthorized(request, reason="invalid_token")

    return await call_next(request)


def _unauthorized(request: Request, *, reason: str) -> JSONResponse:
    logger.warning(
        "auth_rejected",
        path=request.url.path,
        method=request.method,
        client=request.client.host if request.client else None,
        reason=reason,
    )
    return JSONResponse({"error": "unauthorized"}, status_code=401)
