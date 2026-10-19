"""Command-line interface: ``acade2sql inspect`` and (from M3) ``acade2sql convert``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from acade2sql import __version__
from acade2sql.errors import Acade2SqlError
from acade2sql.logging_setup import configure_logging
from acade2sql.readers import Reader, open_reader
from acade2sql.schema import TableInfo

log = logging.getLogger("acade2sql.cli")

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


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

    conv = sub.add_parser("convert", parents=[common], help="convert to SQL (milestone M3)")
    conv.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


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


def _cmd_convert(args: argparse.Namespace) -> int:
    sys.stderr.write("convert is not implemented yet (milestone M3)\n")
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns 0 on success, 1 on an acade2sql error, 2 on usage errors."""
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
        return _cmd_convert(args)
    except Acade2SqlError as exc:
        if verbose:
            log.exception("failed")
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
