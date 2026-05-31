"""GET /api/logs — tail the structlog file for the Control Center.

The agent's stdout/file handler writes JSON rows (structlog
JSONRenderer). This endpoint reads the last N lines from
`settings.log_file`, decodes each as JSON, filters by severity, and
applies a defensive redaction pass before returning. Pure read path.

Why a second redaction pass: the structlog redaction processor catches
rows written *after* it was wired in, but rows from an older deployment
might be on disk without the scrub. Redacting again on read keeps the
endpoint safe regardless of how old the file is.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes.config import settings
from hermes.logging import redact_secrets, redact_secrets_in_text

router = APIRouter(prefix="/api/logs", tags=["logs"])

# Cap matches the largest size we want to ship over the wire on a poll.
MAX_TAIL = 1000

# `min_level` query → numeric severity used to filter rows. Mirrors
# Python `logging` levels; the structlog `add_log_level` processor writes
# the lowercase string into each row.
_LEVELS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}


class LogsResponse(BaseModel):
    rows: list[dict[str, Any]]


def _tail_lines(path: Path, tail: int) -> list[str]:
    """Read the last `tail` lines from `path` without slurping the whole
    file. Walks the file from the end in chunks until enough newlines
    have been seen."""
    if tail <= 0:
        return []
    chunk_size = 8192
    data = b""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and data.count(b"\n") <= tail:
            read = min(chunk_size, pos)
            pos -= read
            f.seek(pos)
            data = f.read(read) + data
    # Split, drop trailing blank line, keep only last `tail` entries.
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-tail:]


def _parse_row(line: str) -> dict[str, Any]:
    """Decode a single JSON row. Malformed lines pass through as
    `{"_raw": "..."}` so a single bad write doesn't break the tail. The
    raw fallback runs through `redact_secrets_in_text` because
    `redact_secrets` only scrubs JSON keys — a stdlib record like
    `Bearer abc123` would otherwise leak through verbatim."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {"_raw": redact_secrets_in_text(line)}
    if not isinstance(obj, dict):
        return {"_raw": redact_secrets_in_text(line)}
    return obj


@router.get("", response_model=LogsResponse)
async def api_logs(
    tail: int = 200,
    min_level: str = "info",
) -> LogsResponse:
    if tail <= 0 or tail > MAX_TAIL:
        raise HTTPException(
            status_code=400,
            detail=f"tail must be between 1 and {MAX_TAIL}",
        )
    if min_level not in _LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"min_level must be one of {sorted(_LEVELS)}",
        )
    if not settings.log_file:
        raise HTTPException(
            status_code=503,
            detail=(
                "log tailing disabled — set HERMES_LOG_FILE to enable the "
                "rotating file handler"
            ),
        )
    path = Path(settings.log_file)
    if not path.exists():
        return LogsResponse(rows=[])

    threshold = _LEVELS[min_level]
    lines = _tail_lines(path, tail)
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        row = _parse_row(line)
        level = row.get("level")
        if isinstance(level, str):
            level_num = _LEVELS.get(level.lower(), 0)
            if level_num < threshold:
                continue
        # Belt-and-braces: scrub even rows that were already serialised by
        # the in-process redaction processor in case the file pre-dates it.
        rows.append(redact_secrets(row))
    return LogsResponse(rows=rows)
