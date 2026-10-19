"""Review-gate-1 regression tests (M3). Driver-free: a fake Reader feeds a real SQLite target."""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import sqlite3
import sys
import tempfile
from io import StringIO
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote_plus

import pytest
from sqlalchemy import MetaData
from sqlalchemy import types as sat

import acade2sql
from acade2sql import cli, converter
from acade2sql.errors import WriteError
from acade2sql.logging_setup import configure_logging
from acade2sql.readers.base import Reader
from acade2sql.redact import mask_url, redact
from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo
from acade2sql.typemap import is_known, sa_type
from acade2sql.validate import table_checksum
from acade2sql.writers import SqlAlchemyWriter
from acade2sql.writers.sqlalchemy_writer import build_table

# ---- helpers -------------------------------------------------------------------------------


def col(name, ordinal, access_type, *, nullable=True, size=None, autoinc=False):
    return ColumnInfo(name, ordinal, access_type, 0, size, None, nullable, autoinc)


def make_info(name, *, extra_cols=(), indexes=(), pk_source="discovered"):
    cols = (
        col("id", 1, "COUNTER", nullable=False, autoinc=True),
        col("label", 2, "VARCHAR", size=50),
        *extra_cols,
    )
    return TableInfo(name, cols, ("id",), pk_source, tuple(indexes))


class FakeReader(Reader):
    """In-memory Reader. ``count_fail``/``read_fail`` name tables whose count/iteration raises."""

    def __init__(self, tables, *, count_fail=(), read_fail=()):
        self.path = Path("fake.mdb")
        self.tables = dict(tables)  # name -> (TableInfo, rows)
        self.count_fail = set(count_fail)
        self.read_fail = set(read_fail)

    def close(self):
        pass

    def list_tables(self, include_system=False):
        return sorted(self.tables, key=str.casefold)

    def get_table(self, name):
        return self.tables[name][0]

    def count_rows(self, name):
        if name in self.count_fail:
            raise RuntimeError("count boom")
        return len(self.tables[name][1])

    def iter_rows(self, name, batch_size=5000):
        rows = self.tables[name][1]
        for i in range(0, len(rows), batch_size):
            if name in self.read_fail and i > 0:
                raise RuntimeError("source went away")
            yield [tuple(r) for r in rows[i : i + batch_size]]


def simple_rows(n=5):
    return [(i, f"row{i}") for i in range(1, n + 1)]


def abc_reader(**kw):
    return FakeReader({n: (make_info(n), simple_rows()) for n in ("A_TBL", "B_TBL", "C_TBL")}, **kw)


@pytest.fixture
def use_reader(monkeypatch):
    def install(reader):
        monkeypatch.setattr(converter, "open_reader", lambda *a, **k: reader)
        return reader

    return install


