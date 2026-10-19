"""Public conversion API: Access database -> SQLAlchemy target, with per-table validation."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Literal, TypeVar

from sqlalchemy.engine import URL

from acade2sql.errors import TableNotFoundError, WriteError
from acade2sql.readers import Reader, open_reader
from acade2sql.redact import redact
from acade2sql.schema import TableInfo
from acade2sql.typemap import is_known
from acade2sql.validate import Report, Status, TableResult, table_checksum, utc_now
from acade2sql.writers import SqlAlchemyWriter

log = logging.getLogger(__name__)

TARGET_ENV = "ACADE2SQL_TARGET"
SQLITE_SUFFIXES = frozenset({".sqlite", ".db", ".sqlite3"})

_T = TypeVar("_T")


def _sqlite_url(path: Path) -> str:
    """``sqlite:///`` URL for ``path`` (absolute). The path is escaped, never URL-decoded."""
    url = URL.create("sqlite", database=str(path.expanduser().resolve()))
    return url.render_as_string(hide_password=False)


def _path_to_url(target: str) -> str:
    if "://" not in target and Path(target).suffix.lower() in SQLITE_SUFFIXES:
        return _sqlite_url(Path(target))
    return target


def resolve_target(target: str | None, source: Path) -> str:
    """Explicit ``target`` -> ``$ACADE2SQL_TARGET`` -> ``sqlite:///<cwd>/<source stem>.sqlite``.

    A plain filesystem path ending ``.sqlite``/``.db``/``.sqlite3`` becomes an absolute
    ``sqlite:///`` URL that opens exactly that file (characters such as ``%`` are escaped).
    """
    if target:
        return _path_to_url(target)
    env = os.environ.get(TARGET_ENV, "").strip()
    if env:
        return _path_to_url(env)
    return _sqlite_url(Path.cwd() / (Path(source).stem + ".sqlite"))


def select_tables(
    available: Sequence[str],
    tables: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
) -> list[str]:
    """Apply case-insensitive include/exclude filters; exclude wins. Unknown includes raise."""
    by_fold: dict[str, str] = {}
    for name in available:
        by_fold.setdefault(name.casefold(), name)
    if tables:
        unknown = [t for t in tables if t.casefold() not in by_fold]
        if unknown:
            raise TableNotFoundError(f"Table(s) not found in source: {', '.join(unknown)}")
        wanted = {t.casefold() for t in tables}
        selected = [n for n in available if n.casefold() in wanted]
    else:
        selected = list(available)
    if exclude:
        dropped = {t.casefold() for t in exclude}
        selected = [n for n in selected if n.casefold() not in dropped]
    return selected


def unmapped_type_warnings(info: TableInfo) -> list[str]:
    """One warning per column whose Access type has no TYPE_MAP entry (stored as text)."""
    return [
        f"unmapped type {c.access_type} in column {c.name} stored as text"
        for c in sorted(info.columns, key=lambda c: c.ordinal)
        if not is_known(c.access_type)
    ]


