"""Tests for the read_user_guide tool."""
import json
from pathlib import Path

import pytest

from hermes import capabilities
from hermes.tools.user_guide import build_user_guide_tools


def _tool():
    [tool] = build_user_guide_tools()
    return tool


@pytest.fixture
def fake_user_guide(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(capabilities, "USER_GUIDE_DIR", tmp_path)
    monkeypatch.setattr(
        capabilities, "CAPABILITY_INDEX_PATH", tmp_path / "CAPABILITIES.md"
    )
    return tmp_path


@pytest.mark.asyncio
async def test_known_topic_returns_content(fake_user_guide: Path) -> None:
    (fake_user_guide / "workspaces.md").write_text(
        "# Workspaces\n\nHello.", encoding="utf-8"
    )

    result = json.loads(await _tool().handler({"topic": "workspaces"}))

    assert result["topic"] == "workspaces"
    assert "Hello" in result["content"]


@pytest.mark.asyncio
async def test_unknown_topic_returns_available_topics(
    fake_user_guide: Path,
) -> None:
    (fake_user_guide / "messengers.md").write_text("ok", encoding="utf-8")
    (fake_user_guide / "memory.md").write_text("ok", encoding="utf-8")
    (fake_user_guide / "CAPABILITIES.md").write_text("index", encoding="utf-8")

    result = json.loads(await _tool().handler({"topic": "nonexistent"}))

    assert "error" in result
    # CAPABILITIES is the index itself and must not be advertised as a topic.
    assert result["available_topics"] == ["memory", "messengers"]


@pytest.mark.asyncio
async def test_topic_is_case_insensitive(fake_user_guide: Path) -> None:
    (fake_user_guide / "tasks.md").write_text("body", encoding="utf-8")

    result = json.loads(await _tool().handler({"topic": "TASKS"}))

    assert result["topic"] == "tasks"
    assert result["content"] == "body"


@pytest.mark.asyncio
async def test_path_traversal_and_empty_topic_are_rejected(
    fake_user_guide: Path,
) -> None:
    # Even if a file matching the traversal target exists, the tool must
    # refuse to read it — only basenames without separators are allowed.
    (fake_user_guide.parent / "secret.md").write_text("nope", encoding="utf-8")

    for evil in ["../secret", "foo/bar", "a\\b", "", "  "]:
        result = json.loads(await _tool().handler({"topic": evil}))
        assert "error" in result, f"expected error for topic={evil!r}"