def run_cli(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def url_of(p: Path) -> str:
    return "sqlite:///" + p.as_posix()


def sqlite_rows(p: Path, sql: str):
    con = sqlite3.connect(p)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def by_name(report):
    return {t.name: t for t in report.tables}


# ---- 1. module rename ----------------------------------------------------------------------


def test_convert_is_a_plain_function_and_old_module_is_gone():
    assert inspect.isfunction(acade2sql.convert)
    assert inspect.isfunction(acade2sql.resolve_target)
    params = inspect.signature(acade2sql.convert).parameters
    assert list(params) == [
        "source", "target", "mode", "tables", "exclude", "batch_size", "checksum",
        "copy_to_temp", "report_path", "key", "no_key", "delete_missing", "schema", "dry_run",
        "counts",
    ]  # fmt: skip
    assert params["mode"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["report_path"].kind is inspect.Parameter.KEYWORD_ONLY
    assert importlib.util.find_spec("acade2sql.converter") is not None
    assert importlib.util.find_spec("acade2sql.convert") is None


# ---- 2. secrets ----------------------------------------------------------------------------


def test_mask_url_userinfo_password():
    out = mask_url("postgresql://alice:hunter2@db.example.com:5432/acade")
    assert "hunter2" not in out
    assert "alice" in out and "db.example.com" in out and "***" in out


@pytest.mark.parametrize("key", ["password", "PWD", "client_secret", "api_token", "apikey", "Key"])
def test_mask_url_sensitive_query_params(key):
    out = mask_url(f"postgresql://u@h/db?{key}=topsecretvalue&sslmode=require")
    assert "topsecretvalue" not in out
    assert "***" in out
    assert "sslmode=require" in out


DRV = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=srv1;UID=bob;"


@pytest.mark.parametrize(
    ("conn", "secret"),
    [
        (DRV + "PWD=supersecret", "supersecret"),
        (DRV + "pwd=supersecret;", "supersecret"),
        (DRV + "PWD={se;cret}", "se;cret"),
        (DRV + "Password=supersecret", "supersecret"),
        ("SERVER=srv1;UID=bob;PASSWORD=supersecret;Database=d", "supersecret"),
    ],
)
def test_mask_url_odbc_connect(conn, secret):
    url = "mssql+pyodbc:///?odbc_connect=" + quote_plus(conn)
    plain = unquote_plus(mask_url(url))
    assert secret not in plain
    assert "cret}" not in plain  # braced value must be masked as a whole
    assert "***" in plain
    for kept in ("SERVER=srv1", "UID=bob", "DRIVER="):
        if kept in conn:
            assert kept in plain, plain


def test_mask_url_sqlite_shows_plain_path(tmp_path):
    p = tmp_path / "a b.sqlite"
    out = mask_url(url_of(p))
    assert out.startswith("sqlite:///")
    assert p.as_posix() in out
    assert "%20" not in out


def test_redact_removes_secrets_from_exception_text():
    url = "postgresql://alice:hunter2@db.example.com/acade"
    msg = f"(psycopg2.OperationalError) could not connect using {url}: refused"
    out = redact(msg, url)
    assert "hunter2" not in out
    assert "db.example.com" in out

    odbc = "(pyodbc.Error) ('08001', 'DRIVER={X};SERVER=s;UID=u;PWD=supersecret;Database=d')"
    assert "supersecret" not in redact(odbc)
    assert "SERVER=s" in redact(odbc)
    assert "se;cret" not in redact("conn UID=u;PWD={se;cret};X=1")
    assert "supersecret" not in redact("Password=supersecret and pwd=supersecret")


def test_redact_url_encoded_odbc_connect_in_text():
    assert "supersecret" not in redact("odbc_connect=" + quote("UID=u;PWD=supersecret"))


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_odbc_target_secret_never_leaks(capsys, use_reader, tmp_path, verbose):
    use_reader(abc_reader())
    conn = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=nohost.invalid;UID=bob;PWD=supersecret"
    target = "mssql+pyodbc:///?odbc_connect=" + quote(conn, safe="")
    rp = tmp_path / "r.json"
    argv = ["convert", "fake.mdb", "--target", target, "--report", str(rp)]
    if verbose:
        argv.insert(0, "--verbose")
    rc, out, err = run_cli(capsys, *argv)
    assert rc != 0
    assert "supersecret" not in out
    assert "supersecret" not in err
    if rp.exists():
        assert "supersecret" not in rp.read_text(encoding="utf-8")


# ---- 3. per-table DDL ----------------------------------------------------------------------


@pytest.fixture
def fail_ddl_for_b(monkeypatch):
    orig = SqlAlchemyWriter.create_table

    def fake(self, info):
        if info.name == "B_TBL":
            raise WriteError("B_TBL", RuntimeError("ddl boom"))
        return orig(self, info)

    monkeypatch.setattr(SqlAlchemyWriter, "create_table", fake)


def test_prepare_does_no_ddl(tmp_path):
    db = tmp_path / "p.sqlite"
    with SqlAlchemyWriter(url_of(db)) as w:
        w.prepare([make_info("T1")])
    assert not db.exists() or sqlite_rows(db, "SELECT name FROM sqlite_master") == []


def test_create_table_drops_and_recreates_one_table(tmp_path):
    db = tmp_path / "ct.sqlite"
    info = make_info("T1", indexes=[IndexInfo("ix_label", ("label",), False)])
    with SqlAlchemyWriter(url_of(db)) as w:
        w.prepare([info])
        w.create_table(info)
        w.write_batches(info, [simple_rows(3)])
        assert w.count_rows("T1") == 3
        w.create_table(info)  # again: drop + create -> empty
        assert w.count_rows("T1") == 0
    idx = sqlite_rows(db, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='T1'")
    assert any("ix_T1" in r[0] for r in idx)


def test_ddl_failure_isolated_report_written_and_later_tables_converted(
    use_reader, fail_ddl_for_b, tmp_path
):
    use_reader(abc_reader())
    db = tmp_path / "o.sqlite"
    rp = tmp_path / "sub" / "rep.json"
    rep = converter.convert("fake.mdb", url_of(db), report_path=rp)
    res = by_name(rep)
    assert res["B_TBL"].status == "error"
    assert "ddl boom" in (res["B_TBL"].error or "")
    assert rep.ok is False
    for n in ("A_TBL", "C_TBL"):
        assert res[n].status == "pass", n
        assert res[n].source_rows == res[n].target_rows == 5
        assert sqlite_rows(db, f'SELECT COUNT(*) FROM "{n}"') == [(5,)]
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert {t["name"]: t["status"] for t in doc["tables"]} == {
        "A_TBL": "pass", "B_TBL": "error", "C_TBL": "pass",
    }  # fmt: skip


def test_ddl_failure_cli_exit_3(capsys, use_reader, fail_ddl_for_b, tmp_path):
    use_reader(abc_reader())
    rp = tmp_path / "rep.json"
    rc, out, _err = run_cli(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"), "--report", str(rp)
    )
    assert rc == 3
    assert any(ln.startswith("ERROR") and "B_TBL" in ln for ln in out.splitlines())
    assert rp.exists()


# ---- 4. types ------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["sqlite", "mssql", "postgresql"])
def test_bigint_maps_to_biginteger(dialect):
    assert isinstance(sa_type(col("n", 1, "BIGINT"), dialect), sat.BigInteger)


@pytest.mark.parametrize("dialect", ["sqlite", "mssql", "postgresql"])
@pytest.mark.parametrize("name", ["TINYINT", "INT", "tinyint", "int"])
def test_tinyint_and_int_aliases(name, dialect):
    assert is_known(name)
    assert isinstance(sa_type(col("n", 1, name), dialect), sat.Integer)


def test_bigint_roundtrips_as_int_on_sqlite(use_reader, tmp_path):
    info = make_info("BIG", extra_cols=(col("n", 3, "BIGINT"),))
    rows = [(1, "a", 2**40), (2, "b", -(2**62)), (3, "c", None)]
    use_reader(FakeReader({"BIG": (info, rows)}))
    db = tmp_path / "big.sqlite"
    rep = converter.convert("fake.mdb", url_of(db), checksum=True)
    assert rep.ok, rep.render_text()
    assert sqlite_rows(db, 'SELECT n, typeof(n) FROM "BIG" ORDER BY id') == [
        (2**40, "integer"), (-(2**62), "integer"), (None, "null"),
    ]  # fmt: skip
    with SqlAlchemyWriter(url_of(db)) as w:
        got = [r for b in w.read_rows("BIG") for r in b]
    assert [r[2] for r in got] == [2**40, -(2**62), None]
    assert all(isinstance(r[2], int) for r in got[:2])


@pytest.fixture
def unknown_type_reader(use_reader):
    info = make_info("ODD", extra_cols=(col("weird", 3, "FROBNICATE"),))
    return use_reader(
        FakeReader({"ODD": (info, [(1, "a", "x")]), "OK": (make_info("OK"), simple_rows(2))})
    )


def test_unknown_type_fails_table_with_warning(unknown_type_reader, tmp_path):
    rep = converter.convert("fake.mdb", url_of(tmp_path / "u.sqlite"))
    res = by_name(rep)
    assert res["ODD"].status == "fail"
    assert any("unmapped type" in w.lower() for w in res["ODD"].warnings), res["ODD"].warnings
    assert "FROBNICATE" in " ".join(res["ODD"].warnings)
    assert res["OK"].status == "pass"
    assert rep.ok is False
    lines = rep.render_text().splitlines()
    odd = next(ln for ln in lines if "ODD" in ln)
    assert odd.startswith("FAIL")
    assert "warnings=1" in odd
    ok = next(ln for ln in lines if ln.startswith("PASS"))
    assert "warnings" not in ok


def test_unknown_type_cli_exit_3(capsys, unknown_type_reader, tmp_path):
    rc, out, _err = run_cli(capsys, "convert", "fake.mdb", "--target", str(tmp_path / "u.sqlite"))
    assert rc == 3
    assert "warnings=1" in out


# ---- 5. mismatch detection -----------------------------------------------------------------


def _wrap_write(monkeypatch, mutate, only="T1"):
    orig = SqlAlchemyWriter.write_batches

    def fake(self, info, batches):
        if info.name != only:
            return orig(self, info, batches)
        return orig(self, info, mutate(list(batches)))

    monkeypatch.setattr(SqlAlchemyWriter, "write_batches", fake)


def _t1_reader(use_reader):
    rows = [(1, "alpha"), (2, ""), (3, "gamma"), (4, "delta")]
    return use_reader(FakeReader({"T1": (make_info("T1"), rows)}))


def _drop_one_row(batches):
    batches[0] = batches[0][:-1]
    return batches


def _empty_to_null(batches):
    return [[(r[0], None if r[1] == "" else r[1]) for r in b] for b in batches]


def _append_space(batches):
    return [[(r[0], r[1] + " " if r[0] == 3 else r[1]) for r in b] for b in batches]


def test_dropped_row_is_fail(monkeypatch, use_reader, tmp_path):
    _t1_reader(use_reader)
    _wrap_write(monkeypatch, _drop_one_row)
    rep = converter.convert("fake.mdb", url_of(tmp_path / "d.sqlite"))
    t = by_name(rep)["T1"]
    assert (t.source_rows, t.target_rows, t.status) == (4, 3, "fail")
    assert rep.ok is False
    assert rep.render_text().splitlines()[0].startswith("FAIL")


def test_dropped_row_cli_exit_3(capsys, monkeypatch, use_reader, tmp_path):
    _t1_reader(use_reader)
    _wrap_write(monkeypatch, _drop_one_row)
    rc, out, _ = run_cli(capsys, "convert", "fake.mdb", "--target", str(tmp_path / "d.sqlite"))
    assert rc == 3
    assert out.splitlines()[0].startswith("FAIL")


@pytest.mark.parametrize("mutate", [_empty_to_null, _append_space], ids=["empty_to_null", "space"])
def test_altered_value_with_checksum_is_fail(monkeypatch, use_reader, tmp_path, mutate):
    _t1_reader(use_reader)
    _wrap_write(monkeypatch, mutate)
    rep = converter.convert("fake.mdb", url_of(tmp_path / "c.sqlite"), checksum=True)
    t = by_name(rep)["T1"]
    assert t.source_rows == t.target_rows == 4
    assert t.checksum_match is False
    assert t.status == "fail"
    assert rep.ok is False
    line = rep.render_text().splitlines()[0]
    assert line.startswith("FAIL") and "checksum=MISMATCH" in line


@pytest.mark.parametrize("mutate", [_empty_to_null, _append_space], ids=["empty_to_null", "space"])
def test_altered_value_without_checksum_passes_counts_only_cannot_see_it(
    monkeypatch, use_reader, tmp_path, mutate
):
    _t1_reader(use_reader)
    _wrap_write(monkeypatch, mutate)
    rep = converter.convert("fake.mdb", url_of(tmp_path / "n.sqlite"), checksum=False)
    t = by_name(rep)["T1"]
    assert t.checksum_match is None
    assert t.status == "pass"


def test_altered_value_cli_exit_3_with_checksum(capsys, monkeypatch, use_reader, tmp_path):
    _t1_reader(use_reader)
    _wrap_write(monkeypatch, _append_space)
    rc, out, _ = run_cli(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "c.sqlite"), "--checksum"
    )
    assert rc == 3
    assert "checksum=MISMATCH" in out


# ---- 6. percent-encoded path ---------------------------------------------------------------


def test_percent_in_sqlite_path_is_literal(use_reader, tmp_path):
    use_reader(FakeReader({"T1": (make_info("T1"), simple_rows(3))}))
    target = tmp_path / "a%20b.sqlite"
    url = converter.resolve_target(str(target), Path("x.mdb"))
    rep = converter.convert("fake.mdb", url)
    assert rep.ok
    names = {p.name for p in tmp_path.iterdir()}
    assert "a%20b.sqlite" in names
    assert "a b.sqlite" not in names


# ---- 7. bad target URLs --------------------------------------------------------------------


@pytest.mark.parametrize("target", ["postgresql://u:secret@h:abc/db", "notadialect://x"])
def test_bad_target_url_clean_error(capsys, use_reader, target):
    use_reader(FakeReader({"T1": (make_info("T1"), simple_rows(3))}))
    rc, out, err = run_cli(capsys, "convert", "fake.mdb", "--target", target)
    assert rc == 1
    assert any(ln.startswith("error:") for ln in err.splitlines()), err
    assert "Traceback" not in err + out
    assert "secret" not in err + out


# ---- 8. logging ----------------------------------------------------------------------------


@pytest.fixture
def clean_logger():
    lg = logging.getLogger("acade2sql")
    handlers, level = list(lg.handlers), lg.level
    yield lg
    lg.handlers[:] = handlers
    lg.setLevel(level)


def test_closed_stderr_does_not_cause_logging_error(clean_logger, monkeypatch, capfd):
    real = sys.stderr
    buf = StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    configure_logging(False)
    buf.close()
    monkeypatch.setattr(sys, "stderr", real)
    logging.getLogger("acade2sql.something").warning("hello after close")
    err = capfd.readouterr().err
    assert "--- Logging error ---" not in err
    assert "ValueError" not in err


def test_configure_logging_twice_keeps_handler_count(clean_logger):
    configure_logging(False)
    n = len(clean_logger.handlers)
    configure_logging(True)
    configure_logging(False)
    assert len(clean_logger.handlers) == n


# ---- 9. temp dirs / pooling ----------------------------------------------------------------


@pytest.mark.requires_driver
def test_copy_to_temp_leaves_no_temp_dir(mdb_db):
    from acade2sql.readers import open_reader

    tmp = Path(tempfile.gettempdir())
    before = {p.name for p in tmp.glob("acade2sql*")}
    with open_reader(mdb_db, copy_to_temp=True) as r:
        assert r.list_tables()
    after = {p.name for p in tmp.glob("acade2sql*")}
    assert after - before == set()


def test_pyodbc_pooling_disabled():
    import pyodbc

    import acade2sql.readers.pyodbc_reader  # noqa: F401

    assert pyodbc.pooling is False


# ---- 10. checksum GUID normalisation -------------------------------------------------------

G = "6F9619FF-8B86-D011-B42D-00C04FC964FF"


def ck(*values):
    """Checksum of one GUID column (GUID folding is column-aware since review gate 2)."""
    return table_checksum([[(v,) for v in values]], column_types=["GUID"])


def test_checksum_unbalanced_guid_braces_differ():
    assert ck("{" + G) != ck(G + "}")
    assert ck("{" + G) != ck(G)
    assert ck(G + "}") != ck(G)


def test_checksum_balanced_guid_braces_and_case_equal():
    assert ck("{" + G + "}") == ck(G)
    assert ck(G.lower()) == ck("{" + G + "}")


def test_checksum_non_guid_strings_are_exact():
    assert ck("Abc") != ck("abc")
    assert ck("{x}") != ck("x")


# ---- 11. count_rows failure ----------------------------------------------------------------


def test_count_rows_failure_gives_none_source_rows(use_reader, tmp_path):
    use_reader(abc_reader(count_fail={"B_TBL"}))
    rp = tmp_path / "r.json"
    rep = converter.convert("fake.mdb", url_of(tmp_path / "o.sqlite"), report_path=rp)
    t = by_name(rep)["B_TBL"]
    assert t.source_rows is None
    assert t.status != "pass"
    line = next(ln for ln in rep.render_text().splitlines() if "B_TBL" in ln)
    assert "source=-" in line
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert doc["summary"]["tables"] == 3
    assert by_name(rep)["A_TBL"].status == "pass"


# ---- 12. PrimaryKey-named non-unique index -------------------------------------------------


def test_nonunique_index_named_primarykey_kept_when_pk_inferred():
    idx = IndexInfo("PrimaryKey", ("label",), False)
    info = make_info("T1", indexes=[idx], pk_source="inferred")
    table = build_table(MetaData(), info, "sqlite", used_index_names=set())
    assert [tuple(c.name for c in i.columns) for i in table.indexes] == [("label",)]


def test_nonunique_primarykey_index_reaches_target(use_reader, tmp_path):
    idx = IndexInfo("PrimaryKey", ("label",), False)
    info = make_info("T1", indexes=[idx], pk_source="inferred")
    use_reader(FakeReader({"T1": (info, simple_rows(3))}))
    db = tmp_path / "pk.sqlite"
    assert converter.convert("fake.mdb", url_of(db)).ok
    names = [r[1] for r in sqlite_rows(db, "PRAGMA index_list('T1')")]
    cols = [
        sqlite_rows(db, f"PRAGMA index_info('{n}')")[0][2]
        for n in names
        if not n.startswith("sqlite_autoindex")
    ]
    assert cols == ["label"]


# ---- 13. read vs write failure wording -----------------------------------------------------


def test_read_failure_mid_iteration_says_read_failed(use_reader, tmp_path):
    use_reader(abc_reader(read_fail={"B_TBL"}))
    rep = converter.convert("fake.mdb", url_of(tmp_path / "r.sqlite"), batch_size=2)
    t = by_name(rep)["B_TBL"]
    assert t.status == "error"
    assert "read failed" in (t.error or "").lower(), t.error
    assert "write failed" not in (t.error or "").lower()
    assert by_name(rep)["C_TBL"].status == "pass"


def test_write_failure_says_write_failed(monkeypatch, use_reader, tmp_path):
    use_reader(abc_reader())
    orig = SqlAlchemyWriter.write_batches

    def fake(self, info, batches):
        if info.name == "B_TBL":
            raise WriteError("B_TBL", RuntimeError("disk full"))
        return orig(self, info, batches)

    monkeypatch.setattr(SqlAlchemyWriter, "write_batches", fake)
    rep = converter.convert("fake.mdb", url_of(tmp_path / "w.sqlite"))
    t = by_name(rep)["B_TBL"]
    assert t.status == "error"
    assert "write failed" in (t.error or "").lower(), t.error
    assert "disk full" in (t.error or "")
    assert "read failed" not in (t.error or "").lower()
