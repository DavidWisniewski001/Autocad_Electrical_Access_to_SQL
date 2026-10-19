"""Structured logging to stderr: ``ts level logger msg key=value``.

Pass key/value context with ``log.info("msg", extra={"kv": {"table": name, "rows": n}})``.
"""

from __future__ import annotations

import logging
import sys
import time

_HANDLER_NAME = "acade2sql-stderr"


class KeyValueFormatter(logging.Formatter):
    """Formats records as ``2026-10-18T20:00:00.123 INFO acade2sql.x message key=value``."""

    converter = time.localtime

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        line = (
            f"{ts}.{int(record.msecs):03d} {record.levelname} {record.name} {record.getMessage()}"
        )
        kv = getattr(record, "kv", None)
        if isinstance(kv, dict) and kv:
            line += " " + " ".join(f"{k}={_fmt(v)}" for k, v in kv.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _fmt(value: object) -> str:
    text = str(value)
    if not text or any(ch.isspace() for ch in text) or '"' in text or "=" in text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def configure_logging(verbose: bool) -> None:
    """Install a single stderr handler on the ``acade2sql`` logger (idempotent)."""
    logger = logging.getLogger("acade2sql")
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
    for handler in list(logger.handlers):
        if handler.get_name() == _HANDLER_NAME:
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(KeyValueFormatter())
    logger.addHandler(handler)
