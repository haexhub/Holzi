"""Session identity + logout (Wave C1, Plan 35 §C1).

Two thin endpoints over the resolved bearer session:

    GET  /api/auth/me      → the caller's user_id/role/email + bootstrap flag
    POST /api/auth/logout  → delete the session backing the presented bearer

Both are bearer-gated by the global auth middleware (NOT in PUBLIC_PATHS);
`/me` reads `user_id`/`role` from `request.state` via the auth helpers.
"""
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import BEARER_PREFIX, current_role, current_user_id
from hermes.config import settings
from hermes.db import tx_for_user
from hermes.identity import hash_token
from hermes.schema import sessions, users

router = APIRouter(prefix="/api/auth")


def _db(request: Request) -> AsyncEngine:
    return request.app.state.db


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    uid = current_user_id(request)
    async with _db(request).connect() as conn:
        row = (
            await conn.execute(
                select(users.c.email, users.c.bootstrap_completed).where(
                    users.c.id == uid
                )
            )
        ).first()
    return {
        "user_id": uid,
        "role": current_role(request),
        "email": row.email if row else None,
        "bootstrap_completed": bool(row.bootstrap_completed) if row else False,
    }


@router.post("/logout")
async def logout(request: Request) -> dict[str, Any]:
    """Delete the session backing the presented bearer. Idempotent."""
    header = request.headers.get("authorization", "")
    token = header[len(BEARER_PREFIX) :] if header.startswith(BEARER_PREFIX) else ""
    # The env bootstrap token is infra (re-seeded at startup), not API-revocable;
    # deleting its session would brick the deployment until restart. Real C2
    # (magic-link) sessions are revocable normally.
    if token and hash_token(token) == hash_token(settings.platform_admin_token):
        return {"ok": True}
    # `sessions` is FORCE RLS; a plain `.begin()` runs with app.user_id unset
    # (GUC default '0') so the DELETE would match zero rows and the session
    # would silently survive. Scope it to the resolved user via tx_for_user
    # (the auth middleware set the ContextVar).
    async with tx_for_user(_db(request)) as conn:
        await conn.execute(
            sessions.delete().where(sessions.c.token_hash == hash_token(token))
        )
    return {"ok": True}
