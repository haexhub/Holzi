"""Read-only workspace browser (Plan 12) + write / git endpoints (Plans 13/24).

Roots = active rows in the `workspaces` table (Plan 25-A made it the
source of truth). `HERMES_WORKSPACE_ROOTS` survives only as the
boot-time backfill in `main.py` lifespan — every request-time
membership check goes through `_active_root_slugs(db)`, so workspaces
created via `POST /api/workspaces` are usable without a restart.

Tree and file reads are served by spinning up (or reusing) the matching
workspace sandbox and reading from its `/workspace` volume — the agent
container never touches the host filesystem here.

Path discipline: everything coming over the wire is a POSIX-style path
*relative* to a workspace root, with no leading `/` and no traversal
segments. The helpers below normalise the inputs and refuse anything that
would break out of `/workspace`.

This package was split out of a single 1.7k-LoC `workspace.py` for the
>500-LoC rule; the public surface is unchanged — `main.py` still imports
`router` from `hermes.routes.workspace`."""

from __future__ import annotations

from fastapi import APIRouter

from . import browser, git_inspect, git_mutate, writer

router = APIRouter(prefix="/api/workspace")
router.include_router(browser.router)
router.include_router(writer.router)
router.include_router(git_inspect.router)
router.include_router(git_mutate.router)

__all__ = ["router"]
