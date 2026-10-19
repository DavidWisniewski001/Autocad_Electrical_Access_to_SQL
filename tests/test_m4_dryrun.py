"""--dry-run and --schema (docs/M4_SPEC.md sections 2, 4, 5). No server, no DBAPI needed."""

from __future__ import annotations

import json
import re
import socket
import sys

import pytest
import sqlalchemy
from sqlalchemy.engine import Engine
from tests.fakes import FakeReader, by_name, make_info, std_info, std_rows

from acade2sql import cli, converter
from acade2sql.errors import Acade2SqlError

PASSWORD = "s3cr3tP4ss"
MSSQL = f"mssql+pyodbc://u:{PASSWORD}@h/db?driver=ODBC+Driver+18+for+SQL+Server"
PG = f"postgresql+psycopg://u:{PASSWORD}@h/db"


def sqlite_target(tmp_path):
    return str(tmp_path / "never.sqlite")


@pytest.fixture
def no_target_connection(monkeypatch):
    """Any attempt to create an engine, connect, or open a socket fails the test."""

    def boom(*_a, **_k):
        raise AssertionError("dry-run touched the target (engine/connection/socket)")

    monkeypatch.setattr(sqlalchemy, "create_engine", boom)
    monkeypatch.setattr("acade2sql.writers.sqlalchemy_writer.create_engine", boom, raising=False)
    monkeypatch.setattr(Engine, "connect", boom)
    monkeypatch.setattr(Engine, "begin", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    # psycopg / psycopg2 must not be needed (nor importable here)
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)


def make_reader():
    idx = (("ix_label", ("label",), False),)
    from acade2sql.schema import IndexInfo

    ixs = tuple(IndexInfo(*i) for i in idx)
    tables = {
        "A_TBL": (std_info("A_TBL", indexes=ixs), std_rows(range(1, 4))),
        "Order": (std_info("Order"), std_rows(range(1, 3))),
        "ALLEN BRADLEY": (std_info("ALLEN BRADLEY"), std_rows(range(1, 2))),
    }
    return FakeReader(tables)


