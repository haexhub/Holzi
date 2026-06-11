"""Task 12: bearer_auth_middleware populates the current-user ContextVar.

The middleware must wrap `call_next` with `set_current_user_token` /
`reset_current_user` so that downstream code can call `tx_for_user(engine)`
without explicitly threading `user_id`.

The end-to-end check below requires the `app_with_pg` fixture from Task 18
(testcontainers Postgres + lifespan + bootstrap admin seed) and a tiny
`/__test/whoami` probe route that returns the active ContextVar value. Until
that fixture lands the test ERRORs with "fixture not found" -- the expected
intermediate state.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.usefixtures("app_with_pg")  # provided by Task 18
async def test_authenticated_request_populates_contextvar(client):
    """A bearer-authenticated GET reaches a probe endpoint that returns the
    active ContextVar value, proving the middleware populated it.
    """
    r = await client.get(
        "/__test/whoami",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert r.status_code == 200
    assert r.json() == {"user_id": 1}
