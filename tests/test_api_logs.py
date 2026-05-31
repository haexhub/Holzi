"""End-to-end tests for `GET /api/logs` (Plan 27).

The logs endpoint tails the structlog file the agent writes to, with a
severity filter and a tail cap, so the Control Center's Logs page can
show recent activity without shelling into the container. Auth-gated.
Secret-looking keys (api_key, token, password, secret, authorization)
are scrubbed on read so even pre-existing log rows can't leak.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client(monkeypatch, tmp_path: Path):
    from hermes import config as hermes_config

    # Default each test to a file path inside tmp_path; tests that need
    # "log_file unset" override this with `None`.
    log_file = tmp_path / "hermes.log"
    monkeypatch.setattr(hermes_config.settings, "log_file", str(log_file))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# ---------------------------------------------------------------------------
# auth + missing config
# ---------------------------------------------------------------------------


async def test_logs_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/logs")
    assert response.status_code == 401


async def test_logs_503_when_log_file_unset(
    monkeypatch, tmp_path: Path
) -> None:
    """If `HERMES_LOG_FILE` is unset, the endpoint reports 503 rather
    than silently returning an empty list."""
    from hermes import config as hermes_config

    monkeypatch.setattr(hermes_config.settings, "log_file", None)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        response = await c.get("/api/logs", headers=AUTH)
    assert response.status_code == 503
    body = response.json()
    assert "HERMES_LOG_FILE" in body["detail"]


async def test_logs_returns_empty_when_file_missing(
    client: httpx.AsyncClient,
) -> None:
    """File path configured but no log file yet → empty list, not 503."""
    response = await client.get("/api/logs", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"rows": []}


# ---------------------------------------------------------------------------
# tail + filter
# ---------------------------------------------------------------------------


async def test_logs_returns_recent_rows(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    _write_rows(
        log_file,
        [
            {"timestamp": "2026-05-30T10:00:00Z", "level": "info", "event": "boot"},
            {"timestamp": "2026-05-30T10:00:01Z", "level": "warning", "event": "slow_query"},
            {"timestamp": "2026-05-30T10:00:02Z", "level": "error", "event": "upstream_timeout"},
        ],
    )
    response = await client.get("/api/logs", headers=AUTH)
    body = response.json()
    assert len(body["rows"]) == 3
    assert [r["event"] for r in body["rows"]] == [
        "boot",
        "slow_query",
        "upstream_timeout",
    ]


async def test_logs_tail_caps_lines(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    _write_rows(
        log_file,
        [{"level": "info", "event": f"e{i}"} for i in range(10)],
    )
    response = await client.get("/api/logs?tail=3", headers=AUTH)
    body = response.json()
    assert len(body["rows"]) == 3
    # Newest-last → last three written.
    assert [r["event"] for r in body["rows"]] == ["e7", "e8", "e9"]


async def test_logs_tail_capped_at_1000(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    _write_rows(
        log_file, [{"level": "info", "event": f"e{i}"} for i in range(50)]
    )
    response = await client.get("/api/logs?tail=99999", headers=AUTH)
    assert response.status_code == 400


async def test_logs_filters_by_min_level(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    _write_rows(
        log_file,
        [
            {"level": "debug", "event": "d"},
            {"level": "info", "event": "i"},
            {"level": "warning", "event": "w"},
            {"level": "error", "event": "e"},
        ],
    )
    response = await client.get("/api/logs?min_level=warning", headers=AUTH)
    body = response.json()
    events = [r["event"] for r in body["rows"]]
    assert events == ["w", "e"]


async def test_logs_rejects_unknown_min_level(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/logs?min_level=fatal", headers=AUTH)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# redaction + malformed lines
# ---------------------------------------------------------------------------


async def test_logs_redacts_secret_looking_keys(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    _write_rows(
        log_file,
        [
            {
                "level": "info",
                "event": "upstream_auth",
                "api_key": "sk-do-not-leak",
                "authorization": "Bearer sk-xxx",
                "token": "abcd",
                "Password": "hunter2",  # case-insensitive match
                "model": "claude-opus-4-7",  # not a secret
            }
        ],
    )
    response = await client.get("/api/logs", headers=AUTH)
    [row] = response.json()["rows"]
    assert row["api_key"] == "<redacted>"
    assert row["authorization"] == "<redacted>"
    assert row["token"] == "<redacted>"
    assert row["Password"] == "<redacted>"
    assert row["model"] == "claude-opus-4-7"


async def test_logs_malformed_line_returns_raw(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    log_file.write_text(
        json.dumps({"level": "info", "event": "ok"})
        + "\nthis-is-not-json\n"
        + json.dumps({"level": "warning", "event": "after"})
        + "\n"
    )
    response = await client.get("/api/logs", headers=AUTH)
    rows = response.json()["rows"]
    assert len(rows) == 3
    assert rows[0]["event"] == "ok"
    assert rows[1] == {"_raw": "this-is-not-json"}
    assert rows[2]["event"] == "after"


async def test_logs_redacts_secret_in_nested_dict(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    log_file = tmp_path / "hermes.log"
    _write_rows(
        log_file,
        [
            {
                "level": "info",
                "event": "deep",
                "context": {"api_key": "sk-deep", "model": "x"},
            }
        ],
    )
    [row] = (await client.get("/api/logs", headers=AUTH)).json()["rows"]
    assert row["context"]["api_key"] == "<redacted>"
    assert row["context"]["model"] == "x"


async def test_logs_redacts_secrets_in_raw_text_fallback(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A non-JSON line that contains an inline secret (e.g. a stdlib log
    record like `authorization=Bearer sk-xxx`) must come out scrubbed —
    redact_secrets only matches JSON keys, so the fallback needs its
    own inline scrubber."""
    log_file = tmp_path / "hermes.log"
    log_file.write_text(
        "uvicorn: GET /x authorization=sk-leak token: abc123 ok\n"
    )
    [row] = (await client.get("/api/logs", headers=AUTH)).json()["rows"]
    assert "sk-leak" not in row["_raw"]
    assert "abc123" not in row["_raw"]
    assert "<redacted>" in row["_raw"]