def run_cli(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.mark.parametrize("which", ["sqlite", "mssql", "pg"])
def test_dry_run_full_prints_plan_and_ddl(
    capsys, tmp_path, install_reader, no_target_connection, which
):
    rd = install_reader(make_reader())
    target = {"sqlite": sqlite_target(tmp_path), "mssql": MSSQL, "pg": PG}[which]
    rp = tmp_path / "rep.json"
    rc, out, err = run_cli(
        capsys, "convert", "fake.mdb", "--target", target, "--dry-run", "--report", str(rp)
    )
    assert rc == 0, err
    assert PASSWORD not in out + err
    assert out.count("CREATE TABLE") == 3
    assert out.count("DROP TABLE IF EXISTS") == 3
    for name in rd.tables:
        assert name in out
    assert "CREATE INDEX" in out
    assert "full" in out  # plan shows the mode
    assert "***" in out or which == "sqlite"  # masked target
    # no engine -> nothing was created
    assert not (tmp_path / "never.sqlite").exists()
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert PASSWORD not in json.dumps(doc)
    assert doc["dry_run"] is True
    assert doc["ok"] is True
    assert {t["status"] for t in doc["tables"]} == {"planned"}
    assert {t["name"] for t in doc["tables"]} == set(rd.tables)


def test_dry_run_type_ddl_per_dialect(capsys, tmp_path, install_reader, no_target_connection):
    install_reader(make_reader())
    _, mssql, _ = run_cli(capsys, "convert", "fake.mdb", "--target", MSSQL, "--dry-run")
    _, pg, _ = run_cli(capsys, "convert", "fake.mdb", "--target", PG, "--dry-run")
    assert "NVARCHAR(max)" in mssql
    assert "NVARCHAR(50)" in mssql
    assert re.search(r"\bTEXT\b", pg)
    assert "VARCHAR(50)" in pg
    assert "NVARCHAR" not in pg


@pytest.mark.parametrize("mode", ["append", "sync"])
def test_dry_run_non_full_modes_never_drop(
    capsys, tmp_path, install_reader, no_target_connection, mode
):
    install_reader(make_reader())
    rc, out, err = run_cli(
        capsys, "convert", "fake.mdb", "--target", PG, "--dry-run", "--mode", mode
    )
    assert rc == 0, err
    assert "DROP TABLE" not in out
    assert out.count("CREATE TABLE") == 3
    assert mode in out
    assert PASSWORD not in out + err


def test_dry_run_sync_plan_shows_key(capsys, tmp_path, install_reader, no_target_connection):
    install_reader(make_reader())
    rc, out, _ = run_cli(
        capsys, "convert", "fake.mdb", "--target", PG, "--dry-run", "--mode", "sync",
        "--key", "A_TBL=code",
    )  # fmt: skip
    assert rc == 0
    plan_lines = [ln for ln in out.splitlines() if "A_TBL" in ln and "key" in ln.lower()]
    assert any("code" in ln for ln in plan_lines), out


def test_dry_run_counts_and_no_counts(capsys, tmp_path, install_reader, no_target_connection):
    rd = install_reader(make_reader())
    rp = tmp_path / "c.json"
    rc, _out, _ = run_cli(
        capsys, "convert", "fake.mdb", "--target", PG, "--dry-run", "--report", str(rp)
    )
    assert rc == 0
    assert sorted(rd.count_calls) == sorted(rd.tables)
    rows = {t["name"]: t["source_rows"] for t in json.loads(rp.read_text("utf-8"))["tables"]}
    assert rows == {"A_TBL": 3, "Order": 2, "ALLEN BRADLEY": 1}
    rd.count_calls.clear()
    rp2 = tmp_path / "n.json"
    rc, _out, _ = run_cli(
        capsys, "convert", "fake.mdb", "--target", PG, "--dry-run", "--no-counts",
        "--report", str(rp2),
    )  # fmt: skip
    assert rc == 0
    assert rd.count_calls == []
    doc = json.loads(rp2.read_text("utf-8"))
    assert all(t["source_rows"] is None for t in doc["tables"])
    assert doc["ok"] is True


def test_dry_run_api_returns_planned_report(tmp_path, install_reader, no_target_connection):
    install_reader(make_reader())
    rep = converter.convert("fake.mdb", PG, dry_run=True)
    assert rep.dry_run is True
    assert rep.ok
    assert {t.status for t in rep.tables} == {"planned"}
    assert PASSWORD not in rep.target
    assert PASSWORD not in json.dumps(rep.to_dict())


def test_dry_run_sqlite_file_not_created_and_existing_untouched(
    tmp_path, install_reader, no_target_connection
):
    install_reader(make_reader())
    existing = tmp_path / "e.sqlite"
    existing.write_bytes(b"")
    converter.convert("fake.mdb", "sqlite:///" + existing.as_posix(), dry_run=True)
    assert existing.read_bytes() == b""
    assert not (tmp_path / "e.sqlite-wal").exists()


# ---- --schema -----------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [MSSQL, PG], ids=["mssql", "pg"])
def test_schema_qualifies_ddl(capsys, tmp_path, install_reader, no_target_connection, url):
    install_reader(make_reader())
    rc, out, err = run_cli(
        capsys, "convert", "fake.mdb", "--target", url, "--dry-run", "--schema", "acade_s"
    )
    assert rc == 0, err
    q = r'["\[]?acade_s["\]]?\.'
    assert len(re.findall(rf"CREATE TABLE\s+{q}", out)) == 3
    assert len(re.findall(rf"DROP TABLE IF EXISTS\s+{q}", out)) == 3
    assert re.search(rf"CREATE INDEX .* ON\s+{q}", out)
    assert "acade_s" in out.split("CREATE TABLE")[0]  # also named in the plan


def test_schema_with_sqlite_is_usage_error_cli(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    db = tmp_path / "s.sqlite"
    rc, _out, err = run_cli(capsys, "convert", "fake.mdb", "--target", str(db), "--schema", "x")
    assert rc == 2
    assert "schema" in err.lower()
    assert not db.exists()
    rc, *_ = run_cli(
        capsys, "convert", "fake.mdb", "--target", str(db), "--schema", "x", "--dry-run"
    )
    assert rc == 2


def test_schema_with_sqlite_api_valueerror(tmp_path, install_reader):
    install_reader(make_reader())
    db = tmp_path / "s2.sqlite"
    with pytest.raises(ValueError):
        converter.convert("fake.mdb", "sqlite:///" + db.as_posix(), schema="x")
    assert not db.exists()


# ---- invalid combos -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--delete-missing"],
        ["--delete-missing", "--mode", "append"],
        ["--delete-missing", "--mode", "full"],
        ["--key", "id"],
        ["--key", "T=id", "--mode", "append"],
        ["--mode", "bogus"],
    ],
)
def test_invalid_option_combos_exit_2(capsys, tmp_path, install_reader, extra):
    install_reader(make_reader())
    db = tmp_path / "x.sqlite"
    rc, _out, err = run_cli(capsys, "convert", "fake.mdb", "--target", str(db), *extra)
    assert rc == 2
    assert err.strip()
    assert not db.exists()


