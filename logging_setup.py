"""Structured logging + per-request correlation IDs.

One ``configure_logging`` call wires the root logger to emit either human
readable lines or JSON (``LOG_JSON=1``), both carrying a ``request_id`` pulled
from a :class:`contextvars.ContextVar`. Middleware sets that var per request so
every log line emitted while handling a request is automatically correlated —
no need to thread an id through call signatures.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar

import config

# Set by the request-id middleware; read by the log formatter.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"


def configure_logging() -> logging.Logger:
    """Idempotently configure the root logger and return the app logger."""
    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)

    # Reset handlers so re-configuration (tests, reload) doesn't duplicate lines.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(_JsonFormatter() if config.LOG_JSON else logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)

    # Quiet noisy third parties a notch.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    return logging.getLogger("phantom")


def get_logger(name: str = "phantom") -> logging.Logger:
    return logging.getLogger(name)
