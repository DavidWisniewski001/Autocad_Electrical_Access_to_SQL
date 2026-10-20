"""Live PostgreSQL / SQL Server tests (M6). Skipped unless the env URLs are set.

``ACADE2SQL_TEST_PG_URL`` / ``ACADE2SQL_TEST_MSSQL_URL`` must point at a throwaway database where
the user may CREATE SCHEMA. Every test works in its own schema ``a2s_<random>`` (dropped in the
teardown, also after a failure), except ``test_schema_acade`` which uses the schema ``acade`` that
``docker/entrypoint.sh`` creates and removes only the tables it created.

Source of the Access data:

* the Access ODBC driver is available (Windows): a fresh session fixture is built, read with the
  ``auto`` reader;
* otherwise (the Linux runner container) the prebuilt ``fixture.accdb`` / ``fixture.mdb`` in
  ``$ACADE2SQL_TEST_FIXTURE_DIR`` (written by ``scripts/build_test_fixtures.py``), read with
  ``reader="pure"``;
* otherwise the live tests skip with a reason.

``ACADE2SQL_TEST_USE_PREBUILT=1`` prefers the prebuilt files even when the driver exists (to
rehearse the container path on Windows).

A third parameter, ``sqlite``, runs the very same scenarios against a throwaway SQLite file. It
needs no server and checks the test logic itself (counters, tampering, rollback) on any
machine with a source available; dialect-specific tests skip for it.
"""

from __future__ import annotations

import json
import os
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from tests.fakes import FakeReader, by_name, make_info
from tests.fixtures import build_access

from acade2sql import converter

pytestmark = pytest.mark.live_db

FIXTURE_DIR_ENV = "ACADE2SQL_TEST_FIXTURE_DIR"
USE_PREBUILT_ENV = "ACADE2SQL_TEST_USE_PREBUILT"
SUBSET = ["COMP", "TERMS", "WIRENO"]  # small keyed tables for the mutation scenarios
NOKEY = "PNLCOMP"
ZERO = (0, 0, 0)


# ---- source -------------------------------------------------------------------------------------


@dataclass
class Source:
    path: Path
    reader: str
    manifest: dict
    origin: str  # "built" | "prebuilt"

    @property
    def tables(self) -> dict[str, dict]:
        return self.manifest["tables"]

    @property
    def tamper(self) -> dict:
        return self.manifest["tamper"]


def _prebuilt() -> Source | None:
    raw = os.environ.get(FIXTURE_DIR_ENV, "").strip()
    if not raw:
        return None
    d = Path(raw)
    for ext in ("accdb", "mdb"):
        if (d / f"fixture.{ext}").is_file():
            mf = d / "MANIFEST.json"
            manifest = (
                json.loads(mf.read_text(encoding="utf-8"))
                if mf.is_file()
                else build_access.manifest_json()
            )
            return Source(d / f"fixture.{ext}", "pure", manifest, "prebuilt")
    return None


@pytest.fixture(scope="session")
def live_source(tmp_path_factory) -> Source:
    prefer = os.environ.get(USE_PREBUILT_ENV) == "1"
    if prefer or not build_access.driver_available():
        src = _prebuilt()
        if src is not None:
            return src
    if not prefer and build_access.driver_available():
        path = build_access.build(tmp_path_factory.mktemp("live_fx") / "live.accdb")
        return Source(path, "auto", build_access.manifest_json(), "built")
    pytest.skip(
        f"live tests need the Access ODBC driver or prebuilt fixtures in ${FIXTURE_DIR_ENV} "
        "(python scripts/build_test_fixtures.py)"
    )


# ---- target -------------------------------------------------------------------------------------


class Live:
    """One target (server + throwaway schema) for a test."""

    def __init__(self, kind: str, url: str, schema: str | None, source: Source) -> None:
        self.kind, self.url, self.schema, self.source = kind, url, schema, source
        self.engine = create_engine(url)

    # conversion ---------------------------------------------------------------------------------
    def convert(self, **kw: Any):
        kw.setdefault("schema", self.schema)
        kw.setdefault("reader", self.source.reader)
        return converter.convert(self.source.path, self.url, **kw)

    # inspection / tampering (through SQLAlchemy, never the package under test) -------------------
    def table(self, name: str, schema: str | None = ...) -> Table:  # type: ignore[assignment]
        schema = self.schema if schema is ... else schema
        return Table(name, MetaData(), autoload_with=self.engine, schema=schema)

    def count(self, name: str) -> int:
        t = self.table(name)
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(t)).scalar_one()

    def rows(self, name: str) -> list[tuple]:
        t = self.table(name)
        with self.engine.connect() as conn:
            return sorted((tuple(r) for r in conn.execute(select(t))), key=repr)

    def table_names(self, schema: str | None = ...) -> list[str]:  # type: ignore[assignment]
        schema = self.schema if schema is ... else schema
        return sorted(inspect(self.engine).get_table_names(schema=schema))

    def drop_all(self, schema: str | None = ...) -> None:  # type: ignore[assignment]
        schema = self.schema if schema is ... else schema
        md = MetaData(schema=schema)
        md.reflect(bind=self.engine)
        md.drop_all(bind=self.engine)


