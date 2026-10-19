"""Review-gate-2 regression tests (M4). Driver-free: a fake Reader feeds a real SQLite target.

Covers: collation-mismatch guard, pre-commit verification, native counters, column-aware value
normalisation, partial unique indexes, SQL Server temp-key DDL and lookup compilation, --schema
validation, dry-run details, connect timeouts, --no-key validation, read failures, delete-missing
with composite keys.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from urllib.parse import quote

import pytest
import sqlalchemy
from sqlalchemy import MetaData
from sqlalchemy.dialects import mssql, postgresql
from sqlalchemy.schema import CreateTable
from tests.fakes import FakeReader, by_name, dump, make_info, sql, sqlite_url, std_info, std_rows

from acade2sql import cli, converter
from acade2sql.errors import UsageError
from acade2sql.keys import parse_key_specs
from acade2sql.modes import RunOptions, run_sync
from acade2sql.validate import TableResult, normalize_value, table_checksum
from acade2sql.writers import SqlAlchemyWriter, build_table
from acade2sql.writers import sqlalchemy_writer as sw
from acade2sql.writers.sync import SyncSession, same_row

G = "0A1B2C3D-0000-0000-0000-00000000ABCD"
MSSQL = "mssql+pyodbc://u:pw@h/db?driver=ODBC+Driver+18+for+SQL+Server"
PG = "postgresql+psycopg://u:pw@h/db"


def conv(tmp_path, reader, install_reader, name="o.sqlite", **kw):
    install_reader(reader)
    return converter.convert("fake.mdb", sqlite_url(tmp_path / name), **kw)


def run_cli(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def one(rep, name):
    return by_name(rep)[name]


# ---- 1. collation mismatch ---------------------------------------------------------------------

NOCASE_INFO = make_info("N", [("tag", "VARCHAR", 10, False), ("v", "VARCHAR", 10)], pk=("tag",))


def precreate(db, ddl, inserts):
    sql(db, ddl)
    for stmt in inserts:
        sql(db, stmt)


@pytest.mark.parametrize("checksum", [False, True], ids=["nochecksum", "checksum"])
@pytest.mark.parametrize("delete_missing", [False, True], ids=["keep", "delete"])
def test_collation_mismatch_pk_is_a_safe_error(
    tmp_path, install_reader, impl, delete_missing, checksum
):
    db = tmp_path / "o.sqlite"
    precreate(
        db,
        'CREATE TABLE "N"(tag VARCHAR(10) COLLATE NOCASE NOT NULL PRIMARY KEY, v VARCHAR(10))',
        ["INSERT INTO N VALUES ('m1','old')"],
    )
    reader = FakeReader({"N": (NOCASE_INFO, [("M1", "new")])})
    rep = conv(
        tmp_path, reader, install_reader, mode="sync",
        delete_missing=delete_missing, checksum=checksum,
    )  # fmt: skip
    t = one(rep, "N")
    assert t.status == "error", rep.render_text()
    assert "collation" in t.error.lower()
    assert "--mode full" in t.error
    assert rep.ok is False
    assert sql(db, "SELECT * FROM N") == [("m1", "old")]


def test_collation_mismatch_cli_exit_3(tmp_path, capsys, install_reader, impl):
    db = tmp_path / "c.sqlite"
    precreate(
        db,
        'CREATE TABLE "N"(tag VARCHAR(10) COLLATE NOCASE NOT NULL PRIMARY KEY, v VARCHAR(10))',
        ["INSERT INTO N VALUES ('m1','old')"],
    )
    install_reader(FakeReader({"N": (NOCASE_INFO, [("M1", "new")])}))
    rc, out, _err = run_cli(
        capsys, "convert", "fake.mdb", "--target", str(db), "--mode", "sync", "--delete-missing"
    )
    assert rc == 3, out
    assert "collation" in out.lower()
    assert sql(db, "SELECT * FROM N") == [("m1", "old")]


@pytest.mark.parametrize("delete_missing", [False, True], ids=["keep", "delete"])
def test_collation_mismatch_non_unique_key_no_duplicate(
    tmp_path, install_reader, impl, delete_missing
):
    info = make_info(
        "N2",
        [("id", "INTEGER", None, False), ("tag", "VARCHAR", 10), ("v", "VARCHAR", 10)],
        pk=("id",),
    )
    db = tmp_path / "o.sqlite"
    precreate(
        db,
        'CREATE TABLE "N2"(id INTEGER NOT NULL PRIMARY KEY, tag VARCHAR(10) COLLATE NOCASE, '
        "v VARCHAR(10))",
        ["INSERT INTO N2 VALUES (1,'m1','old')"],
    )
    reader = FakeReader({"N2": (info, [(5, "M1", "new")])})
    rep = conv(
        tmp_path, reader, install_reader, mode="sync", key=["N2=tag"], delete_missing=delete_missing
    )
    t = one(rep, "N2")
    assert t.status == "error", rep.render_text()
    assert "collation" in t.error.lower()
    assert "--mode full" in t.error
    assert rep.ok is False
    assert sql(db, "SELECT * FROM N2") == [(1, "m1", "old")]


@pytest.mark.parametrize("delete_missing", [False, True], ids=["keep", "delete"])
def test_collation_mismatch_rtrim_trailing_space(tmp_path, install_reader, impl, delete_missing):
    db = tmp_path / "o.sqlite"
    precreate(
        db,
        'CREATE TABLE "N"(tag VARCHAR(10) COLLATE RTRIM NOT NULL PRIMARY KEY, v VARCHAR(10))',
        ["INSERT INTO N VALUES ('m1','old')"],
    )
    reader = FakeReader({"N": (NOCASE_INFO, [("m1  ", "new")])})
    rep = conv(tmp_path, reader, install_reader, mode="sync", delete_missing=delete_missing)
    t = one(rep, "N")
    assert t.status == "error", rep.render_text()
    assert "collation" in t.error.lower()
    assert "--mode full" in t.error
    assert sql(db, "SELECT * FROM N") == [("m1", "old")]


def test_binary_collation_target_still_syncs(tmp_path, install_reader, impl):
    """Control: the guard must not fire for a normal (case-sensitive) target."""
    reader = FakeReader({"N": (NOCASE_INFO, [("m1", "old")])})
    assert conv(tmp_path, reader, install_reader).ok
    reader = FakeReader({"N": (NOCASE_INFO, [("m1", "new"), ("M1", "upper")])})
    rep = conv(tmp_path, reader, install_reader, mode="sync", checksum=True)
    t = one(rep, "N")
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated) == (1, 1)
    assert dump(tmp_path / "o.sqlite", "N") == [("M1", "upper"), ("m1", "new")]


# ---- 2. pre-commit guard ------------------------------------------------------------------------


def test_precommit_count_disagreement_rolls_back(tmp_path, install_reader, impl, monkeypatch):
    info = std_info("T")
    assert conv(tmp_path, FakeReader({"T": (info, std_rows(range(1, 4)))}), install_reader).ok
    db = tmp_path / "o.sqlite"
    before = dump(db, "T")

    real = SyncSession.count_matching

    def short(self):
        return real(self) - 1

    monkeypatch.setattr(SyncSession, "count_matching", short)
    new_rows = std_rows(range(1, 4), tag="X") + std_rows([9])
    rep = conv(tmp_path, FakeReader({"T": (info, new_rows)}), install_reader, mode="sync")
    t = one(rep, "T")
    assert t.status == "error", rep.render_text()
    assert "sync verification failed" in t.error
    assert rep.ok is False
    assert dump(db, "T") == before


def test_precommit_update_affecting_zero_rows_rolls_back(tmp_path, install_reader, monkeypatch):
    """Generic path: UPDATEs that silently match nothing must not commit a partial sync."""
    monkeypatch.setattr(SqlAlchemyWriter, "_force_generic_sync", True)
    info = std_info("T")
    assert conv(tmp_path, FakeReader({"T": (info, std_rows(range(1, 4)))}), install_reader).ok
    db = tmp_path / "o.sqlite"
    before = dump(db, "T")

    real = SyncSession._update_stmt

    def never_matching(self):
        stmt, binds = real(self)
        return stmt.where(sqlalchemy.false()), binds

    monkeypatch.setattr(SyncSession, "_update_stmt", never_matching)
    rep = conv(
        tmp_path,
        FakeReader({"T": (info, std_rows(range(1, 4), tag="X"))}),
        install_reader,
        mode="sync",
    )
    t = one(rep, "T")
    assert t.status == "error", rep.render_text()
    assert "sync verification failed" in t.error
    assert dump(db, "T") == before


# ---- 3. native counters -------------------------------------------------------------------------


def test_native_update_counts_as_updated_not_inserted(tmp_path, install_reader, impl, monkeypatch):
    used = []
    real_load = SyncSession.load

    def spy(self, *a, **k):
        try:
            return real_load(self, *a, **k)
        finally:
            used.append(self.native_used)

    monkeypatch.setattr(SyncSession, "load", spy)
    info = std_info("T")
    assert conv(tmp_path, FakeReader({"T": (info, std_rows(range(1, 6)))}), install_reader).ok
    src = std_rows([1, 2], tag="CHANGED") + std_rows([3, 4, 5]) + std_rows([6, 7])
    rep = conv(tmp_path, FakeReader({"T": (info, src)}), install_reader, mode="sync")
    t = one(rep, "T")
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated, t.unchanged, t.deleted or 0) == (2, 2, 3, 0)
    assert used == [impl == "native"]
    assert dump(tmp_path / "o.sqlite", "T") == sorted(src)


# ---- 4. column-aware normalisation -------------------------------------------------------------

GUID_LOW = G.lower()


@pytest.mark.parametrize(
    "a,b",
    [
        (GUID_LOW, G),
        ("{" + G + "}", G),
        ("{" + GUID_LOW + "}", G),
        (G, "{" + GUID_LOW + "}"),
    ],
)
def test_guid_column_folds_case_and_braces(a, b):
    assert normalize_value(a, "GUID") == normalize_value(b, "GUID")
    assert table_checksum([[(a,)]], column_types=["GUID"]) == table_checksum(
        [[(b,)]], column_types=["GUID"]
    )
    assert same_row((a,), (b,), ["GUID"])


@pytest.mark.parametrize("a,b", [(GUID_LOW, G), ("{" + G + "}", G), ("{" + GUID_LOW + "}", G)])
@pytest.mark.parametrize("ctype", ["VARCHAR", "LONGCHAR", None])
def test_text_column_with_guid_shaped_values_is_exact(a, b, ctype):
    assert normalize_value(a, ctype) != normalize_value(b, ctype)
    types = None if ctype is None else [ctype]
    assert table_checksum([[(a,)]], column_types=types) != table_checksum(
        [[(b,)]], column_types=types
    )
    assert not same_row((a,), (b,), [ctype])


def test_no_column_types_means_no_guid_folding():
    assert normalize_value(GUID_LOW) != normalize_value(G)
    assert table_checksum([[(GUID_LOW,)]]) != table_checksum([[(G,)]])
    assert table_checksum([[(GUID_LOW,)]], None) != table_checksum([[(G,)]], None)


def test_guid_column_only_folds_balanced_guid_shapes():
    assert normalize_value("{" + G, "GUID") != normalize_value(G, "GUID")
    assert normalize_value("not-a-guid", "GUID") != normalize_value("NOT-A-GUID", "GUID")


def test_real_column_compares_at_single_precision():
    single = 0.10000000149011612  # float32(0.1) widened to double
    assert normalize_value(0.1, "REAL") == normalize_value(single, "REAL")
    assert table_checksum([[(0.1,)]], column_types=["REAL"]) == table_checksum(
        [[(single,)]], column_types=["REAL"]
    )
    assert same_row((0.1,), (single,), ["REAL"])
    assert normalize_value(0.1, "DOUBLE") != normalize_value(single, "DOUBLE")
    assert table_checksum([[(0.1,)]], column_types=["DOUBLE"]) != table_checksum(
        [[(single,)]], column_types=["DOUBLE"]
    )
    assert not same_row((0.1,), (single,), ["DOUBLE"])


def test_real_column_still_distinguishes_different_values():
    assert normalize_value(0.1, "REAL") != normalize_value(0.2, "REAL")
    assert normalize_value(None, "REAL") != normalize_value(0.0, "REAL")


def test_same_row_mixed_columns():
    types = ["INTEGER", "GUID", "VARCHAR"]
    assert same_row((1, G, "x"), (1, GUID_LOW, "x"), types)
    assert not same_row((1, G, "x"), (1, G, "X"), types)
    assert not same_row((1, G, GUID_LOW), (1, G, G), types)
    assert not same_row((1, G, "x"), (1, G), types)


def _sync_ref_col(tmp_path, install_reader, ref_type, old, new, **kw):
    info = make_info("H", [("id", "INTEGER", None, False), ("ref", ref_type, 40)], pk=("id",))
    assert conv(tmp_path, FakeReader({"H": (info, [(1, old)])}), install_reader).ok
    rep = conv(tmp_path, FakeReader({"H": (info, [(1, new)])}), install_reader, mode="sync", **kw)
    return rep, one(rep, "H"), dump(tmp_path / "o.sqlite", "H")


@pytest.mark.parametrize(
    "old,new",
    [
        (GUID_LOW, G),
        (G, GUID_LOW),
        (G, "{" + G + "}"),
        ("{" + G + "}", G),
        (GUID_LOW, "{" + G + "}"),
    ],
)
def test_sync_varchar_guid_shaped_change_is_an_update(tmp_path, install_reader, impl, old, new):
    rep, t, rows = _sync_ref_col(tmp_path, install_reader, "VARCHAR", old, new, checksum=True)
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated, t.unchanged) == (0, 1, 0)
    assert rows == [(1, new)]
    assert t.checksum_match is True
    # and the exact-text checksums of old and new really differ
    assert table_checksum([[(1, old)]], column_types=["INTEGER", "VARCHAR"]) != table_checksum(
        [[(1, new)]], column_types=["INTEGER", "VARCHAR"]
    )


@pytest.mark.parametrize("old,new", [(GUID_LOW, G), (G, "{" + G + "}"), ("{" + GUID_LOW + "}", G)])
def test_sync_guid_column_case_or_braces_is_unchanged(tmp_path, install_reader, impl, old, new):
    rep, t, rows = _sync_ref_col(tmp_path, install_reader, "GUID", old, new, checksum=True)
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated, t.unchanged) == (0, 0, 1)
    assert t.checksum_match is True
    assert rows == [(1, old)]


# ---- 5. partial unique index -----------------------------------------------------------------


def test_partial_unique_index_key_syncs_and_is_idempotent(tmp_path, impl):
    db = str(tmp_path / "p.sqlite")
    info = make_info(
        "P",
        [("id", "INTEGER", None, False), ("tag", "VARCHAR", 10), ("v", "VARCHAR", 10)],
        pk=("id",),
    )
    with SqlAlchemyWriter("sqlite:///" + db, _force_generic_sync=impl == "generic") as w:
        w.prepare([info], mode="sync")
        w.ensure_table(info)
    sql(db, 'CREATE UNIQUE INDEX ux ON "P"(tag) WHERE tag IS NOT NULL')
    sql(db, "INSERT INTO P VALUES (1,'a','x')")
    reader = FakeReader({"P": (info, [(1, "a", "y"), (2, "b", "z")])})
    results = []
    for _ in range(2):
        with SqlAlchemyWriter("sqlite:///" + db, _force_generic_sync=impl == "generic") as w:
            w.prepare([info], mode="sync")
            res = TableResult("P", None, None, "pass")
            opts = RunOptions(mode="sync", checksum=True, keys=parse_key_specs(["P=tag"]))
            run_sync(reader, w, info, opts, res)
            results.append(res)
    first, second = results
    assert first.status == "pass", first
    assert (first.inserted, first.updated, first.unchanged) == (1, 1, 0)
    assert first.checksum_match is True
    assert second.status == "pass"
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert sql(db, "SELECT * FROM P ORDER BY id") == [(1, "a", "y"), (2, "b", "z")]


# ---- 6./7. SQL Server temp key table and lookup compilation -----------------------------------


def make_session(tmp_path, columns, key):
    """A SyncSession over a SQLite engine, never entered (no connection is needed)."""
    info = make_info("K", columns, pk=key)
    table = build_table(MetaData(), info, "mssql", used_index_names=set())
    engine = sqlalchemy.create_engine("sqlite://")
    return SyncSession(engine, table, key, native=False, guard=lambda _b: None, table_label="K")


class StubConn:
    """Records the statements a session executes; reports ``dialect`` as its dialect."""

    def __init__(self, dialect):
        self.dialect = dialect
        self.compiled = []

    def execute(self, stmt, *a, **k):
        self.compiled.append(
            stmt.compile(dialect=self.dialect, compile_kwargs={"render_postcompile": True})
        )
        return iter(())


def mssql_session(tmp_path, columns, key):
    s = make_session(tmp_path, columns, key)
    s._conn = StubConn(mssql.dialect())
    return s


def ddl(table, dialect):
    return str(CreateTable(table).compile(dialect=dialect))


def test_mssql_temp_key_table_wide_composite_key_has_no_primary_key(tmp_path):
    s = mssql_session(
        tmp_path,
        [("a", "VARCHAR", 255, False), ("b", "VARCHAR", 255, False), ("v", "VARCHAR", 10)],
        ("a", "b"),
    )
    temp = s._make_temp()
    text = ddl(temp, mssql.dialect())
    assert "PRIMARY KEY" not in text.upper()
    assert temp.name.startswith("#")
    assert "COLLATE DATABASE_DEFAULT" in text


def test_mssql_temp_key_table_narrow_composite_key_has_primary_key(tmp_path):
    s = mssql_session(
        tmp_path,
        [("a", "VARCHAR", 100, False), ("b", "VARCHAR", 100, False), ("v", "VARCHAR", 10)],
        ("a", "b"),
    )
    text = ddl(s._make_temp(), mssql.dialect())
    assert "PRIMARY KEY" in text.upper()
    assert "#" in text
    assert text.upper().count("COLLATE DATABASE_DEFAULT") == 2


def test_mssql_generic_lookup_stays_under_parameter_limit(tmp_path):
    s = mssql_session(
        tmp_path,
        [("a", "INTEGER", None, False), ("b", "VARCHAR", 40, False), ("v", "VARCHAR", 10)],
        ("a", "b"),
    )
    keys = [(i, f"k{i}") for i in range(5000)]
    s._fetch_existing(keys)
    stmts = s._conn.compiled
    assert len(stmts) >= 5000 * 2 // 2100
    seen = set()
    for c in stmts:
        text = str(c)
        assert len(c.params) <= 2100, len(c.params)
        assert " IN ((" not in text.upper() and ") IN " not in text.upper()
        seen.update(v for name, v in c.params.items() if isinstance(v, str) and v.startswith("k"))
        seen.update(v for v in c.params.values() if isinstance(v, int))
    assert {k for _a, k in keys} <= seen
    assert {a for a, _k in keys} <= seen


def test_mssql_single_column_lookup_stays_under_parameter_limit(tmp_path):
    s = mssql_session(tmp_path, [("a", "INTEGER", None, False), ("v", "VARCHAR", 10)], ("a",))
    s._fetch_existing([(i,) for i in range(5000)])
    covered = set()
    for c in s._conn.compiled:
        assert len(c.params) <= 2100
        covered.update(c.params.values())
    assert covered >= set(range(5000))


def test_pg_composite_lookup_uses_row_value_in(tmp_path):
    s = make_session(
        tmp_path,
        [("a", "INTEGER", None, False), ("b", "VARCHAR", 40, False), ("v", "VARCHAR", 10)],
        ("a", "b"),
    )
    s._conn = StubConn(postgresql.dialect())
    keys = [(i, f"k{i}") for i in range(100)]
    s._fetch_existing(keys)
    text = " ".join(str(c) for c in s._conn.compiled)
    assert "(" in text and " IN (" in text.replace("\n", " ")
    assert "(k.a, k.b) in" in text.lower().replace('"', "")
    assert sum(len(c.params) for c in s._conn.compiled) == 200


# ---- 8. --schema validation -------------------------------------------------------------------


@pytest.mark.parametrize("url", [MSSQL, PG], ids=["mssql", "pg"])
@pytest.mark.parametrize("bad", ["a.b", "x].[y", 'a"b', "tab\tx", "", "  "])
def test_bad_schema_is_a_usage_error_cli(capsys, tmp_path, install_reader, url, bad):
    install_reader(FakeReader({"T": (std_info("T"), std_rows([1]))}))
    rc, out, err = run_cli(
        capsys, "convert", "fake.mdb", "--target", url, "--dry-run", "--schema", bad
    )
    assert rc == 2, (out, err)
    assert "schema" in err.lower()


@pytest.mark.parametrize("bad", ["a.b", "x].[y", 'a"b', "tab\tx", "", "  "])
def test_bad_schema_raises_usage_error_api(install_reader, bad):
    install_reader(FakeReader({"T": (std_info("T"), std_rows([1]))}))
    with pytest.raises(UsageError, match=r"(?i)schema"):
        converter.convert("fake.mdb", MSSQL, dry_run=True, schema=bad)


@pytest.mark.parametrize("url", [MSSQL, PG], ids=["mssql", "pg"])
@pytest.mark.parametrize("good", ["acade", "dbo"])
def test_good_schema_accepted_dry_run(capsys, install_reader, url, good):
    install_reader(FakeReader({"T": (std_info("T"), std_rows([1]))}))
    rc, out, err = run_cli(
        capsys, "convert", "fake.mdb", "--target", url, "--dry-run", "--schema", good
    )
    assert rc == 0, err
    assert good in out


# ---- 9. dry-run with an unmapped type ---------------------------------------------------------


@pytest.mark.parametrize("which", ["sqlite", "mssql", "pg"])
def test_dry_run_unmapped_type_fails_the_table(capsys, tmp_path, install_reader, which):
    info = make_info("W", [("id", "INTEGER", None, False), ("mystery", "WEIRDTYPE")], pk=("id",))
    install_reader(FakeReader({"W": (info, [(1, "x")])}))
    target = {"sqlite": str(tmp_path / "never.sqlite"), "mssql": MSSQL, "pg": PG}[which]
    rep = converter.convert("fake.mdb", target, dry_run=True)
    t = one(rep, "W")
    assert t.status == "fail"
    assert any("unmapped type WEIRDTYPE" in w for w in t.warnings)
    assert rep.ok is False
    rc, out, _err = run_cli(capsys, "convert", "fake.mdb", "--target", target, "--dry-run")
    assert rc == 3
    assert "unmapped type" in out
    assert not (tmp_path / "never.sqlite").exists()


# ---- 10. DROP TABLE IF EXISTS in dry-run DDL --------------------------------------------------


@pytest.mark.parametrize("which", ["sqlite", "mssql", "pg"])
def test_dry_run_uses_drop_table_if_exists(tmp_path, install_reader, which):
    install_reader(FakeReader({n: (std_info(n), std_rows([1])) for n in ("A", "Order", "B C")}))
    target = {"sqlite": str(tmp_path / "never.sqlite"), "mssql": MSSQL, "pg": PG}[which]
    rep = converter.convert("fake.mdb", target, dry_run=True)
    drops = [s for s in rep.ddl if s.upper().startswith("DROP")]
    assert len(drops) == 3
    assert all(s.upper().startswith("DROP TABLE IF EXISTS") for s in drops)


# ---- 11. connect timeouts ---------------------------------------------------------------------


class _Stop(Exception):
    pass


@pytest.fixture
def engine_kwargs(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kw):
        captured["url"] = url
        captured["kwargs"] = kw
        raise _Stop

    monkeypatch.setattr(sw, "create_engine", fake_create_engine)

    def get(url):
        captured.clear()
        with pytest.raises(_Stop):
            sw._create_engine(url, False)
        return captured["kwargs"]

    return get


def test_pg8000_gets_no_connect_timeout(engine_kwargs):
    kw = engine_kwargs("postgresql+pg8000://u:p@h/db")
    assert "connect_timeout" not in kw.get("connect_args", {})
    assert "timeout" not in kw.get("connect_args", {})


def test_psycopg_gets_15s_connect_timeout(engine_kwargs):
    assert engine_kwargs("postgresql+psycopg://u:p@h/db")["connect_args"]["connect_timeout"] == 15


def test_url_connect_timeout_is_respected(engine_kwargs):
    kw = engine_kwargs("postgresql+psycopg://u:p@h/db?connect_timeout=5")
    assert "connect_timeout" not in kw.get("connect_args", {})


def test_mssql_pyodbc_gets_15s_timeout(engine_kwargs):
    kw = engine_kwargs(MSSQL)
    assert kw["connect_args"]["timeout"] == 15
    assert kw["fast_executemany"] is True


def test_mssql_url_timeout_is_respected(engine_kwargs):
    kw = engine_kwargs(MSSQL + "&timeout=5")
    assert "timeout" not in kw.get("connect_args", {})


def test_mssql_odbc_connect_timeout_is_respected(engine_kwargs):
    odbc = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=h;DATABASE=db;Connection Timeout=7"
    kw = engine_kwargs("mssql+pyodbc:///?odbc_connect=" + quote(odbc))
    assert "timeout" not in kw.get("connect_args", {})


def test_mssql_odbc_connect_without_timeout_gets_ours(engine_kwargs):
    odbc = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=h;DATABASE=db"
    kw = engine_kwargs("mssql+pyodbc:///?odbc_connect=" + quote(odbc))
    assert kw["connect_args"]["timeout"] == 15


# ---- 12. --no-key validation, dry-run key -----------------------------------------------------


@pytest.mark.parametrize("mode", ["full", "append"])
def test_no_key_replace_requires_sync_cli(capsys, tmp_path, install_reader, mode):
    install_reader(FakeReader({"T": (std_info("T"), std_rows([1]))}))
    db = tmp_path / "o.sqlite"
    rc, _out, err = run_cli(
        capsys, "convert", "fake.mdb", "--target", str(db), "--mode", mode, "--no-key", "replace"
    )
    assert rc == 2
    assert "no-key" in err.lower() or "no_key" in err.lower()
    assert not db.exists()


def test_no_key_replace_requires_sync_api(tmp_path, install_reader):
    install_reader(FakeReader({"T": (std_info("T"), std_rows([1]))}))
    with pytest.raises(UsageError):
        converter.convert("fake.mdb", sqlite_url(tmp_path / "o.sqlite"), no_key="replace")


def test_no_key_replace_with_sync_is_accepted(tmp_path, install_reader):
    info = make_info("K", [("a", "INTEGER"), ("b", "VARCHAR", 5)], pk=())
    rep = conv(
        tmp_path, FakeReader({"K": (info, [(1, "x")])}), install_reader,
        mode="sync", no_key="replace",
    )  # fmt: skip
    assert rep.ok, rep.render_text()


def test_dry_run_keyless_table_key_is_none_not_empty(tmp_path, install_reader):
    info = make_info("K", [("a", "INTEGER"), ("b", "VARCHAR", 5)], pk=())
    install_reader(FakeReader({"K": (info, [(1, "x")])}))
    target = str(tmp_path / "never.sqlite")
    rep = converter.convert("fake.mdb", target, mode="sync", no_key="replace", dry_run=True)
    t = one(rep, "K")
    assert t.status == "planned"
    assert t.key is None
    assert "replace-rows" in t.actions
    rep = converter.convert("fake.mdb", target, mode="sync", dry_run=True)
    t = one(rep, "K")
    assert t.key is None
    assert t.status == "error"
    assert rep.to_dict()["tables"][0]["key"] is None


# ---- 13. source read failure midway through a sync --------------------------------------------


class FailingReader(FakeReader):
    """Raises on the second batch."""

    fail = False

    def iter_rows(self, name, batch_size=5000):
        for i, batch in enumerate(super().iter_rows(name, batch_size)):
            if self.fail and i > 0:
                raise RuntimeError("source went away")
            yield batch


@pytest.mark.parametrize("delete_missing", [False, True], ids=["keep", "delete"])
def test_sync_source_failure_midway_rolls_back(tmp_path, install_reader, impl, delete_missing):
    info = std_info("T")
    reader = FailingReader({"T": (info, std_rows(range(1, 6)))})
    assert conv(tmp_path, reader, install_reader).ok
    db = tmp_path / "o.sqlite"
    before = dump(db, "T")
    reader.tables["T"] = (info, std_rows(range(1, 4), tag="X") + std_rows(range(10, 13)))
    reader.fail = True
    rep = conv(
        tmp_path, reader, install_reader, mode="sync", batch_size=2, delete_missing=delete_missing
    )
    t = one(rep, "T")
    assert t.status == "error", rep.render_text()
    assert "read failed" in t.error
    assert "source went away" in t.error
    assert rep.ok is False
    assert dump(db, "T") == before


# ---- 14. delete-missing with composite keys ---------------------------------------------------


def _composite(tmp_path, install_reader, info, target_rows, source_rows):
    assert conv(tmp_path, FakeReader({"C": (info, target_rows)}), install_reader).ok
    return conv(
        tmp_path, FakeReader({"C": (info, source_rows)}), install_reader,
        mode="sync", delete_missing=True, checksum=True,
    )  # fmt: skip


def test_delete_missing_composite_key_varchar_case_is_exact(tmp_path, install_reader, impl):
    info = make_info(
        "C",
        [("n", "INTEGER", None, False), ("s", "VARCHAR", 40, False), ("v", "VARCHAR", 10)],
        pk=("n", "s"),
    )
    target = [(1, GUID_LOW, "t"), (1, "other", "t"), (2, G, "t")]
    source = [(1, G, "s"), (2, G, "s")]
    rep = _composite(tmp_path, install_reader, info, target, source)
    t = one(rep, "C")
    assert rep.ok, rep.render_text()
    assert t.checksum_match is True
    assert (t.source_rows, t.target_rows, t.extra_rows or 0) == (2, 2, 0)
    # text keys are case-sensitive: the lower-case row is a different key and is deleted
    assert (t.inserted, t.updated, t.deleted) == (1, 1, 2)
    assert dump(tmp_path / "o.sqlite", "C") == sorted(source)


@pytest.mark.parametrize("variant", ["guid_case", "guid_braces"])
def test_delete_missing_composite_key_guid_key_column(tmp_path, install_reader, impl, variant):
    info = make_info(
        "C",
        [("n", "INTEGER", None, False), ("g", "GUID", None, False), ("v", "VARCHAR", 10)],
        pk=("n", "g"),
    )
    old = GUID_LOW if variant == "guid_case" else "{" + G + "}"
    target = [(1, old, "t"), (2, G, "t"), (3, G, "gone")]
    source = [(1, G, "s"), (2, G, "t")]
    rep = _composite(tmp_path, install_reader, info, target, source)
    t = one(rep, "C")
    assert rep.ok, rep.render_text()
    assert t.checksum_match is True
    assert (t.source_rows, t.target_rows) == (2, 2)
    assert t.extra_rows in (0, None)
    rows = dump(tmp_path / "o.sqlite", "C")
    assert len(rows) == 2
    assert {r[0] for r in rows} == {1, 2}
    assert sorted(r[2] for r in rows) == ["s", "t"]  # row 2 kept, row 1 updated, row 3 deleted
    assert t.deleted >= 1  # (3, ...) at least


def test_delete_missing_composite_key_decimal_scale(tmp_path, install_reader, impl):
    info = make_info(
        "C",
        [("d", "DECIMAL", None, False), ("s", "VARCHAR", 10, False), ("v", "VARCHAR", 10)],
        pk=("d", "s"),
    )
    target = [(Decimal("1.5"), "a", "t"), (Decimal("2"), "a", "gone")]
    source = [(Decimal("1.50"), "a", "s")]
    rep = _composite(tmp_path, install_reader, info, target, source)
    t = one(rep, "C")
    assert rep.ok, rep.render_text()
    assert t.checksum_match is True
    assert (t.source_rows, t.target_rows) == (1, 1)
    rows = sql(tmp_path / "o.sqlite", "SELECT * FROM C")
    assert len(rows) == 1
    assert Decimal(rows[0][0]) == Decimal("1.5")
    assert rows[0][2] == "s"
    assert t.deleted >= 1


def test_datetime_extremes_survive_sync_normalisation(tmp_path, install_reader, impl):
    """Sanity: column types flowing through sync do not disturb non-text columns."""
    info = make_info("D", [("id", "INTEGER", None, False), ("ts", "DATETIME")], pk=("id",))
    rows = [(1, dt.datetime(1900, 1, 1)), (2, dt.datetime(9999, 12, 31, 23, 59, 59))]
    assert conv(tmp_path, FakeReader({"D": (info, rows)}), install_reader).ok
    rep = conv(
        tmp_path, FakeReader({"D": (info, rows)}), install_reader, mode="sync", checksum=True
    )
    t = one(rep, "D")
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated, t.unchanged) == (0, 0, 2)
