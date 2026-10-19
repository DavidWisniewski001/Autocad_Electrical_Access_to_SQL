"""Public conversion API: Access database -> SQLAlchemy target, with per-table validation."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from acade2sql import plan
from acade2sql.errors import TableNotFoundError, UsageError
from acade2sql.keys import NO_KEY_CHOICES, KeySpec, parse_key_specs, unmatched_tables
from acade2sql.modes import (
    RUNNERS,
    Mode,
    NoKey,
    ReadFailed,
    RunOptions,
    error_text,
    unmapped_type_warnings,
)
from acade2sql.readers import READER_CHOICES, Reader, ReaderChoice, open_reader
from acade2sql.redact import mask_url
from acade2sql.schema import TableInfo
from acade2sql.validate import Report, TableResult, utc_now
from acade2sql.writers import SqlAlchemyWriter
from acade2sql.writers.dialects import check_pg_identifiers
from acade2sql.writers.sqlalchemy_writer import MODES, check_schema_name

log = logging.getLogger(__name__)

TARGET_ENV = "ACADE2SQL_TARGET"
#: ``progress(result, index, total)``: called after each table of a real run (1-based index).
Progress = Callable[[TableResult, int, int], None]
SQLITE_SUFFIXES = frozenset({".sqlite", ".db", ".sqlite3"})


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


def _convert_table(
    reader: Reader, writer: SqlAlchemyWriter, info: TableInfo, opts: RunOptions
) -> TableResult:
    """Run the mode for one table; every failure becomes ``status="error"``."""
    started = time.perf_counter()
    unmapped = unmapped_type_warnings(info)
    res = TableResult(info.name, None, None, "pass", warnings=list(unmapped))
    written = 0
    try:
        res.warnings.extend(writer.table_warnings(info))
        written = RUNNERS[opts.mode](reader, writer, info, opts, res)
        if unmapped:  # DECISIONS #21: an unmapped type always fails the table
            res.status = "fail"
    except Exception as exc:
        res.status = "error"
        res.error = error_text(exc, writer.url)
        res.seconds = time.perf_counter() - started
        log.error("table failed", extra={"kv": {"table": info.name, "error": res.error}})
        return res
    res.seconds = time.perf_counter() - started
    log.info(
        "converted table",
        extra={
            "kv": {
                "table": info.name,
                "mode": opts.mode,
                "rows": written,
                "seconds": round(res.seconds, 3),
                "rows_per_s": round(written / res.seconds) if res.seconds > 0 else written,
                "status": res.status,
            }
        },
    )
    for warning in res.warnings:
        log.warning(warning, extra={"kv": {"table": info.name}})
    return res


def _read_schemas(
    reader: Reader, names: Sequence[str], results: list[TableResult]
) -> list[TableInfo]:
    infos: list[TableInfo] = []
    for name in names:
        try:
            infos.append(reader.get_table(name))
        except Exception as exc:
            error = error_text(ReadFailed(exc), None)
            log.error("cannot read schema", extra={"kv": {"table": name, "error": error}})
            results.append(TableResult(name, None, None, "error", error=error))
    return infos


def _check_options(
    mode: str,
    batch_size: object,
    key: str | Sequence[str] | None,
    no_key: str,
    delete_missing: bool,
    dry_run: bool,
    counts: bool,
) -> KeySpec:
    if mode not in MODES:
        raise UsageError(f"unsupported mode {mode!r}; expected one of {', '.join(MODES)}")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError(f"batch_size must be an integer >= 1, got {batch_size!r}")
    if no_key not in NO_KEY_CHOICES:
        raise UsageError(f"no_key must be one of {', '.join(NO_KEY_CHOICES)}, got {no_key!r}")
    if delete_missing and mode != "sync":
        raise UsageError("--delete-missing requires --mode sync")
    if no_key != "error" and mode != "sync":
        raise UsageError("--no-key requires --mode sync")
    keys = parse_key_specs(key)
    if keys and mode != "sync":
        raise UsageError("--key requires --mode sync")
    if not counts and not dry_run:
        raise UsageError("--no-counts is only valid with --dry-run")
    return keys


def _backend(url: str) -> str | None:
    try:
        return make_url(url).get_backend_name()
    except (ArgumentError, ValueError, TypeError):
        return None  # reported as TargetError when the writer opens it


def convert(
    source: str | Path,
    target: str | None = None,
    *,
    mode: Mode = "full",
    tables: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    batch_size: int = 5000,
    checksum: bool = False,
    copy_to_temp: bool = False,
    report_path: str | Path | None = None,
    key: str | Sequence[str] | None = None,
    no_key: NoKey = "error",
    delete_missing: bool = False,
    schema: str | None = None,
    dry_run: bool = False,
    counts: bool = True,
    reader: ReaderChoice = "auto",
    progress: Progress | None = None,
) -> Report:
    """Convert ``source`` to ``target`` (see :func:`resolve_target`) and return the report.

    ``mode``: ``full`` (drop + create + load), ``append`` (insert into existing or new tables)
    or ``sync`` (upsert by ``key``; ``delete_missing`` also deletes target rows whose key is not
    in the source; ``no_key`` decides what happens to tables without a key). ``key``: specs
    ``"COL[,COL...]"`` (all tables having those columns) and ``"TABLE=COL[,COL...]"``.
    ``schema``: target schema (not SQLite). ``dry_run``: plan + DDL, no target connection
    (``counts=False`` skips the source row counts). ``reader``: ``auto`` (ODBC driver if usable,
    else the pure-Python reader with a warning), ``odbc`` or ``pure``; the report records the one
    used. ``progress``: called with ``(result, index, total)`` after each table of a real run.

    Invalid option combinations raise :class:`UsageError` (a ``ValueError``) before anything
    is opened. Acade2SqlError subclasses raised before the target is modified (bad source,
    missing driver, unknown table, bad or unreachable target, PostgreSQL identifiers > 63 bytes)
    propagate. Afterwards every failure is recorded in that table's result and the run
    continues. The report is written to ``report_path`` whenever the run gets that far.
    """
    keys = _check_options(mode, batch_size, key, no_key, delete_missing, dry_run, counts)
    if reader not in READER_CHOICES:
        raise UsageError(f"reader must be one of {', '.join(READER_CHOICES)}, got {reader!r}")
    check_schema_name(schema)
    source_path = Path(source)
    url = resolve_target(target, source_path)
    if schema is not None and _backend(url) == "sqlite":
        raise UsageError("--schema is not supported for SQLite targets")
    opts = RunOptions(mode, batch_size, checksum, keys, no_key, delete_missing)
    started = utc_now()
    ddl: list[str] = []
    with open_reader(source_path, copy_to_temp=copy_to_temp, reader=reader) as source_reader:
        reader_kind = getattr(source_reader, "kind", None)
        names = select_tables(source_reader.list_tables(), tables, exclude)
        if not names:
            # INFO, not WARNING: the CLI prints "warning: no tables selected" itself (once).
            log.info("no tables selected", extra={"kv": {"source": str(source_reader.path)}})
        for unknown in unmatched_tables(keys, names):
            log.warning("--key table not converted", extra={"kv": {"table": unknown}})
        results: list[TableResult] = []
        infos = _read_schemas(source_reader, names, results)
        if dry_run:
            planned, ddl, masked = plan.dry_run(
                source_reader, infos, url, opts, schema=schema, counts=counts
            )
            results.extend(planned)
        else:
            results.extend(_write_all(source_reader, infos, url, opts, schema, progress))
            masked = mask_url(url)
    order = {n: i for i, n in enumerate(names)}
    results.sort(key=lambda r: order.get(r.name, len(order)))
    report = Report(
        source=str(source_path.resolve()),
        target=masked,
        mode=mode,
        started=started,
        finished=utc_now(),
        tables=results,
        dry_run=dry_run,
        schema=schema,
        ddl=ddl,
        reader=reader_kind,
    )
    if report_path is not None:
        report.write_json(report_path)
    return report


def _write_all(
    reader: Reader,
    infos: Sequence[TableInfo],
    url: str,
    opts: RunOptions,
    schema: str | None,
    progress: Progress | None = None,
) -> list[TableResult]:
    with SqlAlchemyWriter(url, schema=schema) as writer:
        log.info(
            "converting",
            extra={
                "kv": {
                    "source": str(reader.path),
                    "target": writer.url_masked,
                    "mode": opts.mode,
                    "tables": len(infos),
                }
            },
        )
        if writer.dialect == "postgresql":
            check_pg_identifiers(infos, schema)  # before any target modification
        if infos:
            writer.prepare(infos, mode=opts.mode)  # no DDL; TargetError if unreachable
        results = []
        for index, info in enumerate(infos, 1):
            res = _convert_table(reader, writer, info, opts)
            results.append(res)
            if progress is not None:
                progress(res, index, len(infos))
        return results
