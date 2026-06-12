"""Shared helpers for hermes.routes.*.

Cross-cutting utilities used by multiple route modules. Keep this file
small and focused — only promote helpers here once they have at least
two genuine callers.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from hermes.errors import ErrorCode

MAX_LIST_LIMIT = 500
"""Default upper bound for any list endpoint that accepts a ``limit`` query param.

Negative LIMIT disables LIMIT in SQLite — refuse non-positive values at the
API boundary so an authenticated client can't trigger an unbounded scan.
"""


def validate_limit(limit: int, *, max_limit: int = MAX_LIST_LIMIT) -> int:
    if limit < 1 or limit > max_limit:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.REQUEST_LIMIT_OUT_OF_RANGE.value,
                "params": {"min": 1, "max": max_limit},
            },
        )
    return limit


def http_error(
    status_code: int,
    code: ErrorCode,
    *,
    params: dict[str, Any] | None = None,
) -> HTTPException:
    """Build an ``HTTPException`` with the canonical error envelope.

    Pass ``params=None`` (default) for the bare-string detail shape
    (``detail="<CODE>"``); pass an explicit dict — including ``params={}``
    — for the wrapped shape (``detail={"code": "<CODE>", "params": {...}}``).
    Both shapes are intentional, the frontend handles both via i18n.

    Returns the exception rather than raising so that callers can use
    ``raise http_error(...) from exc`` for chaining.
    """
    if params is None:
        return HTTPException(status_code=status_code, detail=code.value)
    return HTTPException(
        status_code=status_code,
        detail={"code": code.value, "params": params},
    )


def require_sandbox_manager(request: Request) -> Any:
    mgr = request.app.state.sandbox_manager
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorCode.SANDBOX_NOT_CONFIGURED.value,
        )
    return mgr
