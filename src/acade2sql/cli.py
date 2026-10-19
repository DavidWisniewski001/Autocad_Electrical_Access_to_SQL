"""Command-line interface: ``acade2sql inspect`` and ``acade2sql convert``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Sequence
from typing import Any, TextIO

from acade2sql import __version__
from acade2sql.converter import TARGET_ENV, convert
from acade2sql.errors import Acade2SqlError
from acade2sql.logging_setup import configure_logging
from acade2sql.readers import Reader, open_reader
from acade2sql.redact import redact
from acade2sql.schema import TableInfo

log = logging.getLogger("acade2sql.cli")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_VALIDATION = 0, 1, 2, 3


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="debug logging (structured, stderr) and tracebacks on error",
    )
    parser = argparse.ArgumentParser(
        prog="acade2sql",
        description="Convert AutoCAD Electrical Access databases (.mdb/.accdb) to SQL.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="debug logging (structured, stderr) and tracebacks on error",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    insp = sub.add_parser("inspect", parents=[common], help="list tables, columns, keys, counts")
    insp.add_argument("path", help="Access database (.mdb or .accdb)")
    insp.add_argument(
        "--table",
        action="append",
        dest="tables",
        metavar="NAME",
        help="show full column/index detail for this table (repeatable)",
    )
    insp.add_argument("--json", action="store_true", help="write a JSON document to stdout")
    insp.add_argument(
        "--counts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="count rows per table (default: on)",
    )

    conv = sub.add_parser("convert", parents=[common], help="convert to a SQL database")
    conv.add_argument("source", help="Access database (.mdb or .accdb)")
    conv.add_argument(
        "--target",
        metavar="URL|PATH",
        help=f"SQLAlchemy URL or .sqlite/.db path (default: ${TARGET_ENV}, else ./<source>.sqlite)",
    )
    conv.add_argument("--mode", choices=["full"], default="full", help="write mode (default: full)")
    conv.add_argument("--tables", nargs="+", metavar="T", help="only these tables")
    conv.add_argument("--exclude", nargs="+", metavar="T", help="skip these tables")
    conv.add_argument(
        "--batch-size", type=_positive_int, default=5000, metavar="N", help="rows per batch"
    )
    conv.add_argument(
        "--checksum", action="store_true", help="compare row checksums (re-reads every table)"
    )
    conv.add_argument(
        "--copy-to-temp", action="store_true", help="read from a temporary copy of the source"
    )
    conv.add_argument("--report", metavar="PATH.json", help="write the JSON report here")
    return parser


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1: {value}")
    return value


def _table_row(info: TableInfo, rows: int | None) -> dict[str, Any]:
    return {**info.to_dict(), "row_count": rows}


def _collect(reader: Reader, names: list[str], counts: bool) -> list[dict[str, Any]]:
    out = []
    for name in names:
        info = reader.get_table(name)
        rows = reader.count_rows(info.name) if counts else None
        log.debug("inspected table", extra={"kv": {"table": info.name, "rows": rows}})
        out.append(_table_row(info, rows))
    return out


def _pk_text(t: dict[str, Any]) -> str:
    if not t["primary_key"]:
        return "pk=- (none)"
    return f"pk={','.join(t['primary_key'])} ({t['primary_key_source']})"


def _print_summary(tables: list[dict[str, Any]], out: TextIO) -> None:
    width = max((len(t["name"]) for t in tables), default=4)
    for t in tables:
        rows = "-" if t["row_count"] is None else str(t["row_count"])
        out.write(
            f"{t['name']:<{width}}  {len(t['columns']):>4} cols  {rows:>9} rows  {_pk_text(t)}\n"
        )
    total = sum(t["row_count"] or 0 for t in tables)
    if tables and all(t["row_count"] is not None for t in tables):
        out.write(f"{len(tables)} tables, {total} rows\n")
    else:
        out.write(f"{len(tables)} tables\n")


def _print_detail(t: dict[str, Any], out: TextIO) -> None:
    rows = "-" if t["row_count"] is None else str(t["row_count"])
    out.write(f"\nTable {t['name']}  rows={rows}  {_pk_text(t)}\n")
    out.write(f"  {'#':>3}  {'column':<24} {'type':<10} {'size':>10}  nullable\n")
    for c in t["columns"]:
        size = "" if c["size"] is None else str(c["size"])
        flags = "yes" if c["nullable"] else "no"
        if c["autoincrement"]:
            flags += "  autoincrement"
        out.write(
            f"  {c['ordinal']:>3}  {c['name']:<24} {c['access_type']:<10} {size:>10}  {flags}\n"
        )
    if not t["indexes"]:
        out.write("  indexes: none reported\n")
    for i in t["indexes"]:
        kind = "unique" if i["unique"] else "non-unique"
        out.write(f"  index {i['name']} ({', '.join(i['columns'])}) {kind}\n")


def _cmd_inspect(args: argparse.Namespace, out: TextIO) -> int:
    with open_reader(args.path) as reader:
        names = args.tables or reader.list_tables()
        tables = _collect(reader, names, args.counts)
    if args.json:
        json.dump({"source": str(reader.path), "tables": tables}, out, indent=2)
        out.write("\n")
        return EXIT_OK
    _print_summary(tables, out)
    if args.tables:
        for t in tables:
            _print_detail(t, out)
    return EXIT_OK


def _cmd_convert(args: argparse.Namespace, out: TextIO) -> int:
    started = time.perf_counter()
    report = convert(
        args.source,
        args.target,
        mode=args.mode,
        tables=args.tables,
        exclude=args.exclude,
        batch_size=args.batch_size,
        checksum=args.checksum,
        copy_to_temp=args.copy_to_temp,
        report_path=args.report,
    )
    elapsed = time.perf_counter() - started
    if not report.tables:
        sys.stderr.write("warning: no tables selected\n")
    out.write(report.render_text() + "\n")
    rows = sum(t.target_rows or 0 for t in report.tables)
    rate = round(rows / elapsed) if elapsed > 0 else rows
    sys.stderr.write(
        f"target={report.target} rows={rows} elapsed={elapsed:.1f}s rows_per_s={rate}\n"
    )
    return EXIT_OK if report.ok else EXIT_VALIDATION


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Exit codes: 0 success, 1 acade2sql error, 2 usage, 3 validation failure."""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse: --help/--version (0) or usage error (2)
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    verbose = bool(getattr(args, "verbose", False))
    configure_logging(verbose)
    try:
        if args.command == "inspect":
            return _cmd_inspect(args, sys.stdout)
        return _cmd_convert(args, sys.stdout)
    except Acade2SqlError as exc:
        target = getattr(args, "target", None)
        if verbose:
            log.error("failed", exc_info=True)  # target errors are raised "from None": no secrets
        sys.stderr.write(f"error: {redact(str(exc), target)}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
