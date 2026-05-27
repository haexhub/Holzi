"""Attachment policy + on-disk layout helpers (Plan 11).

Pure logic only — no DB, no HTTP. The repository (`repository/attachments.py`)
owns the metadata rows; the route (`routes/api.py`) owns upload/validation.
Files live inside the per-conversation scratch dir from Plan 01b, so deleting
a conversation (which rmtree's that dir) reclaims every attachment with it.
"""
from __future__ import annotations

from pathlib import Path

from hermes.config import conversation_scratch_root
from hermes.repository.models import Attachment

# 25 MB per file. The cap is the only guard against oversized inlining —
# there is no chunking (out of scope), so a huge text file just fails
# upstream on token count, which is acceptable for v1.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Binary types we store and surface as chips but never read into context.
IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
DOC_TYPES = frozenset({"application/pdf"})

# `application/*` types that are really UTF-8 text and safe to inline. Most
# code/markdown/log files arrive as `text/*` and are caught by the prefix
# check below; these are the common exceptions browsers label otherwise.
TEXTUAL_APP_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/typescript",
        "application/toml",
        "application/x-sh",
        "application/sql",
    }
)


def _normalize(content_type: str) -> str:
    # Strip any "; charset=…" parameter and lowercase.
    return content_type.split(";", 1)[0].strip().lower()


def is_inlineable_text(content_type: str) -> bool:
    """True when the file's bytes should be decoded and fed to the agent
    inline (text/log/markdown/code)."""
    ct = _normalize(content_type)
    return ct.startswith("text/") or ct in TEXTUAL_APP_TYPES


def is_allowed(content_type: str) -> bool:
    """True when the upload is accepted at all. Inlineable text plus the
    stored-only binaries (images, PDF). Everything else is rejected."""
    ct = _normalize(content_type)
    return is_inlineable_text(ct) or ct in IMAGE_TYPES or ct in DOC_TYPES


def safe_display_filename(raw: str | None) -> str:
    """Reduce an uploaded filename to a bare basename for display. The
    on-disk name is a generated token, so this is defence-in-depth /
    cosmetics only — it can never influence the storage path."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in ("", ".", ".."):
        return "file"
    return name[:255]


def attachment_dir(conversation_id: int) -> Path:
    """`{data_dir}/conversations/{id}/attachments/`."""
    return conversation_scratch_root() / str(conversation_id) / "attachments"


def file_path(att: Attachment) -> Path:
    return attachment_dir(att.conversation_id) / att.storage_path


def read_text(att: Attachment) -> str:
    """Decode an inlineable attachment as UTF-8, replacing undecodable
    bytes. Caller is responsible for checking `is_inlineable_text` first."""
    return file_path(att).read_bytes().decode("utf-8", errors="replace")