def test_valid_new_flags_accepted(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    rc, *_ = run_cli(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "ok.sqlite"),
        "--mode", "sync", "--key", "id", "--delete-missing", "--no-key", "error",
    )  # fmt: skip
    assert rc == 0


# ---- dialect hardening: mssql indexes ------------------------------------------------------------


def idx_info(name, cols_spec, indexes, pk=("id",)):
    from acade2sql.schema import IndexInfo

    return make_info(
        name,
        cols_spec,
        pk=pk,
        indexes=tuple(IndexInfo(n, tuple(c), u) for n, c, u in indexes),
    )


def ddl_of(capsys, tmp_path, url, tables, monkeypatch, *extra):
    rd = FakeReader({n: (i, []) for n, i in tables.items()})
    monkeypatch.setattr(converter, "open_reader", lambda *a, **k: rd)
    rc = cli.main(["convert", "fake.mdb", "--target", url, "--dry-run", *extra])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


ID = ("id", "COUNTER", None, False)


def v(name, n):
    return (name, "VARCHAR", n)


def test_mssql_index_skip_rules(capsys, tmp_path, monkeypatch, no_target_connection):
    tables = {
        "LOB": idx_info(
            "LOB",
            [ID, ("memo", "LONGCHAR"), v("a", 50)],
            [("ix_memo", ["memo"], False), ("ix_mix", ["a", "memo"], True), ("ix_a", ["a"], False)],
        ),
        "WIDE": idx_info(
            "WIDE",
            [ID, v("c1", 255), v("c2", 255), v("c3", 255), v("c4", 255)],
            [
                ("ix_four", ["c1", "c2", "c3", "c4"], False),  # 2040 bytes: skipped
                ("ix_three", ["c1", "c2", "c3"], False),  # 1530 bytes: kept
            ],
        ),
        "EDGE": idx_info(
            "EDGE",
            [ID, v("a", 250), v("b", 250), v("c", 250), v("d", 100), v("e", 1)],
            [
                ("ix_exact", ["a", "b", "c", "d"], False),  # exactly 1700 bytes: kept
                ("ix_over", ["a", "b", "c", "d", "e"], False),  # 1702 bytes: skipped
            ],
        ),
    }
    rc, out, err = ddl_of(capsys, tmp_path, MSSQL, tables, monkeypatch)
    assert rc == 0, err
    ddl = out
    for skipped in ("ix_memo", "ix_mix", "ix_four", "ix_over"):
        assert not re.search(rf"CREATE (UNIQUE )?INDEX \S*{skipped}\b", ddl), skipped
    for kept in ("ix_a", "ix_three", "ix_exact"):
        assert re.search(rf"CREATE (UNIQUE )?INDEX \S*{kept}\b", ddl), kept
    # warnings reach the report; status is unaffected
    rep = _plan_report(monkeypatch, MSSQL, tables)
    res = by_name(rep)
    assert {t.status for t in rep.tables} == {"planned"}
    assert rep.ok
    assert _warn_names(res["LOB"].warnings) == {"ix_memo", "ix_mix"}
    assert _warn_names(res["WIDE"].warnings) == {"ix_four"}
    assert _warn_names(res["EDGE"].warnings) == {"ix_over"}
    assert "skip" in out.lower()  # the plan prints the warnings


