"""Background jobs for the GUI: one worker thread, results posted to a queue as :class:`Message`.

Tk is not thread-safe, so the worker never touches a widget: every job reports through
``post(Message)`` and the window drains the queue on its own (main) thread. Nothing here imports
tkinter, so the job functions and the message flow can be tested without a display.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from acade2sql.errors import Acade2SqlError
from acade2sql.logging_setup import KeyValueFormatter
from acade2sql.redact import redact
from acade2sql.validate import Report, TableResult

log = logging.getLogger(__name__)

# Message kinds posted by the worker.
LOG = "log"  # data: str (one formatted, redacted log line)
TABLE_STARTED = "table_started"  # data: (name, index, total)
TABLE_DONE = "table_done"  # data: (TableResult, index, total)
REPORT = "report"  # data: Report (convert / dry run finished)
DOCTOR = "doctor"  # data: (exit_code, text)
TABLES = "tables"  # data: list[str] (source table names)
ERROR = "error"  # data: str (redacted message)
FINISHED = "finished"  # data: (job_name, cancelled: bool); always the last message of a job


@dataclass(frozen=True)
class Message:
    kind: str
    data: Any = None


Post = Callable[[Message], None]


class JobContext:
    """What a job function gets: ``post`` for messages, ``cancelled()`` for the Cancel button
    and ``secret_url`` (the target URL, used only to redact its secrets from messages)."""

    def __init__(self, post: Post, cancel_event: threading.Event, secret_url: str | None) -> None:
        self.post = post
        self._cancel = cancel_event
        self.secret_url = secret_url

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def redact(self, text: str) -> str:
        return redact(text, self.secret_url)


Job = Callable[[JobContext], None]


def error_message(exc: BaseException, secret_url: str | None) -> str:
    """User-facing, redacted text for an exception raised by a job."""
    text = str(exc) if isinstance(exc, Acade2SqlError) else f"{type(exc).__name__}: {exc}"
    return redact(text, secret_url)


class Worker:
    """Runs one job at a time in a daemon thread."""

    def __init__(self, post: Post) -> None:
        self._post = post
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.current: str | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, name: str, job: Job, *, secret_url: str | None = None) -> bool:
        """Start ``job`` in the background; False (nothing started) while another job runs."""
        if self.busy:
            return False
        self._cancel = threading.Event()
        self.current = name
        ctx = JobContext(self._post, self._cancel, secret_url)
        self._thread = threading.Thread(
            target=self._run, args=(name, job, ctx), name=f"acade2sql-gui-{name}", daemon=True
        )
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, name: str, job: Job, ctx: JobContext) -> None:
        try:
            job(ctx)
        except Exception as exc:
            log.debug("job failed: %s", ctx.redact(traceback.format_exc()))
            ctx.post(Message(ERROR, error_message(exc, ctx.secret_url)))
        finally:
            ctx.post(Message(FINISHED, (name, ctx.cancelled())))


# -- jobs ----------------------------------------------------------------------------------------


def convert_job(kwargs: dict[str, Any], convert: Callable[..., Report] | None = None) -> Job:
    """Job running ``convert(**kwargs)`` with progress / table-started / cancel hooks."""

    def job(ctx: JobContext) -> None:
        run = convert
        if run is None:
            from acade2sql.converter import convert as run

        def started(name: str, index: int, total: int) -> None:
            ctx.post(Message(TABLE_STARTED, (name, index, total)))

        def progress(res: TableResult, index: int, total: int) -> None:
            ctx.post(Message(TABLE_DONE, (res, index, total)))

        report = run(**kwargs, progress=progress, table_started=started, cancel=ctx.cancelled)
        ctx.post(Message(REPORT, report))

    return job


def doctor_job(run: Callable[[io.StringIO], int] | None = None) -> Job:
    """Job running the ``doctor`` checks; posts ``(exit_code, text)``."""

    def job(ctx: JobContext) -> None:
        runner = run
        if runner is None:
            from acade2sql.doctor import run as runner
        buf = io.StringIO()
        rc = runner(buf)
        ctx.post(Message(DOCTOR, (rc, buf.getvalue())))

    return job


def list_tables_job(source: str, *, reader: str = "auto", copy_to_temp: bool = False) -> Job:
    """Job listing the source's tables (the ``inspect`` reader API, no row counts)."""

    def job(ctx: JobContext) -> None:
        from acade2sql.readers import open_reader

        with open_reader(source, copy_to_temp=copy_to_temp, reader=reader) as r:  # type: ignore[arg-type]
            names = list(r.list_tables())
        ctx.post(Message(TABLES, names))

    return job


# -- logging ---------------------------------------------------------------------------------------


class QueueLogHandler(logging.Handler):
    """Forwards ``acade2sql`` log records to the GUI as redacted :data:`LOG` messages.

    ``secret_url()`` returns the URL of the job in progress (or None); its secrets are removed
    from every line, on top of the structural masking of :func:`acade2sql.redact.redact`.
    """

    def __init__(self, post: Post, secret_url: Callable[[], str | None] = lambda: None) -> None:
        super().__init__()
        self._post = post
        self._secret_url = secret_url
        self.setFormatter(KeyValueFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = redact(self.format(record), self._secret_url())
            self._post(Message(LOG, text))
        except Exception:  # never let logging break a conversion
            self.handleError(record)


def drain(q: queue.Queue[Message], limit: int = 500) -> list[Message]:
    """Up to ``limit`` queued messages without blocking (the window calls this on a timer)."""
    out: list[Message] = []
    while len(out) < limit:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out
