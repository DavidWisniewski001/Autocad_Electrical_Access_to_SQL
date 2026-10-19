"""End-to-end tests of acade2sql.convert on the synthetic fixture (spec M3 section 4)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.fixtures.build_access import BIG_ROWS, MANIFEST, big_row

from acade2sql.converter import convert
from acade2sql.errors import TableNotFoundError, WriteError
from acade2sql.readers import open_reader

pytestmark = pytest.mark.requires_driver


def sqlite_url(p: Path) -> str:
    return "sqlite:///" + p.as_posix()


def db_names(p: Path) -> set[str]:
    con = sqlite3.connect(p)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def target_rows(url: str, name: str, batch_size: int = 5000) -> list[tuple]:
    from acade2sql.writers import SqlAlchemyWriter

    with SqlAlchemyWriter(url) as w:
        return [r for b in w.read_rows(name, batch_size) for r in b]


def source_rows(path: Path, name: str) -> list[tuple]:
    with open_reader(path) as r:
        return [row for b in r.iter_rows(name, 1000) for row in b]


def norm(rows):
    """Sort-stable, type-strict view: (type, value) per cell."""
    return sorted(([(type(v).__name__, v) for v in r] for r in rows), key=repr)


def results(report):
    return {t.name: t for t in report.tables}


@pytest.fixture(scope="module")
def converted(access_db, tmp_path_factory):
    d = tmp_path_factory.mktemp("conv")
    url = sqlite_url(d / "out.sqlite")
    rep = convert(access_db, url, checksum=True)
    return access_db, url, d / "out.sqlite", rep


# ---- full conversion -----------------------------------------------------------------


def test_all_tables_pass(converted):
    _src, _url, _path, rep = converted
    assert rep.ok, rep.render_text()
    assert set(results(rep)) == set(MANIFEST)
    for name, t in results(rep).items():
        assert t.status == "pass", (name, t)
        assert t.source_rows == t.target_rows == MANIFEST[name]["count"], name
        assert t.checksum_match is True, name
        assert t.error is None
    assert rep.mode == "full"
    assert rep.started and rep.finished
    assert rep.render_text().splitlines()[-1] == (
        f"{len(MANIFEST)} tables: {len(MANIFEST)} passed, 0 failed, 0 errors"
    )


def test_report_target_masked_and_source(converted):
    src, _url, _p, rep = converted
    assert str(src.name) in rep.source
    assert "sqlite" in rep.target


def test_target_columns_match_manifest(converted):
    _src, _url, path, _rep = converted
    con = sqlite3.connect(path)
    try:
        for name, m in MANIFEST.items():
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
            assert cols == m["columns"], name
            pk = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")') if r[5]]
            assert sorted(pk) == sorted(m["pk"]), name
    finally:
        con.close()


def test_source_vs_target_value_exact_every_table(converted):
    src, url, _p, _rep = converted
    for name in MANIFEST:
        s = source_rows(src, name)
        t = target_rows(url, name)
        assert len(s) == len(t) == MANIFEST[name]["count"], name
        assert norm(s) == norm(t), name


@pytest.mark.parametrize("name", [n for n, m in MANIFEST.items() if "rows" in m])
def test_target_matches_manifest(converted, name):
    _src, url, _p, _rep = converted
    got = target_rows(url, name)
    exp = MANIFEST[name]["rows"]
    assert norm(got) == norm(exp), name


def test_big_table_exact_and_ordered(converted):
    _src, url, _p, _rep = converted
    rows = target_rows(url, "BIG", batch_size=1000)
    assert len(rows) == BIG_ROWS
    assert rows == [(i, *big_row(i)) for i in range(1, BIG_ROWS + 1)]  # ordered by PK RECNUM


def test_typed_edge_values(converted):
    _src, url, _p, _rep = converted
    rows = {r[0]: r for r in target_rows(url, "TYPED")}
    cols = MANIFEST["TYPED"]["columns"]
    exp = {r[0]: r for r in MANIFEST["TYPED"]["rows"]}
    assert set(rows) == set(exp)
    for i, e in exp.items():
        for c, gv, ev in zip(cols, rows[i], e, strict=True):
            assert type(gv) is type(ev), (i, c, gv, ev)
            assert gv == ev, (i, c, gv, ev)
    assert rows[2][cols.index("BIN")] == b"\x00"
    assert rows[1][cols.index("BIN")] == b"\x00\x01\xff"


def test_null_vs_empty_and_padding_and_unicode(converted):
    _src, url, _p, _rep = converted
    rows = {r[-1]: r for r in target_rows(url, "XX")}  # RECNUM last
    cols = MANIFEST["XX"]["columns"]
    mfg = cols.index("MFG")
    assert rows[1][mfg] == "  ABC  "
    assert rows[2][mfg] == "µ · – Ω °"  # noqa: RUF001
    assert rows[3][mfg] == "日本語テキスト"
    assert rows[4][mfg] == "emoji \U0001f600 ok"
    assert rows[5][mfg] == "line1\r\nline2"
    assert rows[6][mfg] == ""
    assert rows[7][mfg] is None


def test_long_memo(converted):
    _src, url, _p, _rep = converted
    from tests.fixtures.build_access import LONG_MEMO

    notes = MANIFEST["XX"]["columns"].index("NOTES")
    rows = {r[-1]: r for r in target_rows(url, "XX")}
    row = next(r for r in rows.values() if r[0] == "P-007")
    assert row[notes] == LONG_MEMO and len(row[notes]) > 6400


def test_hostile_names_in_sqlite_master(converted):
    _src, _url, path, _rep = converted
    names = db_names(path)
    for n in ("ALLEN BRADLEY", "ALLEN-BRADLEY", "KRAUS & NAIMER", "DO", "_PINLIST_BRUNDY/HUBBEL"):
        assert n in names


def test_indexes_unique_database_wide(converted):
    _src, _url, path, _rep = converted
    con = sqlite3.connect(path)
    try:
        ix = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        ]
        assert len(ix) == len(set(ix))
        assert len(ix) >= 2  # IDX_CAT on XX and _PINLIST_XX
        for t in ("XX", "_PINLIST_XX"):
            lst = con.execute(f'PRAGMA index_list("{t}")').fetchall()
            assert [r for r in lst if r[3] == "c"], t
    finally:
        con.close()


def test_empty_table(converted):
    _src, url, _p, rep = converted
    assert results(rep)["EMPTYFAM"].target_rows == 0
    assert target_rows(url, "EMPTYFAM") == []


def test_source_unchanged_no_locks(converted):
    src, *_ = converted
    assert not [p for p in src.parent.iterdir() if p.suffix.lower() in (".ldb", ".laccdb")]


def test_source_file_unchanged_by_convert(access_db, tmp_path):
    before = access_db.stat()
    convert(access_db, sqlite_url(tmp_path / "o.sqlite"), tables=["COMP"])
    after = access_db.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


# ---- modes / batching -------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [1000, 7])
def test_big_with_batch_sizes(access_db, tmp_path, batch_size):
    url = sqlite_url(tmp_path / "b.sqlite")
    rep = convert(access_db, url, tables=["BIG"], batch_size=batch_size, checksum=True)
    assert rep.ok
    (t,) = rep.tables
    assert t.name == "BIG" and t.target_rows == BIG_ROWS and t.checksum_match is True
    assert target_rows(url, "BIG") == [(i, *big_row(i)) for i in range(1, BIG_ROWS + 1)]


def test_rerun_full_is_idempotent(access_db, tmp_path):
    url = sqlite_url(tmp_path / "r.sqlite")
    names = ["COMP", "TERMS", "XX", "TYPED", "PNLCOMP"]
    a = convert(access_db, url, tables=names)
    first = {n: target_rows(url, n) for n in names}
    b = convert(access_db, url, tables=names)
    assert a.ok and b.ok
    for n in names:
        assert results(b)[n].target_rows == MANIFEST[n]["count"]
        assert norm(target_rows(url, n)) == norm(first[n])


def test_unrelated_table_survives_convert(access_db, tmp_path):
    path = tmp_path / "u.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE keepme (a INTEGER)")
    con.execute("INSERT INTO keepme VALUES (42)")
    con.commit()
    con.close()
    rep = convert(access_db, sqlite_url(path), tables=["COMP"])
    assert rep.ok
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT a FROM keepme").fetchall() == [(42,)]
        assert con.execute("SELECT COUNT(*) FROM COMP").fetchone()[0] == 5
    finally:
        con.close()


def test_copy_to_temp(access_db, tmp_path):
    rep = convert(access_db, sqlite_url(tmp_path / "c.sqlite"), tables=["COMP"], copy_to_temp=True)
    assert rep.ok and rep.tables[0].target_rows == 5


def test_without_checksum_match_is_none(access_db, tmp_path):
    rep = convert(access_db, sqlite_url(tmp_path / "n.sqlite"), tables=["COMP"])
    assert rep.tables[0].checksum_match is None


def test_accepts_str_source_and_plain_path_target(access_db, tmp_path):
    out = tmp_path / "plain.sqlite"
    rep = convert(str(access_db), str(out), tables=["NETS"])
    assert rep.ok and out.exists()
    assert db_names(out) == {"NETS"}


# ---- filters ------------------------------------------------------------------------------


def test_tables_filter_case_insensitive(access_db, tmp_path):
    out = tmp_path / "f.sqlite"
    rep = convert(access_db, sqlite_url(out), tables=["comp", "Terms"])
    assert {t.name for t in rep.tables} == {"COMP", "TERMS"}
    assert db_names(out) == {"COMP", "TERMS"}


def test_exclude_filter_case_insensitive(access_db, tmp_path):
    out = tmp_path / "e.sqlite"
    rep = convert(access_db, sqlite_url(out), exclude=["big", "typed"])
    names = {t.name for t in rep.tables}
    assert names == set(MANIFEST) - {"BIG", "TYPED"}
    assert not ({"BIG", "TYPED"} & db_names(out))


def test_exclude_wins_over_tables(access_db, tmp_path):
    out = tmp_path / "w.sqlite"
    rep = convert(access_db, sqlite_url(out), tables=["COMP", "TERMS"], exclude=["terms"])
    assert [t.name for t in rep.tables] == ["COMP"]
    assert db_names(out) == {"COMP"}


def test_unknown_table_raises_before_writing(access_db, tmp_path):
    out = tmp_path / "x.sqlite"
    with pytest.raises(TableNotFoundError) as ei:
        convert(access_db, sqlite_url(out), tables=["COMP", "NOPE"])
    assert "NOPE" in str(ei.value)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


# ---- report file -------------------------------------------------------------------------------


def test_report_path_written(access_db, tmp_path):
    rp = tmp_path / "rep.json"
    rep = convert(
        access_db, sqlite_url(tmp_path / "o.sqlite"), tables=["COMP", "NETS"], report_path=rp
    )
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert doc["mode"] == "full"
    assert [t["name"] for t in doc["tables"]] == [t.name for t in rep.tables]
    assert all(t["status"] == "pass" for t in doc["tables"])
    assert doc["tables"][0]["source_rows"] == results(rep)["COMP"].source_rows


def test_report_path_str(access_db, tmp_path):
    rp = tmp_path / "rep2.json"
    convert(access_db, sqlite_url(tmp_path / "o.sqlite"), tables=["NETS"], report_path=str(rp))
    json.loads(rp.read_text(encoding="utf-8"))


# ---- failure injection ------------------------------------


@pytest.fixture
def inject_failure(monkeypatch):
    from acade2sql.writers.sqlalchemy_writer import SqlAlchemyWriter

    orig = SqlAlchemyWriter.write_batches

    def fake(self, info, batches):
        if info.name == "TERMS":
            raise WriteError("TERMS", RuntimeError("injected boom"))
        return orig(self, info, batches)

    monkeypatch.setattr(SqlAlchemyWriter, "write_batches", fake)


def test_injected_failure_isolated(access_db, tmp_path, inject_failure):
    url = sqlite_url(tmp_path / "fail.sqlite")
    rp = tmp_path / "fail.json"
    rep = convert(access_db, url, tables=["COMP", "TERMS", "NETS", "WIRENO"], report_path=rp)
    res = results(rep)
    assert rep.ok is False
    assert res["TERMS"].status == "error"
    assert "injected boom" in (res["TERMS"].error or "")
    for n in ("COMP", "NETS", "WIRENO"):
        assert res[n].status == "pass", n
        assert res[n].target_rows == MANIFEST[n]["count"]
    assert norm(target_rows(url, "COMP")) == norm(MANIFEST["COMP"]["rows"])
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert {t["name"]: t["status"] for t in doc["tables"]}["TERMS"] == "error"
    text = rep.render_text()
    assert any(ln.startswith("ERROR") and "TERMS" in ln for ln in text.splitlines())
    assert text.splitlines()[-1] == "4 tables: 3 passed, 0 failed, 1 errors"


def test_failure_in_first_and_last_table_continues(access_db, tmp_path, monkeypatch):
    from acade2sql.writers.sqlalchemy_writer import SqlAlchemyWriter

    orig = SqlAlchemyWriter.write_batches

    def fake(self, info, batches):
        if info.name in ("COMP", "WIRENO"):  # first and last alphabetically
            raise WriteError(info.name, RuntimeError("x"))
        return orig(self, info, batches)

    monkeypatch.setattr(SqlAlchemyWriter, "write_batches", fake)
    rep = convert(
        access_db, sqlite_url(tmp_path / "f2.sqlite"), tables=["COMP", "NETS", "TERMS", "WIRENO"]
    )
    st = {t.name: t.status for t in rep.tables}
    assert st == {"COMP": "error", "NETS": "pass", "TERMS": "pass", "WIRENO": "error"}


def test_generic_exception_also_recorded_as_error(access_db, tmp_path, monkeypatch):
    from acade2sql.writers.sqlalchemy_writer import SqlAlchemyWriter

    orig = SqlAlchemyWriter.write_batches

    def fake(self, info, batches):
        if info.name == "NETS":
            raise RuntimeError("not a WriteError")
        return orig(self, info, batches)

    monkeypatch.setattr(SqlAlchemyWriter, "write_batches", fake)
    rep = convert(access_db, sqlite_url(tmp_path / "g.sqlite"), tables=["COMP", "NETS"])
    st = {t.name: t.status for t in rep.tables}
    assert st == {"COMP": "pass", "NETS": "error"}
    assert "not a WriteError" in (results(rep)["NETS"].error or "")
