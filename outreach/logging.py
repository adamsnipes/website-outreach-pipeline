"""Logging setup.

Text logging by default, JSON when ``LOG_FORMAT=json`` so that unattended cron /
systemd runs can be shipped somewhere structured without changing code.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

_CONFIGURED = False

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Minimal dependency-free JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human readable formatter that still surfaces structured extras."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
            base = f"{base} | {rendered}"
        return base


def safe_extra(values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Sanitise a mapping for use as logging ``extra=``.

    ``logging`` raises if an extra key collides with a LogRecord attribute --
    ``created``, ``module``, ``name`` and friends -- which is easy to hit when
    passing arbitrary counters through.  Colliding keys are prefixed rather
    than dropped so no information is lost.
    """
    merged = {**(values or {}), **kwargs}
    out: dict[str, Any] = {}
    for key, value in merged.items():
        clean = str(key)
        if clean in _RESERVED or clean.startswith("_"):
            clean = f"x_{clean.lstrip('_')}"
        out[clean] = value
    return out


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Idempotently configure root logging."""
    global _CONFIGURED
    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.environ.get("LOG_FORMAT") or "text").lower()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    if _CONFIGURED:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
        return

    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)

    # Third-party libraries are noisy at DEBUG and have nothing useful to say.
    for noisy in (
        "httpx", "httpcore", "urllib3", "googleapiclient", "google_auth_httplib2",
        "alembic.runtime.plugins",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
