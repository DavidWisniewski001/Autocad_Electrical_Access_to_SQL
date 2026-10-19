"""Compare the pure-Python reader with the ODBC reader on one or more Access databases.

For every table: schema (column names, order, access_type) and the column-aware
``table_checksum`` of all rows must be identical. Other schema fields (data_type, size,
decimal_digits, nullable, indexes, primary key) are compared too and reported as notes.

Nothing is written next to the sources. With ``--copy-to-temp`` the ODBC reader reads a private
temporary copy, so not even its transient lock file appears next to the source (the pure reader
never creates one).

    python scripts/compare_readers.py DB.mdb [DB2.accdb ...] [--copy-to-temp] [--tables T ...]
        [--json OUT.json]

Exit codes: 0 every table identical; 1 differences found; 2 usage error (bad arguments, a
source that is missing or cannot be opened/read). A database that fails is reported and the
remaining ones are still compared; ``--json`` is always written (also on errors).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from acade2sql.errors import Acade2SqlError
from acade2sql.readers import open_reader
from acade2sql.schema import TableInfo
from acade2sql.validate import table_checksum

READERS = ("odbc", "pure")
OPEN_ORDER = ("pure", "odbc")  # pure first: it warns if a lock file exists, and odbc creates one
EXIT_OK, EXIT_DIFFERENCES, EXIT_USAGE = 0, 1, 2


class SourceUnusable(Exception):
    """The source cannot be compared at all (missing, unreadable, not openable)."""


@dataclass
class TableComparison:
    name: str
    identical: bool = False
    rows: dict[str, int | None] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    differences: list[str] = field(default_factory=list)  # schema core or checksum: failures
    notes: list[str] = field(default_factory=list)  # other schema fields: informational


def _schema_core(info: TableInfo) -> list[tuple[str, int, str]]:
    return [(c.name, c.ordinal, c.access_type) for c in info.columns]


def _schema_notes(odbc: TableInfo, pure: TableInfo) -> list[str]:
    notes = []
    for co, cp in zip(odbc.columns, pure.columns, strict=False):
        for attr in ("data_type", "size", "decimal_digits", "nullable", "autoincrement"):
            a, b = getattr(co, attr), getattr(cp, attr)
            if a != b:
                notes.append(f"column {co.name!r} {attr}: odbc={a!r} pure={b!r}")
    if sorted(odbc.indexes, key=lambda i: i.name) != sorted(pure.indexes, key=lambda i: i.name):
        notes.append(f"indexes: odbc={odbc.indexes!r} pure={pure.indexes!r}")
    if (odbc.primary_key, odbc.primary_key_source) != (pure.primary_key, pure.primary_key_source):
        notes.append(
            f"primary key: odbc={odbc.primary_key}/{odbc.primary_key_source} "
            f"pure={pure.primary_key}/{pure.primary_key_source}"
        )
    return notes


def _read(reader: Any, name: str, batch_size: int) -> tuple[TableInfo | None, str, int, float]:
    started = time.perf_counter()
    info = reader.get_table(name)
    rows = 0

    def counted():
        nonlocal rows
        for batch in reader.iter_rows(name, batch_size):
            rows += len(batch)
            yield batch

    checksum = table_checksum(counted(), info.column_types)
    return info, checksum, rows, time.perf_counter() - started


def compare_table(readers: dict[str, Any], name: str, batch_size: int) -> TableComparison:
    result = TableComparison(name)
    infos: dict[str, TableInfo] = {}
    sums: dict[str, str] = {}
    for kind in READERS:
        try:
            info, checksum, rows, seconds = _read(readers[kind], name, batch_size)
        except Exception as exc:  # report and continue with the next table
            result.differences.append(f"{kind} failed: {type(exc).__name__}: {exc}")
            result.rows[kind] = None
            continue
        infos[kind], sums[kind] = info, checksum
        result.rows[kind], result.seconds[kind] = rows, round(seconds, 3)
    if len(infos) == len(READERS):
        if _schema_core(infos["odbc"]) != _schema_core(infos["pure"]):
            result.differences.append(
                f"schema: odbc={_schema_core(infos['odbc'])} pure={_schema_core(infos['pure'])}"
            )
        if result.rows["odbc"] != result.rows["pure"]:
            rows = result.rows
            result.differences.append(f"rows: odbc={rows['odbc']} pure={rows['pure']}")
        if sums["odbc"] != sums["pure"]:
            result.differences.append("checksum differs")
        result.notes = _schema_notes(infos["odbc"], infos["pure"])
    result.identical = not result.differences
    return result


def _check_source(path: Path) -> None:
    if not path.exists():
        raise SourceUnusable(f"source not found: {path}")
    if not path.is_file():
        raise SourceUnusable(f"source is not a file: {path}")
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise SourceUnusable(f"cannot open {path}: {exc.strerror or exc}") from None


def _open_readers(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    readers: dict[str, Any] = {}
    try:
        for kind in OPEN_ORDER:
            copy = args.copy_to_temp and kind == "odbc"  # the pure reader never locks
            try:
                readers[kind] = open_reader(path, reader=kind, copy_to_temp=copy)
            except (Acade2SqlError, OSError, ImportError) as exc:
                raise SourceUnusable(f"{kind} reader cannot open {path}: {exc}") from None
    except BaseException:
        for r in readers.values():
            r.close()
        raise
    return readers


def compare_db(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Compare one database. Raises :class:`SourceUnusable` when it cannot be opened."""
    out = sys.stdout
    _check_source(path)
    readers = _open_readers(path, args)
    doc: dict[str, Any] = {"source": str(path), "error": None, "tables": []}
    try:
        try:
            names = {kind: readers[kind].list_tables() for kind in READERS}
        except Exception as exc:
            raise SourceUnusable(f"cannot list the tables of {path}: {exc}") from None
        tables = args.tables or names["odbc"]
        doc.update(
            table_lists_equal=names["odbc"] == names["pure"],
            only_odbc=sorted(set(names["odbc"]) - set(names["pure"])),
            only_pure=sorted(set(names["pure"]) - set(names["odbc"])),
        )
        out.write(f"== {path}  ({len(tables)} tables)\n")
        if not doc["table_lists_equal"]:
            out.write(
                f"TABLE LISTS DIFFER only_odbc={doc['only_odbc']} only_pure={doc['only_pure']}\n"
            )
        for i, name in enumerate(tables, 1):
            res = compare_table(readers, name, args.batch_size)
            doc["tables"].append(asdict(res))
            status = "same" if res.identical else "DIFF"
            secs = " ".join(f"{k}={v:.1f}s" for k, v in res.seconds.items())
            out.write(f"[{i}/{len(tables)}] {status} {name} rows={res.rows.get('pure')} {secs}\n")
            for d in res.differences:
                out.write(f"    difference: {d}\n")
            if args.verbose:
                for n in res.notes:
                    out.write(f"    note: {n}\n")
            out.flush()
    finally:
        for r in readers.values():
            r.close()
    _summarise(doc, path)
    return doc


