"""Regression tests for the ODBC reader's Large Number (BIGINT) guard (DECISIONS #45).

The Access ODBC driver reports a BIGINT column as CHAR(4) and returns garbage 4-character strings;
the ODBC reader must refuse such a table (cross-checked against the file's TDEF) instead of
silently converting the garbage.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from tests.fixtures import final_review
from tests.fixtures.build_access import connect_rw, create_empty_db
from tests.fixtures.final_review import BIG

import acade2sql
from acade2sql import cli
from acade2sql.converter import convert
from acade2sql.errors import UnsupportedAccessFeature
from acade2sql.readers import access_parser_reader, open_reader

pytestmark = pytest.mark.requires_driver

C4_VALUES = ["abcd", "ab", "", None]


@pytest.fixture(scope="module")
def bigint_db(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("guard_bigint")
    try:
        return final_review.build_bigint(d / "big.accdb")
    except Exception as exc:  # the driver of this machine cannot create BIGINT columns
        pytest.skip(f"cannot create a BIGINT column here: {exc}")


@pytest.fixture(scope="module", params=["mdb", "accdb"])
def char4_db(request, tmp_path_factory) -> Path:
    """A genuine CHAR(4) text column (what the guard's pre-filter matches) and a plain table."""
    path = tmp_path_factory.mktemp(f"guard_c4_{request.param}") / f"c4.{request.param}"
    create_empty_db(path)
    con = connect_rw(path)
    cur = con.cursor()
    try:
        cur.execute("CREATE TABLE C4 (ID INTEGER, C CHAR(4))")
        for i, v in enumerate(C4_VALUES, 1):
            cur.execute("INSERT INTO C4 (ID, C) VALUES (?, ?)", i, v)
        cur.execute("CREATE TABLE PLAIN (ID INTEGER, S TEXT(10))")
        cur.execute("INSERT INTO PLAIN VALUES (1, 'x')")
    finally:
        cur.close()
        con.close()
    return path


def sqlite_tables(path: Path) -> list[str]:
    con = sqlite3.connect(str(path))
    try:
        return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()


def read_all(path: Path, table: str, reader: str, **kw) -> list[tuple]:
    with open_reader(path, reader=reader, **kw) as r:
        return sorted((tuple(x) for b in r.iter_rows(table) for x in b), key=lambda t: t[0])


def count_cross_checks(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = access_parser_reader.column_type_codes

    def spy(source, table):
        calls.append(table)
        return real(source, table)

    monkeypatch.setattr(access_parser_reader, "column_type_codes", spy)
    return calls


# ------------------------------------------------------------------------------- BIGINT refused


def test_odbc_get_table_bigint_raises_with_reader_pure_hint(bigint_db):
    with open_reader(bigint_db, reader="odbc") as r:
        with pytest.raises(UnsupportedAccessFeature) as ei:
            r.get_table("BIG64")
        msg = str(ei.value)
        assert "'BI'" in msg and "'BIG64'" in msg
        assert "Large Number (BIGINT)" in msg
        assert "--reader pure" in msg
        # iter_rows is guarded too: no garbage for library users who skip get_table
        with pytest.raises(UnsupportedAccessFeature):
            r.iter_rows("BIG64")
        assert r.get_table("OKTBL").name == "OKTBL"


def test_odbc_convert_bigint_table_is_error_others_pass(bigint_db, tmp_path):
    target = tmp_path / "o.sqlite"
    report = convert(bigint_db, str(target), reader="odbc", checksum=True)
    by = {t.name: t for t in report.tables}
    assert by["BIG64"].status == "error", by["BIG64"]
    assert "--reader pure" in (by["BIG64"].error or "")
    assert by["OKTBL"].status == "pass", by["OKTBL"]
    assert report.ok is False
    tables = sqlite_tables(target)
    assert "BIG64" not in tables
    assert "OKTBL" in tables


def test_odbc_convert_bigint_cli_exit_3(bigint_db, tmp_path):
    target = tmp_path / "o.sqlite"
    rc = cli.main(["convert", str(bigint_db), "--reader", "odbc", "--target", str(target)])
    assert rc == 3
    assert "BIG64" not in sqlite_tables(target)


def test_odbc_bigint_guard_works_with_copy_to_temp(bigint_db, monkeypatch):
    calls: list[Path] = []
    real = access_parser_reader.column_type_codes

    def spy(source, table):
        calls.append(Path(source))
        return real(source, table)

    monkeypatch.setattr(access_parser_reader, "column_type_codes", spy)
    with (
        open_reader(bigint_db, reader="odbc", copy_to_temp=True) as r,
        pytest.raises(UnsupportedAccessFeature, match="--reader pure"),
    ):
        r.get_table("BIG64")
    assert calls and all(p != bigint_db for p in calls)  # the copy was checked


def test_odbc_bigint_convert_copy_to_temp_is_error(bigint_db, tmp_path):
    target = tmp_path / "o.sqlite"
    report = convert(bigint_db, str(target), reader="odbc", copy_to_temp=True)
    by = {t.name: t for t in report.tables}
    assert by["BIG64"].status == "error"
    assert by["OKTBL"].status == "pass"
    assert "BIG64" not in sqlite_tables(target)


def test_pure_reader_converts_bigint_values(bigint_db, tmp_path):
    expected = [(1, BIG), (2, -BIG), (3, 0), (4, None)]
    assert read_all(bigint_db, "BIG64", "pure") == expected
    target = tmp_path / "o.sqlite"
    report = convert(bigint_db, str(target), reader="pure", checksum=True)
    by = {t.name: t for t in report.tables}
    assert by["BIG64"].status == "pass", by["BIG64"]
    assert report.ok
    con = sqlite3.connect(str(target))
    try:
        assert sorted(con.execute("SELECT ID, BI FROM BIG64")) == sorted(
            expected, key=lambda t: t[0]
        )
    finally:
        con.close()


# -------------------------------------------------------------------- no false positive, no cost


def test_genuine_char4_column_is_not_refused(char4_db, tmp_path, monkeypatch):
    calls = count_cross_checks(monkeypatch)
    with open_reader(char4_db, reader="odbc") as r:
        info = r.get_table("C4")
        col = next(c for c in info.columns if c.name == "C")
        assert (col.access_type, col.size) == ("CHAR", 4)  # the pre-filter matched...
    assert calls == ["C4"]  # ...and the cross-check ran, without a false positive
    odbc_rows = read_all(char4_db, "C4", "odbc")
    assert odbc_rows == read_all(char4_db, "C4", "pure")
    values = [v.rstrip() if isinstance(v, str) else v for _, v in odbc_rows]
    assert values == C4_VALUES
    target = tmp_path / "o.sqlite"
    report = convert(char4_db, str(target), reader="odbc", checksum=True)
    assert report.ok, report.tables
    con = sqlite3.connect(str(target))
    try:
        assert sorted(con.execute("SELECT ID, C FROM C4")) == odbc_rows
    finally:
        con.close()


def test_cross_check_skipped_without_char4_column(char4_db, monkeypatch):
    calls = count_cross_checks(monkeypatch)
    with open_reader(char4_db, reader="odbc") as r:
        r.get_table("PLAIN")
        list(r.iter_rows("PLAIN"))
    assert calls == []


def test_cross_check_runs_once_per_table(char4_db, monkeypatch):
    calls = count_cross_checks(monkeypatch)
    with open_reader(char4_db, reader="odbc") as r:
        r.get_table("C4")
        r.get_table("C4")
        list(r.iter_rows("C4"))
    assert calls == ["C4"]


def test_cross_check_failure_warns_and_keeps_driver_metadata(char4_db, monkeypatch, caplog):
    def broken(source, table):
        raise ValueError("unparseable")

    monkeypatch.setattr(access_parser_reader, "column_type_codes", broken)
    with (
        caplog.at_level(logging.WARNING, logger="acade2sql.readers.pyodbc_reader"),
        open_reader(char4_db, reader="odbc") as r,
    ):
        info = r.get_table("C4")
        rows = sorted((tuple(x) for b in r.iter_rows("C4") for x in b), key=lambda t: t[0])
    assert [c.name for c in info.columns] == ["ID", "C"]
    assert len(rows) == len(C4_VALUES)
    assert any(
        rec.levelno == logging.WARNING and "cross-check" in rec.getMessage()
        for rec in caplog.records
    )


# ------------------------------------------------------------------------------------- exports


def test_errors_exported_from_package():
    from acade2sql import SourceIntegrityError, UnsupportedAccessFeature  # noqa: F401

    assert "SourceIntegrityError" in acade2sql.__all__
    assert "UnsupportedAccessFeature" in acade2sql.__all__
    assert access_parser_reader.UnsupportedAccessFeature is acade2sql.UnsupportedAccessFeature
    assert acade2sql.errors.SourceIntegrityError is acade2sql.SourceIntegrityError
