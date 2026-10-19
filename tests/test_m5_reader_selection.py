"""Reader selection (docs/M5_SPEC.md section 2): open_reader(reader=...), convert(reader=...),
``--reader`` on inspect/convert, and ``"reader"`` in the report JSON and ``inspect --json``."""

from __future__ import annotations

import json
import logging

import pyodbc
import pytest
from tests.fixtures.build_access import MANIFEST

from acade2sql import cli, converter
from acade2sql.errors import DriverNotFoundError
from acade2sql.readers import open_reader
from acade2sql.readers.access_parser_reader import AccessParserReader
from acade2sql.readers.pyodbc_reader import PyodbcReader

pytestmark = pytest.mark.requires_driver

N = len(MANIFEST)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture
def no_pyodbc(monkeypatch):
    """Any use of pyodbc (connect or driver enumeration) fails the test."""

    def boom(*_a, **_k):
        raise AssertionError("the pure reader touched pyodbc")

    monkeypatch.setattr(pyodbc, "connect", boom)
    monkeypatch.setattr(pyodbc, "drivers", boom)


def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ------------------------------------------------------------------------------ open_reader


def test_default_is_auto_and_picks_odbc(access_db, caplog):
    caplog.set_level(logging.DEBUG)
    with open_reader(access_db) as r:
        assert isinstance(r, PyodbcReader)
    assert not _warnings(caplog)


def test_auto_picks_odbc_when_driver_ok(access_db, caplog):
    caplog.set_level(logging.DEBUG)
    with open_reader(access_db, reader="auto") as r:
        assert isinstance(r, PyodbcReader)
    assert not _warnings(caplog)


def test_auto_falls_back_to_pure_with_exactly_one_warning(access_db, no_odbc_driver, caplog):
    caplog.set_level(logging.DEBUG)
    with open_reader(access_db, reader="auto") as r:
        assert isinstance(r, AccessParserReader)
        assert r.count_rows("COMP") == 5
    warns = _warnings(caplog)
    assert len(warns) == 1, [w.getMessage() for w in warns]
    assert warns[0].levelno == logging.WARNING
    text = warns[0].getMessage().lower()
    assert "slow" in text or "speed" in text or "faster" in text  # explains the speed difference
    assert "odbc" in text and "install" in text  # and how to get the driver


def test_auto_fallback_does_not_connect_pyodbc(access_db, no_odbc_driver, no_pyodbc):
    with open_reader(access_db, reader="auto") as r:
        assert isinstance(r, AccessParserReader)
        assert r.list_tables()


def test_pure_never_touches_pyodbc(access_db, no_pyodbc, caplog):
    caplog.set_level(logging.DEBUG)
    with open_reader(access_db, reader="pure") as r:
        assert isinstance(r, AccessParserReader)
        assert r.count_rows("COMP") == 5
        assert sum(len(b) for b in r.iter_rows("TERMS")) == 3
    assert not _warnings(caplog)  # explicit choice: no fallback warning


def test_pure_with_copy_to_temp_never_touches_pyodbc(access_db, no_pyodbc):
    with open_reader(access_db, reader="pure", copy_to_temp=True) as r:
        assert r.count_rows("COMP") == 5


def test_odbc_without_driver_keeps_todays_error(access_db, no_odbc_driver):
    with pytest.raises(DriverNotFoundError):
        open_reader(access_db, reader="odbc")


def test_unknown_reader_value_is_rejected(access_db):
    with pytest.raises(ValueError):
        open_reader(access_db, reader="turbo")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ convert API


@pytest.mark.parametrize(("kw", "expected"), [({}, "odbc"), ({"reader": "pure"}, "pure")])
def test_convert_records_reader(access_db, tmp_path, kw, expected):
    rp = tmp_path / "r.json"
    rep = converter.convert(
        access_db, str(tmp_path / "o.sqlite"), tables=["COMP", "TERMS"], report_path=rp, **kw
    )
    assert rep.ok
    assert rep.to_dict()["reader"] == expected
    assert json.loads(rp.read_text("utf-8"))["reader"] == expected


def test_convert_auto_fallback_records_pure(access_db, tmp_path, no_odbc_driver, no_pyodbc):
    rp = tmp_path / "r.json"
    rep = converter.convert(access_db, str(tmp_path / "o.sqlite"), report_path=rp, reader="auto")
    assert rep.ok, rep.render_text()
    assert json.loads(rp.read_text("utf-8"))["reader"] == "pure"
    assert len(rep.tables) == N


def test_convert_pure_checksum_passes_for_all_tables(access_db, tmp_path):
    rep = converter.convert(access_db, str(tmp_path / "o.sqlite"), reader="pure", checksum=True)
    assert rep.ok, rep.render_text()
    assert {t.name for t in rep.tables} == set(MANIFEST)
    for t in rep.tables:
        assert t.status == "pass" and t.checksum_match is True, t
        assert t.source_rows == t.target_rows == MANIFEST[t.name]["count"], t.name


def test_convert_dry_run_records_reader(access_db, tmp_path):
    rep = converter.convert(access_db, str(tmp_path / "x.sqlite"), dry_run=True, reader="pure")
    assert rep.to_dict()["reader"] == "pure"
    assert not (tmp_path / "x.sqlite").exists()