def _summarise(doc: dict[str, Any], path: Path) -> None:
    same = sum(1 for t in doc["tables"] if t["identical"])
    total = len(doc["tables"])
    noted = sum(1 for t in doc["tables"] if t["notes"])
    timing = {k: round(sum(t["seconds"].get(k, 0.0) for t in doc["tables"]), 1) for k in READERS}
    doc.update(identical=same, total=total, tables_with_notes=noted, seconds=timing)
    sys.stdout.write(
        f"== {path.name}: {same}/{total} identical; tables with schema notes: {noted}; "
        f"time odbc={timing['odbc']}s pure={timing['pure']}s\n"
    )


def _failed_doc(path: Path, error: str) -> dict[str, Any]:
    return {"source": str(path), "error": error, "tables": [], "identical": 0, "total": 0}


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _write_json(target: Path, docs: list[dict[str, Any]]) -> bool:
    try:
        target.write_text(json.dumps(docs, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"error: cannot write --json {target}: {exc.strerror or exc}\n")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sources", nargs="+", type=Path, help=".mdb/.accdb files")
    parser.add_argument("--tables", nargs="+", metavar="T", help="only these tables")
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=5000,
        help="rows fetched per batch from each reader (default: %(default)s)",
    )
    parser.add_argument(
        "--copy-to-temp", action="store_true", help="ODBC reads a temp copy (no .ldb)"
    )
    parser.add_argument("--json", type=Path, metavar="OUT.json", help="write the details here")
    parser.add_argument("-v", "--verbose", action="store_true", help="print schema notes")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help (0) or a usage error (2)
        return EXIT_USAGE if exc.code else EXIT_OK
    docs: list[dict[str, Any]] = []
    failed = False
    try:
        for path in args.sources:
            try:
                docs.append(compare_db(path, args))
            except SourceUnusable as exc:
                failed = True
                sys.stderr.write(f"error: {exc}\n")
                docs.append(_failed_doc(path, str(exc)))
    finally:
        if args.json and not _write_json(args.json, docs):
            failed = True
    if failed:
        return EXIT_USAGE
    ok = all(d["identical"] == d["total"] and d["table_lists_equal"] for d in docs)
    return EXIT_OK if ok else EXIT_DIFFERENCES


if __name__ == "__main__":
    sys.exit(main())
