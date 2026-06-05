"""GET /api/insights — aggregates over `agent_runs` for the Control Center.

Backs the Insights page (Plan 27). Pure read path over the persistent
`agent_runs` table: total runs / errors / token usage, daily series for
the chart, per-model split, and status counts.

For `7d`/`30d` the window is anchored to UTC day boundaries: SQL cutoff
and the rendered bucket labels are derived from the same `_utc_today()`
snapshot, so totals/by_model/by_status never include rows that the
`series` view drops as out-of-window. `24h` is the one rolling window —
two UTC bucket labels straddle the 24h boundary by design.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.errors import ErrorCode
from hermes.repository import runs as runs_repo

router = APIRouter(prefix="/api/insights", tags=["insights"])

Period = Literal["24h", "7d", "30d"]

# 24h windows can straddle two UTC days; 7d / 30d always render that many
# buckets (the most recent N days ending today, inclusive).
_PERIOD_BUCKETS: dict[Period, int] = {
    "24h": 2,
    "7d": 7,
    "30d": 30,
}


class TotalsResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    runs: int
    errors: int


class DailyBucket(BaseModel):
    bucket: str  # YYYY-MM-DD (UTC)
    input_tokens: int
    output_tokens: int
    runs: int


class ModelBreakdown(BaseModel):
    model: str
    runs: int
    input_tokens: int
    output_tokens: int
    errors: int


class StatusCounts(BaseModel):
    success: int
    error: int
    cancelled: int
    running: int


class InsightsResponse(BaseModel):
    period: Period
    totals: TotalsResponse
    series: list[DailyBucket]
    by_model: list[ModelBreakdown]
    by_status: StatusCounts


def _utc_today() -> datetime:
    return datetime.now(tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _bucket_dates(period: Period, today: datetime) -> list[str]:
    """Daily UTC bucket labels for the window, oldest-first. Takes
    `today` as a parameter so the caller can pin SQL cutoff and labels
    to the same UTC boundary."""
    count = _PERIOD_BUCKETS[period]
    return [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(count - 1, -1, -1)
    ]


def _zero_fill_series(
    period: Period, today: datetime, observed: list[dict]
) -> list[DailyBucket]:
    by_bucket = {b["bucket"]: b for b in observed}
    out: list[DailyBucket] = []
    for label in _bucket_dates(period, today):
        row = by_bucket.get(label)
        if row is None:
            out.append(
                DailyBucket(
                    bucket=label, input_tokens=0, output_tokens=0, runs=0
                )
            )
        else:
            out.append(
                DailyBucket(
                    bucket=label,
                    input_tokens=int(row["input_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                    runs=int(row["runs"]),
                )
            )
    return out


def _since_ts(period: Period, today: datetime) -> int:
    """SQL cutoff for a given period, in epoch seconds.

    - 24h: now - 24h (rolling, can straddle two UTC days).
    - 7d / 30d: the oldest rendered UTC bucket's 00:00, so SQL and
      labels stay in sync; otherwise rows on the partial oldest UTC day
      counted in totals/by_model/by_status would be dropped from series.
    """
    if period == "24h":
        return int(time.time()) - 24 * 3600
    bucket_count = _PERIOD_BUCKETS[period]
    oldest_bucket_start = today - timedelta(days=bucket_count - 1)
    return int(oldest_bucket_start.timestamp())


@router.get("", response_model=InsightsResponse)
async def api_insights(
    request: Request,
    period: str = "7d",
) -> InsightsResponse:
    # Reject unknown periods at the boundary with a 400 (matches /api/runs
    # and /api/tasks); a typed `Literal` parameter would 422 via FastAPI's
    # validator, which the rest of the API surface doesn't do.
    if period not in _PERIOD_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.REQUEST_INVALID_PERIOD.value,
                "params": {"allowed": sorted(_PERIOD_BUCKETS)},
            },
        )
    db: AsyncEngine = request.app.state.db
    today = _utc_today()
    since_ts = _since_ts(period, today)
    totals = await runs_repo.aggregate_totals(db, since_ts=since_ts)
    raw_series = await runs_repo.aggregate_by_day(db, since_ts=since_ts)
    by_model = await runs_repo.aggregate_by_model(db, since_ts=since_ts)
    by_status = await runs_repo.aggregate_by_status(db, since_ts=since_ts)
    return InsightsResponse(
        period=period,
        totals=TotalsResponse(**totals),
        series=_zero_fill_series(period, today, raw_series),
        by_model=[
            ModelBreakdown(
                model=str(row["model"]),
                runs=int(row["runs"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                errors=int(row["errors"]),
            )
            for row in by_model
        ],
        by_status=StatusCounts(**by_status),
    )
