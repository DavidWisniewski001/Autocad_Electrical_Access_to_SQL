"""Per-table write modes: ``full`` (drop + create + load), ``append`` and ``sync`` (upsert).

Each ``run_*`` function fills a :class:`TableResult` in place and raises on failure; the caller
(:func:`acade2sql.converter.convert`) turns exceptions into ``status="error"``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from sqlalchemy.engine import URL

from acade2sql.errors import Acade2SqlError, SyncKeyError, WriteError
from acade2sql.keys import NO_KEY_MESSAGE, KeySpec, key_for
from acade2sql.readers import Reader
from acade2sql.redact import redact
from acade2sql.schema import TableInfo
from acade2sql.typemap import is_known
from acade2sql.validate import Status, TableResult, table_checksum
from acade2sql.writers import SqlAlchemyWriter
from acade2sql.writers.sqlalchemy_writer import Mode

log = logging.getLogger(__name__)

NoKey = Literal["error", "replace"]
APPEND_CHECKSUM_WARNING = "checksum not computed in append mode"

_T = TypeVar("_T")


@dataclass(frozen=True)
class RunOptions:
    """Options shared by every table of one run."""

    mode: Mode = "full"
    batch_size: int = 5000
    checksum: bool = False
    keys: KeySpec = field(default_factory=KeySpec)
    no_key: NoKey = "error"
    delete_missing: bool = False


# -- source-side error tracking -----------------------------------------------------------------


class ReadFailed(Exception):
    """The source side (Access reader) raised ``cause``."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class SourceBatches:
    """Iterate the reader's batches, remembering an exception raised by the source."""

    def __init__(self, open_batches: Callable[[], Iterable[list[tuple]]]) -> None:
        self._open = open_batches
        self.error: BaseException | None = None

    def __iter__(self) -> Iterator[list[tuple]]:
        try:
            yield from self._open()
        except Exception as exc:
            self.error = exc
            raise


def read(fn: Callable[[], _T]) -> _T:
    """Call a source-side function, marking its exception as a read failure."""
    try:
        return fn()
    except Exception as exc:
        raise ReadFailed(exc) from exc


def load(write: Callable[[SourceBatches], _T], reader: Reader, info: TableInfo, size: int) -> _T:
    """Run ``write`` over the source batches; a source failure is reported as ``read failed``."""
    source = SourceBatches(lambda: reader.iter_rows(info.name, size))
    try:
        return write(source)
    except Exception as exc:
        if source.error is not None:
            raise ReadFailed(source.error) from exc
        raise


def error_text(exc: BaseException, url: URL | str | None) -> str:
    """``read failed: ...`` or ``write failed: ...`` with secrets redacted."""
    if isinstance(exc, ReadFailed):
        cause = exc.cause
        return f"read failed: {type(cause).__name__}: {redact(str(cause), url)}"
    if isinstance(exc, WriteError):
        return f"write failed: {exc.detail}"
    if isinstance(exc, Acade2SqlError):
        return f"write failed: {redact(str(exc), url)}"
    return f"write failed: {type(exc).__name__}: {redact(str(exc), url)}"


def unmapped_type_warnings(info: TableInfo) -> list[str]:
    """One warning per column whose Access type has no TYPE_MAP entry (stored as text)."""
    return [
        f"unmapped type {c.access_type} in column {c.name} stored as text"
        for c in sorted(info.columns, key=lambda c: c.ordinal)
        if not is_known(c.access_type)
    ]


def _source_checksum(reader: Reader, info: TableInfo, size: int) -> str:
    return read(lambda: table_checksum(reader.iter_rows(info.name, size), info.column_types))


def _target_checksum(rows: Iterable[list[tuple]], info: TableInfo) -> str:
    return table_checksum(rows, info.column_types)


def _status(ok: bool) -> Status:
    return "pass" if ok else "fail"


# -- modes --------------------------------------------------------------------------------------


def run_full(
    reader: Reader, writer: SqlAlchemyWriter, info: TableInfo, opts: RunOptions, res: TableResult
) -> int:
    """Drop + create + load; pass when counts (and checksums, if requested) match."""
    res.source_rows = read(lambda: reader.count_rows(info.name))
    writer.create_table(info)
    written = load(lambda b: writer.write_batches(info, b), reader, info, opts.batch_size)
    res.target_rows = writer.count_rows(info.name)
    if opts.checksum:
        src = _source_checksum(reader, info, opts.batch_size)
        res.checksum_match = src == _target_checksum(
            writer.read_rows(info.name, opts.batch_size), info
        )
    res.status = _status(res.source_rows == res.target_rows and res.checksum_match is not False)
    return written


def run_append(
    reader: Reader, writer: SqlAlchemyWriter, info: TableInfo, opts: RunOptions, res: TableResult
) -> int:
    """Insert every source row into the (possibly new) table; never drops anything."""
    res.source_rows = read(lambda: reader.count_rows(info.name))
    created = writer.ensure_table(info)
    res.target_rows_before = 0 if created else writer.count_rows(info.name)
    written = load(lambda b: writer.write_batches(info, b), reader, info, opts.batch_size)
    res.inserted = written
    res.target_rows = writer.count_rows(info.name)
    if opts.checksum:
        res.warnings.append(APPEND_CHECKSUM_WARNING)
    res.status = _status(res.target_rows == res.target_rows_before + res.source_rows)
    return written


def run_sync(
    reader: Reader, writer: SqlAlchemyWriter, info: TableInfo, opts: RunOptions, res: TableResult
) -> int:
    """Upsert by key (or replace all rows for a keyless table with ``no_key="replace"``)."""
    key = key_for(info, opts.keys)
    if key is None and opts.no_key == "error":
        raise SyncKeyError(NO_KEY_MESSAGE)
    res.key = list(key) if key else None
    res.source_rows = read(lambda: reader.count_rows(info.name))
    writer.ensure_table(info)
    if key is None:
        return _replace(reader, writer, info, opts, res)
    with writer.sync(info, key) as session:
        counts = load(lambda b: session.load(b, opts.delete_missing), reader, info, opts.batch_size)
        in_source = session.count_matching()
        res.target_rows = session.count_all()
        if opts.checksum:
            src = _source_checksum(reader, info, opts.batch_size)
            res.checksum_match = src == _target_checksum(
                session.read_matching(opts.batch_size), info
            )
    res.inserted, res.updated = counts.inserted, counts.updated
    res.deleted, res.unchanged = counts.deleted, counts.unchanged
    res.extra_rows = res.target_rows - in_source
    ok = (
        in_source == res.source_rows
        and res.checksum_match is not False
        and (not opts.delete_missing or res.target_rows == res.source_rows)
    )
    res.status = _status(ok)
    return counts.inserted + counts.updated


def _replace(
    reader: Reader, writer: SqlAlchemyWriter, info: TableInfo, opts: RunOptions, res: TableResult
) -> int:
    deleted, inserted = load(lambda b: writer.replace_rows(info, b), reader, info, opts.batch_size)
    res.deleted, res.inserted, res.updated, res.unchanged = deleted, inserted, 0, 0
    res.target_rows = writer.count_rows(info.name)
    res.extra_rows = 0
    if opts.checksum:
        src = _source_checksum(reader, info, opts.batch_size)
        res.checksum_match = src == _target_checksum(
            writer.read_rows(info.name, opts.batch_size), info
        )
    res.status = _status(res.target_rows == res.source_rows and res.checksum_match is not False)
    return inserted


RUNNERS = {"full": run_full, "append": run_append, "sync": run_sync}
