"""Small secret-safe console logger. Never accept raw exceptions, URLs or payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json

_format = None  # Imported helpers retain legacy plain-text output; CLI selects JSON.
_context = ContextVar("example_log_context", default={})
_ALLOWED = frozenset(("provider", "stage", "callId", "registrationId", "direction", "eventType", "digit"))


def configure_logging(format_name="json"):
    global _format
    if format_name not in ("json", "text"):
        raise ValueError("Unsupported log format")
    _format = format_name


@contextmanager
def log_context(**fields):
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if k in _ALLOWED}})
    try:
        yield
    finally:
        _context.reset(token)


def log(message, *, flush=True, level="INFO", **fields):
    """Messages must be static or sanitized via safe_error, never provider bodies."""
    if _format is None:
        print(message, flush=flush)
        return
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             "level": level, "message": str(message), **_context.get(),
             **{k: v for k, v in fields.items() if k in _ALLOWED}}
    if _format == "json":
        print(json.dumps(entry, ensure_ascii=False), flush=flush)
    else:
        context = " ".join(f"{k}={v}" for k, v in entry.items() if k in _ALLOWED)
        print(f"{entry['timestamp']} {level} {context} {message}".strip(), flush=flush)