def test_convert_pure_equals_odbc_in_target(access_db, tmp_path):
    import sqlite3

    outs = {}
    for kind in ("odbc", "pure"):
        out = tmp_path / f"{kind}.sqlite"
        assert converter.convert(access_db, str(out), reader=kind).ok
        outs[kind] = out
    dumps = {}
    for kind, out in outs.items():
        con = sqlite3.connect(out)
        try:
            names = sorted(
                r[0] for r in con.execute("select name from sqlite_master where type='table'")
            )
            dumps[kind] = {
                n: sorted(
                    con.execute(f'select * from "{n}"').fetchall(),
                    key=lambda r: [repr(v) for v in r],
                )
                for n in names
            }
        finally:
            con.close()
    assert dumps["pure"] == dumps["odbc"]


# ------------------------------------------------------------------------------ CLI


def test_cli_inspect_json_records_reader_default_odbc(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--json")
    assert rc == 0, err
    assert json.loads(out)["reader"] == "odbc"


@pytest.mark.parametrize("flag", ["odbc", "pure", "auto"])
def test_cli_inspect_reader_flag(capsys, mdb_db, flag):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--reader", flag, "--json")
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["reader"] == ("odbc" if flag == "auto" else flag)
    assert {t["name"] for t in doc["tables"]} == set(MANIFEST)
    assert {t["name"]: t["row_count"] for t in doc["tables"]} == {
        n: m["count"] for n, m in MANIFEST.items()
    }


def test_cli_inspect_pure_equals_odbc(capsys, mdb_db):
    docs = {}
    for kind in ("odbc", "pure"):
        rc, out, err = run(capsys, "inspect", str(mdb_db), "--reader", kind, "--json")
        assert rc == 0, err
        docs[kind] = json.loads(out)
    for t_o, t_p in zip(docs["odbc"]["tables"], docs["pure"]["tables"], strict=True):
        assert t_p["name"] == t_o["name"] and t_p["row_count"] == t_o["row_count"]
        assert [c["name"] for c in t_p["columns"]] == [c["name"] for c in t_o["columns"]]
        assert [c["access_type"] for c in t_p["columns"]] == [
            c["access_type"] for c in t_o["columns"]
        ]


def test_cli_inspect_text_with_pure(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--reader", "pure", "--table", "COMP")
    assert rc == 0, err
    assert "1 tables, 5 rows" in out and "Table COMP" in out and "HDL" in out
    assert "index PrimaryKey (HDL) unique" in out  # indexes are discovered by the pure reader


def test_cli_inspect_auto_without_driver_uses_pure(capsys, mdb_db, no_odbc_driver, no_pyodbc):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--json")
    assert rc == 0, err
    assert json.loads(out)["reader"] == "pure"


def test_cli_inspect_pure_no_pyodbc(capsys, mdb_db, no_pyodbc):
    rc, _out, err = run(capsys, "inspect", str(mdb_db), "--reader", "pure")
    assert rc == 0, err


def test_cli_bad_reader_value_is_usage_error(capsys, mdb_db):
    for sub in ("inspect", "convert"):
        rc, _out, err = run(capsys, sub, str(mdb_db), "--reader", "bogus")
        assert rc == 2
        assert "bogus" in err or "invalid choice" in err


@pytest.mark.parametrize("flag", ["pure", "odbc"])
def test_cli_convert_reader_flag_and_report(capsys, mdb_db, tmp_path, flag):
    rp = tmp_path / "rep.json"
    rc, out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "o.sqlite"),
        "--reader", flag, "--checksum", "--report", str(rp),
    )  # fmt: skip
    assert rc == 0, err
    assert f"{N} tables: {N} passed, 0 failed, 0 errors" in out
    assert out.count("checksum=match") == N
    assert json.loads(rp.read_text("utf-8"))["reader"] == flag


def test_cli_convert_default_reader_is_auto(capsys, mdb_db, tmp_path):
    rp = tmp_path / "rep.json"
    rc, _out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "o.sqlite"),
        "--tables", "COMP", "--report", str(rp),
    )  # fmt: skip
    assert rc == 0, err
    assert json.loads(rp.read_text("utf-8"))["reader"] == "odbc"


def test_cli_convert_auto_without_driver(capsys, mdb_db, tmp_path, no_odbc_driver, no_pyodbc):
    rp = tmp_path / "rep.json"
    rc, _out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "o.sqlite"),
        "--report", str(rp),
    )  # fmt: skip
    assert rc == 0, err
    assert json.loads(rp.read_text("utf-8"))["reader"] == "pure"


def test_cli_convert_dry_run_pure(capsys, mdb_db, tmp_path):
    rp = tmp_path / "rep.json"
    rc, _out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "never.sqlite"),
        "--dry-run", "--reader", "pure", "--report", str(rp),
    )  # fmt: skip
    assert rc == 0, err
    assert not (tmp_path / "never.sqlite").exists()
    assert json.loads(rp.read_text("utf-8"))["reader"] == "pure"
