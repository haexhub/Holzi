"""End-to-end identity round-trip smoke test against a lifespan-booted app.

This proves a request flows through the entire chain on real Postgres:
    bearer -> SessionResolver -> auth middleware -> current-user ContextVar
    -> route -> tx_for_user -> RLS-filtered query -> response.

It uses the conftest `client` fixture, which is bound to an app booted via
`app_with_pg` (LifespanManager against the per-test Postgres). That lifespan
runs `init_db()` + `ensure_platform_admin_seeded` (platform_admin = user_id=1,
session keyed on `test-token-for-pytest`) and `ensure_personas_backfill(1)`,
which seeds the admin's default persona. We then read that persona back through
the RLS-locked `personas` table as the authenticated admin.

Cross-user RLS isolation is covered separately by `test_rls_cross_user.py`;
this test's job is the full request-path smoke against the booted lifespan.
"""

AUTH = {"Authorization": "Bearer test-token-for-pytest"}


async def test_platform_admin_authenticates_and_reads_own_rls_scoped_data(client):
    # GET an endpoint backed by an RLS-locked personal table, as the seeded
    # admin. The full chain (bearer -> session -> middleware -> ContextVar ->
    # route -> tx_for_user -> RLS query) must yield the admin's own rows.
    r = await client.get("/api/personas", headers=AUTH)
    assert r.status_code == 200
    # GET /api/personas returns a PersonaListResponse: {"personas": [...]}.
    personas = r.json()["personas"]
    # ensure_personas_backfill seeded the admin's default persona under user_id=1.
    assert any(p.get("is_default") for p in personas)


async def test_unauthenticated_request_is_rejected(client):
    # No bearer -> the auth middleware must reject before reaching the route,
    # proving the auth gate is actually in the request path.
    r = await client.get("/api/personas")
    assert r.status_code == 401
