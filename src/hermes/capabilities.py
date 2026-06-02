"""Capability index + user-guide loader.

The capability index is a short markdown file at
`docs/user-guide/CAPABILITIES.md` that is injected into Hermes's
system prompt at runtime so the agent knows what user-facing features
Holzi exposes. Detail topic files live next to it as
`docs/user-guide/<topic>.md` and are loaded on demand by the
`read_user_guide` tool.

This module is the single source of truth for the on-disk layout, so
both the prompt resolver and the tool reference the same paths.
"""
from pathlib import Path
from typing import Final

_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

USER_GUIDE_DIR: Path = _PROJECT_ROOT / "docs" / "user-guide"
CAPABILITY_INDEX_PATH: Path = USER_GUIDE_DIR / "CAPABILITIES.md"


def load_capability_index() -> str:
    """Return the capability index markdown, or empty string if missing.

    A missing file is treated as "no index" rather than an error so
    dev/test setups without the docs directory still boot cleanly.
    """
    try:
        return CAPABILITY_INDEX_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def read_topic(topic: str) -> str | None:
    """Read `<USER_GUIDE_DIR>/<topic>.md` and return its contents.

    Returns None when the topic doesn't resolve to an existing file.
    Topic names containing path separators or parent-dir traversal are
    rejected so the tool can only surface curated docs, not arbitrary
    disk paths.
    """
    if not topic or "/" in topic or "\\" in topic or ".." in topic:
        return None
    path = USER_GUIDE_DIR / f"{topic}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