def need(live: Live, *kinds: str) -> None:
    if live.kind not in kinds:
        pytest.skip(f"only for {'/'.join(kinds)} (this is {live.kind})")


@pytest.fixture(params=["pg", "mssql", "sqlite"])
def live(request, live_source, tmp_path):
    kind = request.param
    if kind == "sqlite":
        yield Live(kind, converter._sqlite_url(tmp_path / "live.sqlite"), None, live_source)
        return
    url = request.getfixturevalue("pg_url" if kind == "pg" else "mssql_url")
    schema = f"a2s_{uuid.uuid4().hex[:10]}"
    lv = Live(kind, url, schema, live_source)
    quoted = f'"{schema}"' if kind == "pg" else schema
    try:
        with lv.engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {quoted}"))
        yield lv
    finally:
        try:
            if kind == "pg":
                with lv.engine.begin() as conn:
                    conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))
            else:
                lv.drop_all()
                with lv.engine.begin() as conn:
                    conn.execute(text(f"DROP SCHEMA {schema}"))
        finally:
            lv.engine.dispose()


def counters(res) -> tuple:
    return (res.inserted, res.updated, res.deleted, res.unchanged, res.extra_rows)


# ---- scenarios ----------------------------------------------------------------------------------


def test_full_checksum_every_table_then_noop_sync(live):
    src = live.source
    nul = set(src.manifest.get("pg_nul_tables", [])) if live.kind == "pg" else set()
    rep = live.convert(checksum=True)
    assert {t.name for t in rep.tables} == set(src.tables)
    for t in rep.tables:
        if t.name in nul:
            assert t.status == "error" and "NUL" in (t.error or ""), (t.name, t.error)
            continue
        assert t.status == "pass", (t.name, t.status, t.error, t.warnings)
        assert t.source_rows == t.target_rows == src.tables[t.name]["count"], t.name
        assert t.checksum_match is True, t.name
    for name, spec in src.tables.items():
        if name not in nul:
            assert live.count(name) == spec["count"], name
    skip = nul | {NOKEY}
    rep = live.convert(mode="sync", checksum=True, exclude=sorted(skip))
    assert rep.ok, rep.render_text()
    for t in rep.tables:
        assert (t.inserted, t.updated, t.deleted) == ZERO, t.name
        assert t.unchanged == t.source_rows == src.tables[t.name]["count"], t.name
        assert t.extra_rows == 0 and t.checksum_match is True, t.name


def test_target_tampering_then_delete_missing(live):
    tp = live.source.tamper
    name, key, col = tp["table"], tp["key"], tp["update_column"]
    n = live.source.tables[name]["count"]
    rep = live.convert(tables=SUBSET, checksum=True)
    assert rep.ok, rep.render_text()

    t = live.table(name)
    with live.engine.begin() as conn:
        conn.execute(t.update().where(t.c[key] == tp["update_key"]).values({col: "TAMPERED"}))
        conn.execute(t.delete().where(t.c[key] == tp["delete_key"]))
        conn.execute(t.insert().values({key: tp["extra_key"]}))
    assert live.count(name) == n  # -1 +1

    rep = live.convert(tables=SUBSET, mode="sync", checksum=True)
    res = by_name(rep)
    assert rep.ok, rep.render_text()
    assert counters(res[name]) == (1, 1, 0, n - 2, 1), res[name]
    assert res[name].target_rows == n + 1 and res[name].checksum_match is True
    for other in set(SUBSET) - {name}:
        assert (res[other].inserted, res[other].updated, res[other].deleted) == ZERO, other
    assert live.count(name) == n + 1  # the extra row survives a plain sync

    rep = live.convert(tables=SUBSET, mode="sync", checksum=True, delete_missing=True)
    res = by_name(rep)
    assert rep.ok, rep.render_text()
    assert counters(res[name]) == (0, 0, 1, n, 0), res[name]
    assert live.count(name) == n

    rep = live.convert(tables=SUBSET, mode="sync", checksum=True, delete_missing=True)
    assert rep.ok, rep.render_text()
    for r in rep.tables:
        assert (r.inserted, r.updated, r.deleted) == ZERO, r.name


