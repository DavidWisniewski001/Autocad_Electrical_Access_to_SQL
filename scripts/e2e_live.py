"""End-to-end check of the real sample databases against live servers (docs/M6_SPEC.md §4).

For every target URL and every ``*.mdb`` / ``*.accdb`` in the data directory:

1. ``full --reader pure --checksum`` into schema ``e2e`` (all tables pass, all checksums match);
2. tamper one table through SQLAlchemy: 3 updates, 2 deletes, 1 extra row;
3. ``sync --checksum``: exactly inserted=2 updated=3 deleted=0 extra=1 on that table, all zero
   elsewhere;
4. ``sync --checksum --delete-missing``: deleted=1 on that table, all zero elsewhere;
5. ``sync --checksum``: all zero everywhere.

A summary table is printed; the exit code is 0 when everything matched, 1 on any deviation and
2 on a usage problem. The sources are only read (pure reader, read-only; their size and mtime
are compared before/after). The schema is dropped at the end unless ``--keep``.

    python scripts/e2e_live.py                                  # URLs from the environment
    python scripts/e2e_live.py --targets sqlite:///build/e2e.sqlite --data-dir data_files

Targets default to ``ACADE2SQL_TEST_MSSQL_URL`` and ``ACADE2SQL_TEST_PG_URL``; the data
directory to ``ACADE2SQL_DATA_DIR``. SQLite targets (local smoke test) use no schema; the
converted tables are dropped at the end instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Engine,
    MetaData,
    Table,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy import types as sat
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from acade2sql import Report, convert, mask_url

log = logging.getLogger("e2e_live")

ENV_TARGETS = ("ACADE2SQL_TEST_MSSQL_URL", "ACADE2SQL_TEST_PG_URL")
SCHEMA_RE = re.compile(r"^e2e[A-Za-z0-9_]{0,40}$")
N_UPDATES, N_DELETES = 3, 2
MIN_ROWS = 10
ZERO = (0, 0, 0, 0)  # inserted, updated, deleted, extra_rows
MAX_DEVIATIONS_SHOWN = 20


@dataclass
class Tamper:
    table: str
    key: str
    column: str
    updated_keys: list[Any]
    deleted_keys: list[Any]
    extra_key: Any


@dataclass
class Step:
    target: str
    source: str
    name: str
    tables: int = 0
    passed: int = 0
    rows: int = 0
    tampered: str = ""
    seconds: float = 0.0
    deviations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.deviations


# ------------------------------------------------------------------------------------- targets


def backend(url: str) -> str:
    return make_url(url).get_backend_name()


def sqlite_path(url: str) -> Path | None:
    db = make_url(url).database
    if backend(url) != "sqlite" or not db or db == ":memory:":
        return None
    return Path(db).resolve()


def target_inside(url: str, data_dir: Path) -> bool:
    """True for a SQLite target inside the (read-only) data directory."""
    path = sqlite_path(url)
    return path is not None and data_dir.resolve() in path.parents


def drop_schema(engine: Engine, schema: str) -> None:
    """Drop every table of ``schema`` and the schema itself (portable: no CASCADE on mssql)."""
    with engine.begin() as conn:
        insp = inspect(conn)
        if not insp.has_schema(schema):
            return
        md = MetaData(schema=schema)
        for name in insp.get_table_names(schema=schema):
            Table(name, md)  # DROP TABLE needs no columns
        md.drop_all(conn)
        conn.execute(DropSchema(schema))


def create_schema(engine: Engine, schema: str) -> None:
    with engine.begin() as conn:
        if not inspect(conn).has_schema(schema):
            conn.execute(CreateSchema(schema))


def drop_tables(engine: Engine, names: Sequence[str]) -> None:
    with engine.begin() as conn:
        existing = set(inspect(conn).get_table_names())
        md = MetaData()
        for name in names:
            if name in existing:
                Table(name, md)
        md.drop_all(conn)


# ------------------------------------------------------------------------------------- tamper


def _key_column(engine: Engine, name: str, schema: str | None) -> str | None:
    insp = inspect(engine)
    pk = insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or []
    if len(pk) != 1:
        return None
    others = [
        ix
        for ix in insp.get_indexes(name, schema=schema)
        if ix.get("unique") and list(ix.get("column_names") or []) != pk
    ]
    try:
        uniques = insp.get_unique_constraints(name, schema=schema)
    except NotImplementedError:
        # SQLAlchemy's mssql dialect: unique constraints are backed by indexes, already listed.
        uniques = []
    others += [uc for uc in uniques if list(uc.get("column_names") or []) != pk]
    return None if others else pk[0]


def _tamper_column(table: Table, key: str) -> str | None:
    """A non-key column whose value can be changed: text first, then a number."""
    cols = [c for c in table.columns if c.name != key]
    for c in cols:
        if isinstance(c.type, sat.String) and (c.type.length is None or c.type.length >= 3):
            return c.name
    for c in cols:
        if isinstance(c.type, (sat.Integer, sat.Numeric)) and not isinstance(c.type, sat.Boolean):
            return c.name
    return None


def _new_value(col: Any, old: Any, i: int) -> Any:
    if isinstance(col.type, sat.String):
        length = col.type.length
        new = f"e2e-{i}" if length is None or length >= 5 else f"E{i}"
        return new if new != old else f"X{i}"
    return (old or 0) + 1


def _extra_key(conn: Any, table: Table, key: str) -> Any | None:
    col = table.c[key]
    if isinstance(col.type, sat.Integer):
        return (conn.execute(select(func.max(col))).scalar() or 0) + 1
    if isinstance(col.type, sat.String):
        base = conn.execute(select(func.min(col))).scalar() or ""
        new = f"{base}~e2e"
        if col.type.length is not None and len(new) > col.type.length:
            new = "~e2e"[: col.type.length]
        exists = conn.execute(select(func.count()).where(col == new)).scalar()
        return None if exists else new
    return None


def choose_and_tamper(engine: Engine, schema: str | None, report: Report) -> Tamper:
    candidates = sorted(
        (t for t in report.tables if t.status == "pass" and (t.target_rows or 0) >= MIN_ROWS),
        key=lambda t: (t.target_rows, t.name),
    )
    for res in candidates:
        key = _key_column(engine, res.name, schema)
        if key is None:
            continue
        with engine.begin() as conn:
            table = Table(res.name, MetaData(schema=schema), autoload_with=conn)
            column = _tamper_column(table, key)
            extra = _extra_key(conn, table, key) if column else None
            if column is None or extra is None:
                continue
            kc, vc = table.c[key], table.c[column]
            rows = (
                conn.execute(select(table).order_by(kc).limit(N_UPDATES + N_DELETES))
                .mappings()
                .all()
            )
            upd, dele = rows[:N_UPDATES], rows[N_UPDATES:]
            for i, row in enumerate(upd, 1):
                conn.execute(
                    update(table).where(kc == row[key]).values({vc: _new_value(vc, row[column], i)})
                )
            conn.execute(delete(table).where(kc.in_([r[key] for r in dele])))
            new_row = {table.c[k]: v for k, v in rows[0].items()}
            new_row[kc] = extra
            conn.execute(insert(table).values(new_row))
        return Tamper(res.name, key, column, [r[key] for r in upd], [r[key] for r in dele], extra)
    raise RuntimeError(
        f"no table with a single-column key, >= {MIN_ROWS} rows and a changeable column"
    )


# ------------------------------------------------------------------------------------- checks


def _counters(t: Any) -> tuple[int, int, int, int]:
    return (t.inserted or 0, t.updated or 0, t.deleted or 0, t.extra_rows or 0)


def check_report(
    step: Step, report: Report, expected: dict[str, tuple[int, int, int, int]]
) -> None:
    """Every table pass + checksum match; sync counters as ``expected`` (default all zero)."""
    step.tables = len(report.tables)
    step.passed = sum(1 for t in report.tables if t.status == "pass")
    step.rows = sum(t.target_rows or 0 for t in report.tables)
    if not report.tables:
        step.deviations.append("no tables converted")
    for t in report.tables:
        if t.status != "pass":
            step.deviations.append(f"{t.name}: status {t.status} ({t.error or 'no error text'})")
        if t.checksum_match is not True:
            step.deviations.append(f"{t.name}: checksum_match={t.checksum_match}")
        if report.mode == "sync":
            want = expected.get(t.name, ZERO)
            got = _counters(t)
            if got != want:
                step.deviations.append(
                    f"{t.name}: inserted/updated/deleted/extra {got}, expected {want}"
                )
        if t.name in expected:
            step.tampered = f"{t.name} {_counters(t)}"


def source_state(paths: Sequence[Path]) -> dict[Path, tuple[int, int]]:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}


# ------------------------------------------------------------------------------------- runner


def run_source(
    url: str,
    source: Path,
    schema: str | None,
    opts: argparse.Namespace,
    engine: Engine,
    steps: list[Step],
    created: set[str],
) -> None:
    target = backend(url)
    common: dict[str, Any] = {
        "checksum": True,
        "reader": opts.reader,
        "schema": schema,
        "tables": opts.tables,
        "batch_size": opts.batch_size,
    }

    def run(name: str, expected: dict[str, tuple[int, int, int, int]], **kw: Any) -> Report | None:
        """One convert() call; the report when every check passed, else None."""
        step = Step(target, source.name, name)
        steps.append(step)
        started = time.monotonic()
        try:
            report = convert(source, url, **common, **kw)
        except Exception as exc:
            step.deviations.append(f"convert raised {type(exc).__name__}: {exc}")
            return None
        finally:
            step.seconds = time.monotonic() - started
        created.update(t.name for t in report.tables)
        check_report(step, report, expected)
        log.info("%s %s %s: %s", target, source.name, name, "ok" if step.ok else "DEVIATION")
        return report if step.ok else None

    full = run("full", {}, mode="full")
    if full is None:
        return
    tamper_step = Step(target, source.name, "tamper")
    steps.append(tamper_step)
    started = time.monotonic()
    try:
        tamper = choose_and_tamper(engine, schema, full)
    except Exception as exc:
        tamper_step.deviations.append(f"tamper failed: {type(exc).__name__}: {exc}")
        return
    finally:
        tamper_step.seconds = time.monotonic() - started
    tamper_step.tampered = (
        f"{tamper.table} key={tamper.key} col={tamper.column} "
        f"upd={tamper.updated_keys} del={tamper.deleted_keys} extra={tamper.extra_key}"
    )
    log.info("tampered %s", tamper_step.tampered)
    t = tamper.table
    run("sync", {t: (N_DELETES, N_UPDATES, 0, 1)}, mode="sync")
    run("sync --delete-missing", {t: (0, 0, 1, 0)}, mode="sync", delete_missing=True)
    run("sync (no-op)", {t: ZERO}, mode="sync")


def run_target(
    url: str, sources: Sequence[Path], opts: argparse.Namespace, steps: list[Step]
) -> None:
    schema = None if backend(url) == "sqlite" else opts.schema
    engine = create_engine(url)
    created: set[str] = set()
    try:
        if schema is not None:
            drop_schema(engine, schema)  # leftovers of an earlier --keep run
            create_schema(engine, schema)
        for source in sources:
            run_source(url, source, schema, opts, engine, steps, created)
    finally:
        try:
            if opts.keep:
                log.info("--keep: leaving %s in place", f"schema {schema}" if schema else "tables")
            elif schema is not None:
                drop_schema(engine, schema)
            else:
                drop_tables(engine, sorted(created))
        except Exception as exc:
            steps.append(Step(backend(url), "-", "cleanup", deviations=[f"cleanup failed: {exc}"]))
        engine.dispose()


def print_summary(steps: Sequence[Step], targets: Sequence[str]) -> None:
    print()
    print("targets: " + "; ".join(mask_url(u) for u in targets))
    head = ("target", "source", "step", "tables", "pass", "rows", "secs", "result", "tampered")
    rows = [
        (
            s.target,
            s.source,
            s.name,
            str(s.tables),
            str(s.passed),
            str(s.rows),
            f"{s.seconds:.1f}",
            "OK" if s.ok else f"FAIL({len(s.deviations)})",
            s.tampered,
        )
        for s in steps
    ]
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head) - 1)]
    for r in [head, *rows]:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=False)) + "  " + r[-1])
    for s in steps:
        for d in s.deviations[:MAX_DEVIATIONS_SHOWN]:
            print(f"DEVIATION {s.target} {s.source} {s.name}: {d}")
        if len(s.deviations) > MAX_DEVIATIONS_SHOWN:
            print(f"... and {len(s.deviations) - MAX_DEVIATIONS_SHOWN} more")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--targets",
        nargs="+",
        metavar="URL",
        help=f"target URLs (default: {' / '.join(ENV_TARGETS)} from the environment)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="directory with the source databases (default: $ACADE2SQL_DATA_DIR)",
    )
    p.add_argument("--schema", default="e2e", help="throwaway target schema (default: e2e)")
    p.add_argument("--keep", action="store_true", help="keep the schema/tables at the end")
    p.add_argument("--tables", nargs="+", default=None, help="only these tables (quick runs)")
    p.add_argument("--reader", default="pure", choices=("pure", "odbc", "auto"))
    p.add_argument("--batch-size", type=int, default=5000)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    opts = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if opts.verbose:
        log.setLevel(logging.INFO)
    if not SCHEMA_RE.match(opts.schema):
        print("error: --schema must start with 'e2e' (it is dropped at the end)", file=sys.stderr)
        return 2
    targets = opts.targets or [os.environ[v] for v in ENV_TARGETS if os.environ.get(v, "").strip()]
    if not targets:
        print(
            f"error: no targets: set {' / '.join(ENV_TARGETS)} or pass --targets", file=sys.stderr
        )
        return 2
    data = opts.data_dir or (
        Path(os.environ["ACADE2SQL_DATA_DIR"]) if os.environ.get("ACADE2SQL_DATA_DIR") else None
    )
    if data is None or not data.is_dir():
        print(
            "error: no data directory: set ACADE2SQL_DATA_DIR or pass --data-dir", file=sys.stderr
        )
        return 2
    sources = sorted(
        p for p in data.iterdir() if p.is_file() and p.suffix.lower() in (".mdb", ".accdb")
    )
    if not sources:
        print(f"error: no .mdb/.accdb files in {data}", file=sys.stderr)
        return 2
    for url in targets:
        if target_inside(url, data):
            print(f"error: refusing to write a SQLite target inside {data}", file=sys.stderr)
            return 2
    print(
        f"e2e: {len(sources)} source(s) x {len(targets)} target(s): "
        + ", ".join(p.name for p in sources),
        flush=True,
    )
    before = source_state(sources)
    steps: list[Step] = []
    for url in targets:
        run_target(url, sources, opts, steps)
    if source_state(sources) != before:
        steps.append(Step("-", "-", "sources", deviations=["a source file changed during the run"]))
    print_summary(steps, targets)
    ok = all(s.ok for s in steps)
    print("e2e: PASS" if ok else "e2e: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