def _plan_report(monkeypatch, url, tables, **kw):
    rd = FakeReader({n: (i, []) for n, i in tables.items()})
    monkeypatch.setattr(converter, "open_reader", lambda *a, **k: rd)
    return converter.convert("fake.mdb", url, dry_run=True, **kw)


def _warn_names(warnings):
    names = set()
    for w in warnings:
        for cand in ("ix_memo", "ix_mix", "ix_a", "ix_four", "ix_three", "ix_exact", "ix_over"):
            if re.search(rf"{cand}\b", w):
                names.add(cand)
    return names


@pytest.mark.parametrize("url", [PG, "sqlite:///x.sqlite"], ids=["pg", "sqlite"])
def test_other_dialects_do_not_skip_indexes(monkeypatch, no_target_connection, url):
    tables = {
        "LOB": idx_info(
            "LOB",
            [ID, ("memo", "LONGCHAR"), v("c1", 255), v("c2", 255), v("c3", 255), v("c4", 255)],
            [("ix_memo", ["memo"], False), ("ix_four", ["c1", "c2", "c3", "c4"], False)],
        )
    }
    rep = _plan_report(monkeypatch, url, tables)
    assert rep.tables[0].warnings == []
    assert rep.tables[0].status == "planned"


def test_mssql_pk_on_nvarchar_max_is_error(monkeypatch, no_target_connection):
    tables = {"P": idx_info("P", [("memo", "LONGCHAR", None, False), v("a", 5)], [], pk=("memo",))}
    rep = _plan_report(monkeypatch, MSSQL, tables)
    t = rep.tables[0]
    assert t.status == "error"
    assert "memo" in (t.error or "")
    assert "primary key" in (t.error or "").lower()
    assert rep.ok is False


def test_mssql_pk_limit_900_bytes(monkeypatch, no_target_connection):
    ok = idx_info("OK", [("k", "VARCHAR", 450, False)], [], pk=("k",))  # 900 bytes
    over = idx_info("OVER", [("k", "VARCHAR", 451, False)], [], pk=("k",))  # 902 bytes
    comp = idx_info(
        "COMP", [("a", "VARCHAR", 250, False), ("b", "VARCHAR", 250, False)], [], pk=("a", "b")
    )  # 1000 bytes
    res = by_name(_plan_report(monkeypatch, MSSQL, {"OK": ok, "OVER": over, "COMP": comp}))
    assert res["OK"].status == "planned"
    assert res["OVER"].status == "error" and res["COMP"].status == "error"
    # the same keys are fine on PostgreSQL
    res = by_name(_plan_report(monkeypatch, PG, {"OVER": over, "COMP": comp}))
    assert {t.status for t in res.values()} == {"planned"}


# ---- dialect hardening: PostgreSQL identifiers and NUL ------------------------------------------

LONG_MULTIBYTE = "µ" * 32  # 64 UTF-8 bytes
OK_MULTIBYTE = "µ" * 31  # 62 bytes
BOUNDARY_63 = "µ" * 31 + "a"  # 63 bytes


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (LONG_MULTIBYTE, "label"),
        ("T" * 64, "label"),
        ("OK", LONG_MULTIBYTE),
        ("OK", "c" * 64),
    ],
    ids=["table_mb64", "table_ascii64", "col_mb64", "col_ascii64"],
)
def test_pg_identifier_over_63_bytes_is_error_naming_it(
    capsys, tmp_path, monkeypatch, no_target_connection, table, column
):
    info = make_info(table, [ID, (column, "VARCHAR", 10)])
    rc, out, err = ddl_of(capsys, tmp_path, PG, {table: info}, monkeypatch)
    assert rc == 1, (out, err)
    offender = table if table != "OK" else column
    assert offender in err
    assert "63" in err
    assert "CREATE TABLE" not in out
    assert PASSWORD not in err


@pytest.mark.parametrize(
    "name", [BOUNDARY_63, OK_MULTIBYTE, "T" * 63], ids=["mb63", "mb62", "ascii63"]
)
def test_pg_identifier_up_to_63_bytes_ok(capsys, tmp_path, monkeypatch, no_target_connection, name):
    info = make_info(name, [ID, (name, "VARCHAR", 10)], pk=("id",))
    rc, out, err = ddl_of(capsys, tmp_path, PG, {name: info}, monkeypatch)
    assert rc == 0, err
    assert "CREATE TABLE" in out


