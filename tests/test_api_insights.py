"""End-to-end tests for `GET /api/insights` (Plan 27).

The insights endpoint surfaces aggregates over the `agent_runs` table so
the Control Center's Insights page can render daily usage, per-model
splits, and status counts without the frontend having to walk every row
itself. Pure read path — the table itself is unchanged.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import conversations as conversations_repo
from hermes.repository import runs as runs_repo

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


async def _seed_run(
    *,
    conversation_id: int,
    run_id: str,
    started_at: int,
    model: str,
    status: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error_code: str | None = None,
) -> None:
    """Insert + finalize a fake agent_runs row at `started_at`."""
    db = app.state.db
    await runs_repo.insert(
        db,
        run_id=run_id,
        conversation_id=conversation_id,
        channel="web",
        model=model,
        started_at=started_at,
    )
    if status != "running":
        await runs_repo.finalize(
            db,
            run_id,
            status=status,
            finished_at=started_at + 1,
            error_code=error_code,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _utc_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# auth + shape
# ---------------------------------------------------------------------------


async def test_insights_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/insights")
    assert response.status_code == 401


async def test_insights_rejects_unknown_period(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/insights?period=99d", headers=AUTH)
    assert response.status_code == 400
    body = response.json()
    assert body["detail"]["code"] == "REQUEST_INVALID_PERIOD"
    assert "7d" in body["detail"]["params"]["allowed"]


async def test_insights_default_period_is_7d(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/insights", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "7d"
    assert "totals" in body
    assert "series" in body
    assert "by_model" in body
    assert "by_status" in body


async def test_insights_empty_window_returns_zero_filled_series(
    client: httpx.AsyncClient,
) -> None:
    """With no rows, every bucket exists but is empty — the chart isn't blank."""
    response = await client.get("/api/insights?period=7d", headers=AUTH)
    body = response.json()
    assert body["totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "runs": 0,
        "errors": 0,
    }
    # 7 daily buckets (today plus the previous 6 UTC days).
    assert len(body["series"]) == 7
    for bucket in body["series"]:
        assert bucket["input_tokens"] == 0
        assert bucket["output_tokens"] == 0
        assert bucket["runs"] == 0
    assert body["by_model"] == []
    assert body["by_status"] == {
        "success": 0,
        "error": 0,
        "cancelled": 0,
        "running": 0,
    }


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


async def test_insights_aggregates_totals_and_buckets(
    client: httpx.AsyncClient,
) -> None:
    conv = await conversations_repo.create(app.state.db, user_id=1, channel="web")
    now = int(time.time())
    # Three runs today (success/success/error), one yesterday (success).
    await _seed_run(
        conversation_id=conv.id,
        run_id="r1",
        started_at=now,
        model="claude-opus-4-7",
        status="success",
        input_tokens=100,
        output_tokens=200,
    )
    await _seed_run(
        conversation_id=conv.id,
        run_id="r2",
        started_at=now,
        model="claude-opus-4-7",
        status="success",
        input_tokens=50,
        output_tokens=75,
    )
    await _seed_run(
        conversation_id=conv.id,
        run_id="r3",
        started_at=now,
        model="claude-opus-4-7",
        status="error",
        error_code="upstream_timeout",
    )
    await _seed_run(
        conversation_id=conv.id,
        run_id="r4",
        started_at=now - 86400,
        model="gpt-4o",
        status="success",
        input_tokens=10,
        output_tokens=20,
    )

    response = await client.get("/api/insights?period=7d", headers=AUTH)
    body = response.json()

    # Totals
    assert body["totals"]["runs"] == 4
    assert body["totals"]["errors"] == 1
    assert body["totals"]["input_tokens"] == 160  # 100 + 50 + 10 (error row has None)
    assert body["totals"]["output_tokens"] == 295  # 200 + 75 + 20

    # Series — today's bucket has the three same-day rows.
    today = _utc_date(now)
    yesterday = _utc_date(now - 86400)
    series_by_bucket = {b["bucket"]: b for b in body["series"]}
    assert series_by_bucket[today]["runs"] == 3
    assert series_by_bucket[today]["input_tokens"] == 150
    assert series_by_bucket[today]["output_tokens"] == 275
    assert series_by_bucket[yesterday]["runs"] == 1
    assert series_by_bucket[yesterday]["input_tokens"] == 10

    # by_model split
    by_model = {row["model"]: row for row in body["by_model"]}
    assert by_model["claude-opus-4-7"]["runs"] == 3
    assert by_model["claude-opus-4-7"]["errors"] == 1
    assert by_model["claude-opus-4-7"]["input_tokens"] == 150
    assert by_model["gpt-4o"]["runs"] == 1
    assert by_model["gpt-4o"]["errors"] == 0

    # by_status counts
    assert body["by_status"]["success"] == 3
    assert body["by_status"]["error"] == 1
    assert body["by_status"]["cancelled"] == 0
    assert body["by_status"]["running"] == 0


async def test_insights_window_excludes_old_rows(
    client: httpx.AsyncClient,
) -> None:
    """A row from 10 days ago is invisible to a 7d window."""
    conv = await conversations_repo.create(app.state.db, user_id=1, channel="web")
    now = int(time.time())
    await _seed_run(
        conversation_id=conv.id,
        run_id="recent",
        started_at=now,
        model="m",
        status="success",
        input_tokens=5,
        output_tokens=5,
    )
    await _seed_run(
        conversation_id=conv.id,
        run_id="stale",
        started_at=now - 10 * 86400,
        model="m",
        status="success",
        input_tokens=999,
        output_tokens=999,
    )

    response = await client.get("/api/insights?period=7d", headers=AUTH)
    body = response.json()
    assert body["totals"]["runs"] == 1
    assert body["totals"]["input_tokens"] == 5
    # Stale row still counted by 30d window.
    response = await client.get("/api/insights?period=30d", headers=AUTH)
    body = response.json()
    assert body["totals"]["runs"] == 2
    assert body["totals"]["input_tokens"] == 1004


async def test_insights_24h_period_returns_short_series(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/insights?period=24h", headers=AUTH)
    body = response.json()
    assert body["period"] == "24h"
    # A 24h window spans at most two UTC days (today and yesterday).
    assert 1 <= len(body["series"]) <= 2


async def test_insights_30d_period_returns_30_buckets(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/insights?period=30d", headers=AUTH)
    body = response.json()
    assert body["period"] == "30d"
    assert len(body["series"]) == 30


async def test_insights_running_rows_count_in_totals_not_errors(
    client: httpx.AsyncClient,
) -> None:
    conv = await conversations_repo.create(app.state.db, user_id=1, channel="web")
    now = int(time.time())
    # An in-flight run with no tokens yet.
    await _seed_run(
        conversation_id=conv.id,
        run_id="live",
        started_at=now,
        model="m",
        status="running",
    )
    response = await client.get("/api/insights?period=7d", headers=AUTH)
    body = response.json()
    assert body["totals"]["runs"] == 1
    assert body["totals"]["errors"] == 0
    assert body["by_status"]["running"] == 1
