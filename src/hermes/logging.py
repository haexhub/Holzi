import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from hermes.config import settings

# Plan 27: secret-looking keys are scrubbed before JSON-rendering so we
# don't leak credentials into the log file or stdout. The same regex is
# applied a second time on `/api/logs` reads so even pre-existing rows
# (written before the processor was in place) come out redacted.
SECRET_KEY_PATTERN = re.compile(
    r"^(api[_-]?key|token|password|secret|authorization)$",
    re.IGNORECASE,
)
REDACTED = "<redacted>"

# Same key vocabulary but as a `key=value` / `key: value` matcher for
# free-form text lines that don't parse as JSON (e.g. stdlib log records
# from uvicorn / httpx). Captures the value up to the next whitespace,
# quote, comma, or end-of-line so URL params and key=value pairs are
# both covered.
SECRET_INLINE_PATTERN = re.compile(
    r"(?P<key>api[_-]?key|token|password|secret|authorization|bearer)"
    r"(?P<sep>\s*[:=]\s*|\s+)"
    r"(?P<val>[^\s,;\"']+)",
    re.IGNORECASE,
)


def redact_secrets(obj: Any) -> Any:
    """Walk a JSON-ish value and replace any value whose key matches
    SECRET_KEY_PATTERN. Lists / nested dicts are walked recursively.
    Non-container leaves are returned unchanged.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and SECRET_KEY_PATTERN.match(k):
                out[k] = REDACTED
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [redact_secrets(v) for v in obj]
    return obj


def redact_secrets_in_text(line: str) -> str:
    """Belt-and-braces redaction for raw log text that doesn't parse as
    a JSON dict — `redact_secrets` only matches keys in structured data,
    so a stdlib record like `Bearer abc123` would otherwise leak. The
    secret-key list mirrors `SECRET_KEY_PATTERN` plus `bearer`."""
    return SECRET_INLINE_PATTERN.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", line
    )


def _redaction_processor(_logger, _method, event_dict):
    """structlog processor that redacts secret-looking keys in the event
    dict before the JSON renderer serialises it."""
    return redact_secrets(event_dict)


def configure_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(stream=sys.stdout)]
    if settings.log_file:
        # Ensure the parent directory exists so first boot in a fresh
        # container (e.g. /var/log/hermes/) doesn't crash on the open().
        log_path = Path(settings.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=settings.log_file_max_bytes,
                backupCount=settings.log_file_backup_count,
                encoding="utf-8",
            )
        )

    # Replace existing handlers so re-running configure_logging() (tests,
    # hot-reload) doesn't multiply them. `close()` releases the underlying
    # stream/FD; just `removeHandler` would leak file descriptors on each
    # rebuild and could keep a rotated log file locked.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        h.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(h)
    root.setLevel(log_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redaction_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


logger: structlog.stdlib.BoundLogger = structlog.get_logger("hermes")
