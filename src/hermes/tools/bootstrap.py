"""Built-in bootstrap tools: persona_update + mark_bootstrap_complete (Plan 37 Task 5).

These tools are used by the bootstrap-first-chat skill during onboarding.
`persona_update` writes fragments to the default persona with author='bootstrap'.
`mark_bootstrap_complete` flips users.bootstrap_completed to 1 (idempotent).
"""
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.errors import ErrorCode
from hermes.repository import personas as personas_repo

# Plan 35 §C1: onboarding writes the admin's (id=1) default persona — the
# bootstrap flag itself is admin-scoped (`mark_bootstrap_complete` updates
# `users.id = 1`), so the persona it edits is the admin's too.
_BOOTSTRAP_USER_ID = 1


def build_bootstrap_tools(db: AsyncEngine) -> list[Tool]:
    return [_persona_update(db), _mark_bootstrap_complete(db)]


def _persona_update(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        soul = args.get("soul")
        identity = args.get("identity")
        agents = args.get("agents")
        if soul is None and identity is None and agents is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY.value,
                    "params": {},
                },
            )
        default = await personas_repo.get_default(db, user_id=_BOOTSTRAP_USER_ID)
        if default is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": ErrorCode.PERSONA_NOT_FOUND.value,
                    "params": {},
                },
            )
        updated = await personas_repo.update(
            db,
            default.id,
            user_id=_BOOTSTRAP_USER_ID,
            soul=soul,
            identity=identity,
            agents=agents,
            history_author="bootstrap",
        )
        if updated is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": ErrorCode.PERSONA_NOT_FOUND.value,
                    "params": {},
                },
            )
        return json.dumps(
            {
                "name": updated.name,
                "soul": updated.soul,
                "identity": updated.identity,
                "agents": updated.agents,
            }
        )

    return Tool(
        name="persona_update",
        description=(
            "Update the default persona's soul / identity / agents "
            "fragments. Use this during onboarding (after bootstrap-"
            "first-chat) or when the user explicitly asks to change "
            "their persona via chat. Every write creates an audit "
            "history row that the user can restore from /settings/"
            "preferences."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "soul": {"type": "string"},
                "identity": {"type": "string"},
                "agents": {"type": "string"},
            },
        },
        handler=handler,
        requires_approval=False,
        source="builtin",
    )


def _mark_bootstrap_complete(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:  # noqa: ARG001
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE users SET bootstrap_completed = true WHERE id = 1")
            )
        return json.dumps({"ok": True})

    return Tool(
        name="mark_bootstrap_complete",
        description=(
            "Flip users.bootstrap_completed to 1. Call this as the "
            "very last action of the bootstrap-first-chat skill, "
            "after persona_update succeeded. Idempotent — calling "
            "twice is harmless."
        ),
        parameters_schema={
            "type": "object",
            "properties": {},
        },
        handler=handler,
        requires_approval=False,
        source="builtin",
    )
