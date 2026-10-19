"""Structured logging to stderr: ``ts level logger msg key=value``.

Pass key/value context with ``log.info("msg", extra={"kv": {"table": name, "rows": n}})``.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import TextIO

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


class CurrentStderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """StreamHandler that always writes to the *current* ``sys.stderr`` (looked up at emit time),
    so redirected or captured stderr (pytest capsys, contextlib.redirect_stderr) is honoured."""

    def __init__(self) -> None:
        super().__init__()

    @property  # type: ignore[override]
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: object) -> None:  # StreamHandler.__init__/setStream assign it
        pass


def configure_logging(verbose: bool) -> None:
    """Route the ``acade2sql`` logger to stderr. Idempotent: never stacks handlers.

    Sets the level (DEBUG if ``verbose`` else WARNING) and ``propagate=False`` so records are not
    duplicated by root handlers. Without this call the package adds no handler of its own.
    """
    logger = logging.getLogger("acade2sql")
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
    logger.propagate = False
    ours = [h for h in logger.handlers if h.get_name() == _HANDLER_NAME]
    for extra in ours[1:]:
        logger.removeHandler(extra)
    if ours and isinstance(ours[0], CurrentStderrHandler):
        return
    if ours:
        logger.removeHandler(ours[0])
    handler = CurrentStderrHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(KeyValueFormatter())
    logger.addHandler(handler)