class _ReadFailed(Exception):
    """Internal marker: the source side (Access reader) raised ``cause``."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _SourceBatches:
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


def _read(fn: Callable[[], _T]) -> _T:
    try:
        return fn()
    except Exception as exc:
        raise _ReadFailed(exc) from exc


def _error_text(exc: BaseException, url: URL | str | None) -> str:
    """``read failed: ...`` or ``write failed: ...`` with secrets redacted."""
    if isinstance(exc, _ReadFailed):
        cause = exc.cause
        return f"read failed: {type(cause).__name__}: {redact(str(cause), url)}"
    if isinstance(exc, WriteError):
        return f"write failed: {exc.detail}"
    return f"write failed: {type(exc).__name__}: {redact(str(exc), url)}"


def _convert_table(
    reader: Reader,
    writer: SqlAlchemyWriter,
    info: TableInfo,
    batch_size: int,
    checksum: bool,
) -> TableResult:
    """create_table + load + verify one table; every failure becomes ``status="error"``."""
    started = time.perf_counter()
    warnings = unmapped_type_warnings(info)
    source_rows: int | None = None
    target_rows: int | None = None
    try:
        source_rows = _read(lambda: reader.count_rows(info.name))
        writer.create_table(info)
        source = _SourceBatches(lambda: reader.iter_rows(info.name, batch_size))
        try:
            written = writer.write_batches(info, source)
        except WriteError as exc:
            if source.error is not None:
                raise _ReadFailed(source.error) from exc
            raise
        target_rows = writer.count_rows(info.name)
        match: bool | None = None
        if checksum:
            src_sum = _read(lambda: table_checksum(reader.iter_rows(info.name, batch_size)))
            match = src_sum == table_checksum(writer.read_rows(info.name, batch_size))
        status: Status = "pass" if source_rows == target_rows and match is not False else "fail"
        if warnings:  # DECISIONS #21: an unmapped type always fails the table
            status = "fail"
    except Exception as exc:
        error = _error_text(exc, writer.url)
        log.error("table failed", extra={"kv": {"table": info.name, "error": error}})
        return TableResult(
            info.name,
            source_rows,
            target_rows,
            "error",
            error=error,
            seconds=time.perf_counter() - started,
            warnings=warnings,
        )
    seconds = time.perf_counter() - started
    log.info(
        "converted table",
        extra={
            "kv": {
                "table": info.name,
                "rows": written,
                "seconds": round(seconds, 3),
                "rows_per_s": round(written / seconds) if seconds > 0 else written,
                "status": status,
            }
        },
    )
    for warning in warnings:
        log.warning(warning, extra={"kv": {"table": info.name}})
    return TableResult(
        info.name,
        source_rows,
        target_rows,
        status,
        seconds=seconds,
        checksum_match=match,
        warnings=warnings,
    )


def _read_schemas(
    reader: Reader, names: Sequence[str], results: list[TableResult]
) -> list[TableInfo]:
    infos: list[TableInfo] = []
    for name in names:
        try:
            infos.append(reader.get_table(name))
        except Exception as exc:
            error = _error_text(_ReadFailed(exc), None)
            log.error("cannot read schema", extra={"kv": {"table": name, "error": error}})
            results.append(TableResult(name, None, None, "error", error=error))
    return infos


def convert(
    source: str | Path,
    target: str | None = None,
    *,
    mode: Literal["full"] = "full",
    tables: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    batch_size: int = 5000,
    checksum: bool = False,
    copy_to_temp: bool = False,
    report_path: str | Path | None = None,
) -> Report:
    """Convert ``source`` to ``target`` (see :func:`resolve_target`) and return the report.

    Acade2SqlError subclasses raised before the target is modified (bad source, missing driver,
    unknown table, bad or unreachable target) propagate. Afterwards each table is dropped,
    created and loaded in turn; any failure is recorded in that table's result and the run
    continues. The report is written to ``report_path`` whenever the run gets that far.
    """
    if mode != "full":
        raise ValueError(f"unsupported mode {mode!r} (M3 supports only 'full')")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError(f"batch_size must be an integer >= 1, got {batch_size!r}")
    source_path = Path(source)
    url = resolve_target(target, source_path)
    started = utc_now()
    with open_reader(source_path, copy_to_temp=copy_to_temp) as reader:
        names = select_tables(reader.list_tables(), tables, exclude)
        if not names:
            log.warning("no tables selected", extra={"kv": {"source": str(reader.path)}})
        results: list[TableResult] = []
        infos = _read_schemas(reader, names, results)
        with SqlAlchemyWriter(url) as writer:
            log.info(
                "converting",
                extra={
                    "kv": {
                        "source": str(reader.path),
                        "target": writer.url_masked,
                        "tables": len(infos),
                    }
                },
            )
            if infos:
                writer.prepare(infos, mode=mode)  # no DDL; TargetError if unreachable
            for info in infos:
                results.append(_convert_table(reader, writer, info, batch_size, checksum))
            masked = writer.url_masked
    order = {n: i for i, n in enumerate(names)}
    results.sort(key=lambda r: order.get(r.name, len(order)))
    report = Report(
        source=str(source_path.resolve()),
        target=masked,
        mode=mode,
        started=started,
        finished=utc_now(),
        tables=results,
    )
    if report_path is not None:
        report.write_json(report_path)
    return report