def test_append_twice_duplicate_key_leaves_target_unchanged(live):
    names = [*SUBSET, NOKEY]
    rep = live.convert(tables=names)
    assert rep.ok, rep.render_text()
    before = {n: live.rows(n) for n in names}
    rep = live.convert(tables=names, mode="append")
    res = by_name(rep)
    for n in SUBSET:  # keyed: the whole table's insert is rolled back
        assert res[n].status == "error", (n, res[n])
        assert res[n].error
        assert live.rows(n) == before[n], n
    # a table without any key cannot collide: the rows are simply doubled
    assert res[NOKEY].status == "pass", res[NOKEY]
    assert live.count(NOKEY) == 2 * len(before[NOKEY])


def test_schema_acade(live):
    need(live, "pg", "mssql")
    if "acade" not in inspect(live.engine).get_schema_names():
        pytest.skip("schema 'acade' does not exist (docker/entrypoint.sh creates it)")
    names = ["COMP", "TERMS"]
    try:
        rep = live.convert(tables=names, schema="acade", checksum=True)
        assert rep.ok, rep.render_text()
        assert set(names) <= set(live.table_names("acade"))
        assert not set(names) & set(live.table_names())  # nothing leaked into the test's schema
        assert live.table("COMP", "acade").schema == "acade"
    finally:
        md = MetaData(schema="acade")
        for n in names:
            if n in inspect(live.engine).get_table_names(schema="acade"):
                Table(n, md, autoload_with=live.engine, schema="acade").drop(live.engine)


def test_mssql_collation_guard_case_only_key(live):
    need(live, "mssql")
    with live.engine.connect() as conn:
        coll = conn.execute(
            text("SELECT CONVERT(varchar(128), DATABASEPROPERTYEX(DB_NAME(), 'Collation'))")
        ).scalar_one()
    if "_CI_" not in coll:
        pytest.skip(f"database collation {coll} is case sensitive")
    tp = live.source.tamper
    name, key = tp["table"], tp["key"]
    rep = live.convert(tables=[name])
    assert rep.ok, rep.render_text()
    t = live.table(name)
    with live.engine.begin() as conn:  # a case-only difference to the source key
        conn.execute(
            t.update().where(t.c[key] == tp["case_key"]).values({key: tp["case_key"].lower()})
        )
    before = live.rows(name)
    assert tp["case_key"].lower() in {r[t.c.keys().index(key)] for r in before}
    rep = live.convert(tables=[name], mode="sync", delete_missing=True)
    res = by_name(rep)[name]
    assert res.status == "error", res
    assert "collation" in (res.error or "").lower(), res.error
    assert live.rows(name) == before  # nothing was changed


def test_pg_nul_in_text_is_a_table_error(live, install_reader):
    need(live, "pg")
    info = make_info("N", [("id", "COUNTER"), ("s", "VARCHAR", 20)], pk=("id",))
    install_reader(FakeReader({"N": (info, [(1, "ok"), (2, "a\x00b")])}))
    rep = converter.convert("fake.mdb", live.url, schema=live.schema)
    res = by_name(rep)["N"]
    assert res.status == "error"
    assert "NUL" in (res.error or ""), res.error


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def test_real_columns_second_sync_updates_nothing(live, install_reader):
    """float4 round trip: REAL values must compare equal after a trip through the target."""
    values = [0.1, 3.14159, 1e-3, 16777216.0, -2.5e10, 1.0e-30, 0.0]
    rows = [(i, _f32(v)) for i, v in enumerate(values, 1)]
    rows += [(len(rows) + 1, None), (len(rows) + 2, 0.1)]  # NULL, and a value that is not float4
    info = make_info("R", [("id", "COUNTER"), ("v", "REAL")], pk=("id",))
    install_reader(FakeReader({"R": (info, rows)}))
    kw = {"schema": live.schema, "checksum": True}
    rep = converter.convert("fake.mdb", live.url, **kw)
    assert rep.ok, rep.render_text()
    for _ in range(2):
        rep = converter.convert("fake.mdb", live.url, mode="sync", **kw)
        res = by_name(rep)["R"]
        assert rep.ok, rep.render_text()
        assert (res.inserted, res.updated, res.deleted) == ZERO, res
        assert res.unchanged == len(rows)


def test_dry_run_changes_nothing(live):
    rep = live.convert(tables=SUBSET, dry_run=True)
    assert rep.ok, rep.render_text()
    assert rep.dry_run
    assert live.table_names() == []  # nothing created
    rep = live.convert(tables=SUBSET)
    assert rep.ok, rep.render_text()
    before = {n: live.rows(n) for n in SUBSET}
    for mode in ("full", "sync"):
        rep = live.convert(tables=SUBSET, mode=mode, dry_run=True, delete_missing=mode == "sync")
        assert rep.ok, rep.render_text()
    assert {n: live.rows(n) for n in SUBSET} == before
    assert live.table_names() == sorted(SUBSET)