def test_long_identifiers_fine_on_sqlite_and_mssql(
    capsys, tmp_path, monkeypatch, no_target_connection
):
    info = make_info(LONG_MULTIBYTE, [ID, ("c" * 64, "VARCHAR", 10)])
    for url in (MSSQL, "sqlite:///x.sqlite"):
        rc, _out, err = ddl_of(capsys, tmp_path, url, {LONG_MULTIBYTE: info}, monkeypatch)
        assert rc == 0, err


def test_pg_long_identifier_preflight_before_any_target_change(
    tmp_path, install_reader, monkeypatch
):
    """Dialect-name disguise: a SQLite writer that reports 'postgresql' must refuse up front."""
    from tests.conftest import patch_writer

    from acade2sql.writers import SqlAlchemyWriter

    db = tmp_path / "pre.sqlite"
    url = "sqlite:///" + db.as_posix()
    install_reader(FakeReader({"GOOD": (std_info("GOOD"), std_rows(range(1, 4)))}))
    assert converter.convert("fake.mdb", url).ok
    with SqlAlchemyWriter(url) as w:
        before = [r for b in w.read_rows("GOOD") for r in b]
    patch_writer(monkeypatch, False, dialect="postgresql")
    install_reader(
        FakeReader(
            {
                "GOOD": (std_info("GOOD"), std_rows(range(10, 12))),  # sorts before the bad one
                LONG_MULTIBYTE: (std_info(LONG_MULTIBYTE), std_rows([1])),
            }
        )
    )
    with pytest.raises(Acade2SqlError) as ei:
        converter.convert("fake.mdb", url)
    assert LONG_MULTIBYTE in str(ei.value)
    with SqlAlchemyWriter(url) as w:
        assert [r for b in w.read_rows("GOOD") for r in b] == before  # GOOD was not dropped


def test_pg_nul_in_text_is_table_error(tmp_path, install_reader, monkeypatch):
    """Simulated: a SQLite writer disguised as 'postgresql' (see patch_writer) must reject NUL
    before/while writing and report the spec message; other tables are unaffected."""
    from tests.conftest import patch_writer

    patch_writer(monkeypatch, False, dialect="postgresql")
    rows = [(1, "ok", "c1", None), (2, "bad\x00label", "c2", None)]
    install_reader(
        FakeReader({"BAD": (std_info("BAD"), rows), "GOOD": (std_info("GOOD"), std_rows([1, 2]))})
    )
    rep = converter.convert("fake.mdb", "sqlite:///" + (tmp_path / "n.sqlite").as_posix())
    res = by_name(rep)
    assert res["BAD"].status == "error"
    assert res["BAD"].error == (
        "write failed: NUL character in text column label (not supported by PostgreSQL)"
    )
    assert res["GOOD"].status == "pass"
    assert rep.ok is False


# ---- mssql engine --------------------------------------------------------------------------------


def test_mssql_engine_uses_fast_executemany(monkeypatch):
    import acade2sql.writers.sqlalchemy_writer as w

    seen = {}
    real = w.create_engine

    def capture(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(w, "create_engine", capture)
    with w.SqlAlchemyWriter(MSSQL) as writer:
        assert writer.dialect == "mssql"
        dialect_flag = getattr(writer.engine.dialect, "fast_executemany", None)
    assert seen.get("fast_executemany") is True or dialect_flag is True


def test_sqlite_engine_not_given_fast_executemany(monkeypatch, tmp_path):
    import acade2sql.writers.sqlalchemy_writer as w

    seen = {}
    real = w.create_engine

    def capture(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(w, "create_engine", capture)
    with w.SqlAlchemyWriter("sqlite:///" + (tmp_path / "f.sqlite").as_posix()):
        pass
    assert "fast_executemany" not in seen


def test_missing_postgres_driver_gives_actionable_error(monkeypatch, tmp_path, install_reader):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    install_reader(make_reader())
    from acade2sql.errors import TargetError

    with pytest.raises(TargetError) as ei:
        converter.convert("fake.mdb", PG)
    assert "acade2sql[postgresql]" in str(ei.value)
    assert PASSWORD not in str(ei.value)
