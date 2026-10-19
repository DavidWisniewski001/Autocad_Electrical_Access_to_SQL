"""full -> modify source copy -> sync on the synthetic .accdb (docs/M4_SPEC.md section 6.2).

The session fixture file is never touched: a copy is opened read-write, only the copy is modified.
"""

# ruff: noqa: RUF001
from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal

import pytest
from tests.fakes import by_name, sqlite_url
from tests.fixtures import build_access as ba

from acade2sql import converter
from acade2sql.readers import open_reader
from acade2sql.writers import SqlAlchemyWriter

pytestmark = pytest.mark.requires_driver

EXCLUDE = ["PNLCOMP"]  # no key at all: covered by test_m4_modes
ALL_TABLES = sorted(ba.MANIFEST)
SYNC_TABLES = [t for t in ALL_TABLES if t not in EXCLUDE]


def _modify(path):
    con = ba.connect_rw(path)
    cur = con.cursor()
    try:
        # COMP (pk HDL): NULL->'', ''->NULL, trailing space, unicode change, delete, 2 inserts
        cur.execute("UPDATE [COMP] SET [DESC2] = '' WHERE [HDL] = 'H0001'")
        cur.execute("UPDATE [COMP] SET [DESC3] = NULL WHERE [HDL] = 'H0001'")
        cur.execute("UPDATE [COMP] SET [TAGNAME] = 'CR2 ' WHERE [HDL] = 'H0002'")
        cur.execute("UPDATE [COMP] SET [DESC1] = ? WHERE [HDL] = ?", "µΩ° změna", "H0003")
        cur.execute("DELETE FROM [COMP] WHERE [HDL] = 'H0005'")
        ba._insert(
            cur,
            "COMP",
            ["HDL", "TAGNAME", "CAT"],
            [("H0006", "CR6", "CAT-6"), ("H0007", "", None)],
        )
        # TERMS (composite pk)
        cur.execute("UPDATE [TERMS] SET [PIN] = 'B1' WHERE [HDL] = 'H0001' AND [TERM] = '1'")
        cur.execute("UPDATE [TERMS] SET [PIN] = '' WHERE [HDL] = 'H0001' AND [TERM] = '2'")
        cur.execute("DELETE FROM [TERMS] WHERE [HDL] = 'H0002'")
        ba._insert(cur, "TERMS", ["HDL", "TERM", "WIRENO", "PIN"], [("H0003", "1", "W9", "C1")])
        # WIRENO (RECNUM)
        cur.execute("UPDATE [WIRENO] SET [COLOR] = '' WHERE [RECNUM] = 2")
        cur.execute("DELETE FROM [WIRENO] WHERE [RECNUM] = 3")
        ba._insert(
            cur, "WIRENO", ["WIRENO", "SHEET", "COLOR"], [("W4", "3", "GRN"), ("W5", "3", None)]
        )
        # WFRM2ALL: one numeric update
        cur.execute("UPDATE [WFRM2ALL] SET [LEN] = 2.5 WHERE [RECNUM] = 1")
        # NETS: delete everything -> empty source table
        cur.execute("DELETE FROM [NETS]")
        # XX: unicode swaps, ''<->NULL, memo change, deletes, inserts
        cur.execute("UPDATE [XX] SET [MFG] = ? WHERE [RECNUM] = 2", "Ω · – µ °")
        cur.execute("UPDATE [XX] SET [MFG] = NULL WHERE [RECNUM] = 6")
        cur.execute("UPDATE [XX] SET [DESC1] = '' WHERE [RECNUM] = 7")
        cur.execute("UPDATE [XX] SET [NOTES] = ? WHERE [RECNUM] = 8", ba.LONG_MEMO + " +edit")
        cur.execute("DELETE FROM [XX] WHERE [RECNUM] >= 39")
        ba._insert(
            cur,
            "XX",
            ["CATALOG", "ASSEMBLYCODE", "MFG", "DESC1", "NOTES"],
            [(f"P-1{i:02d}", "AS9", "NEW", f"new {i}", None) for i in range(3)],
        )
        # TYPED
        cur.execute(
            "UPDATE [TYPED] SET [TXT] = ?, [CUR] = ?, [DT] = ? WHERE [ID] = 1",
            "changed µ",
            Decimal("1.0001"),
            dt.datetime(2000, 2, 29, 12, 0, 0),
        )
        cur.execute("DELETE FROM [TYPED] WHERE [ID] = 4")
        ba._insert(
            cur,
            "TYPED",
            ["L", "S", "B", "DBL", "RL", "CUR", "DT", "YN", "G", "DEC", "BIN", "TXT"],
            [
                (
                    7, 1, 2, 0.5, 0.5, Decimal("9.99"), dt.datetime(2020, 1, 2, 3, 4, 5), True,
                    "{AAAAAAAA-0000-0000-0000-EEEEEEEEEEEE}", Decimal("1.5"), b"\x01", "new",
                )
            ],
        )  # fmt: skip
        # BIG: 100 updated, 45 deleted (RECNUM > 12300), 30 inserted
        cur.execute("UPDATE [BIG] SET [VAL] = [VAL] + 1 WHERE [RECNUM] <= 100")
        cur.execute("DELETE FROM [BIG] WHERE [RECNUM] > 12300")
        ba._insert(
            cur, "BIG", ["KEY1", "VAL", "TXT"], [(f"N{i:04d}", i, f"n{i}") for i in range(30)]
        )
    finally:
        cur.close()
        con.close()


@pytest.fixture(scope="module")
def modified(accdb_original, tmp_path_factory):
    d = tmp_path_factory.mktemp("m4_mod")
    dst = d / "modified.accdb"
    shutil.copyfile(accdb_original, dst)
    _modify(dst)
    return dst


def _read_all(path, name):
    with open_reader(path) as r:
        return [row for b in r.iter_rows(name, 1000) for row in b], r.get_table(name)


def _oracle(orig, mod, name):
    """Independent expected counters, from reading both sources."""
    old, info = _read_all(orig, name)
    new, _ = _read_all(mod, name)
    idx = [info.column_names.index(c) for c in info.primary_key]

    def keyed(rows):
        return {tuple(r[i] for i in idx): r for r in rows}

    o, n = keyed(old), keyed(new)
    assert len(o) == len(old) and len(n) == len(new)
    return dict(
        inserted=len(n.keys() - o.keys()),
        deleted=len(o.keys() - n.keys()),
        updated=sum(1 for k in n.keys() & o.keys() if n[k] != o[k]),
        unchanged=sum(1 for k in n.keys() & o.keys() if n[k] == o[k]),
        source_rows=len(new),
    )


def _target(url, name):
    with SqlAlchemyWriter(url) as w:
        return [r for b in w.read_rows(name, 1000) for r in b]


def norm(rows):
    return sorted(([(type(v).__name__, v) for v in r] for r in rows), key=repr)


def _run(accdb_original, modified, d, impl):
    """full(original) -> sync(modified) [-> staged variants]; returns reports and db paths."""
    from tests.conftest import patch_writer

    with pytest.MonkeyPatch.context() as mp:
        patch_writer(mp, impl == "generic")
        out = {}
        # A: direct full -> sync --delete-missing -> sync again
        a = d / f"a_{impl}.sqlite"
        ua = sqlite_url(a)
        assert converter.convert(accdb_original, ua, checksum=True).ok
        out["a_sync"] = converter.convert(
            modified, ua, mode="sync", checksum=True, delete_missing=True, exclude=EXCLUDE
        )
        out["a_again"] = converter.convert(
            modified, ua, mode="sync", checksum=True, delete_missing=True, exclude=EXCLUDE
        )
        # B: sync without delete-missing (extra rows reported), then with it
        b = d / f"b_{impl}.sqlite"
        ub = sqlite_url(b)
        assert converter.convert(accdb_original, ub).ok
        out["b_nodel"] = converter.convert(
            modified, ub, mode="sync", checksum=True, exclude=EXCLUDE
        )
        out["b_del"] = converter.convert(
            modified, ub, mode="sync", checksum=True, delete_missing=True, exclude=EXCLUDE
        )
        out["a_url"], out["b_url"] = ua, ub
        out["a"], out["b"] = a, b
        return out


@pytest.fixture(scope="module")
def runs(accdb_original, modified, tmp_path_factory):
    d = tmp_path_factory.mktemp("m4_runs")
    return {impl: _run(accdb_original, modified, d, impl) for impl in ("native", "generic")}


@pytest.fixture(scope="module")
def oracle(accdb_original, modified):
    return {n: _oracle(accdb_original, modified, n) for n in SYNC_TABLES}


@pytest.mark.parametrize("impl", ["native", "generic"])
def test_sync_counters_exact(runs, oracle, impl):
    rep = runs[impl]["a_sync"]
    assert rep.ok, rep.render_text()
    res = by_name(rep)
    assert set(res) == set(SYNC_TABLES)
    for name, exp in oracle.items():
        t = res[name]
        assert t.status == "pass", (name, t)
        got = dict(
            inserted=t.inserted,
            deleted=t.deleted,
            updated=t.updated,
            unchanged=t.unchanged,
            source_rows=t.source_rows,
        )
        assert got == exp, name
        assert t.target_rows == t.source_rows, name
        assert t.checksum_match is True, name
        assert t.inserted + t.updated + t.unchanged == t.source_rows, name


def test_oracle_sanity_hard_coded(oracle):
    assert oracle["COMP"] == dict(inserted=2, updated=3, deleted=1, unchanged=1, source_rows=6)
    assert oracle["BIG"] == dict(
        inserted=30, updated=100, deleted=45, unchanged=12_200, source_rows=12_330
    )
    assert oracle["NETS"]["source_rows"] == 0 and oracle["NETS"]["deleted"] == 2
    assert oracle["TERMS"]["inserted"] == 1 and oracle["TERMS"]["deleted"] == 1


@pytest.mark.parametrize("impl", ["native", "generic"])
def test_target_equals_modified_source_value_exact(runs, modified, impl):
    url = runs[impl]["a_url"]
    for name in SYNC_TABLES:
        src, _ = _read_all(modified, name)
        assert norm(_target(url, name)) == norm(src), name


@pytest.mark.parametrize("impl", ["native", "generic"])
def test_excluded_table_untouched(runs, accdb_original, impl):
    src, _ = _read_all(accdb_original, "PNLCOMP")
    assert norm(_target(runs[impl]["a_url"], "PNLCOMP")) == norm(src)


@pytest.mark.parametrize("impl", ["native", "generic"])
def test_second_sync_is_noop(runs, impl):
    rep = runs[impl]["a_again"]
    assert rep.ok, rep.render_text()
    for t in rep.tables:
        assert (t.inserted, t.updated, t.deleted) == (0, 0, 0), t.name
        assert t.unchanged == t.source_rows, t.name
        assert t.checksum_match is True, t.name


@pytest.mark.parametrize("impl", ["native", "generic"])
def test_without_delete_missing_reports_extra_then_delete_removes(runs, oracle, impl):
    nodel, dele = by_name(runs[impl]["b_nodel"]), by_name(runs[impl]["b_del"])
    assert runs[impl]["b_nodel"].ok, runs[impl]["b_nodel"].render_text()
    for name, exp in oracle.items():
        t = nodel[name]
        assert t.deleted in (0, None), name
        assert t.extra_rows == exp["deleted"], name
        assert t.target_rows == exp["source_rows"] + exp["deleted"], name
        assert (t.inserted, t.updated) == (exp["inserted"], exp["updated"]), name
        d = dele[name]
        assert d.status == "pass", name
        assert (d.inserted, d.updated) == (0, 0), name
        assert d.deleted == exp["deleted"], name
        assert d.target_rows == exp["source_rows"], name


def test_native_and_generic_paths_agree(runs):
    for key in ("a_sync", "a_again", "b_nodel", "b_del"):
        n = [(t.name, t.status, t.inserted, t.updated, t.deleted, t.unchanged, t.extra_rows,
              t.source_rows, t.target_rows) for t in runs["native"][key].tables]  # fmt: skip
        g = [(t.name, t.status, t.inserted, t.updated, t.deleted, t.unchanged, t.extra_rows,
              t.source_rows, t.target_rows) for t in runs["generic"][key].tables]  # fmt: skip
        assert n == g, key
    for name in SYNC_TABLES:
        assert norm(_target(runs["native"]["a_url"], name)) == norm(
            _target(runs["generic"]["a_url"], name)
        ), name


def test_sources_unchanged_and_no_lock_files(runs, accdb_original, modified):
    assert runs  # ensure the sync runs happened first
    for p in (accdb_original, modified):
        assert not [q for q in p.parent.iterdir() if q.suffix.lower() in (".ldb", ".laccdb")], p
    # the original still has its original content (COMP H0005 present, 5 rows)
    rows, _ = _read_all(accdb_original, "COMP")
    assert len(rows) == 5 and any(r[0] == "H0005" for r in rows)


def test_no_key_table_errors_by_default_on_fixture(accdb_original, modified, tmp_path):
    url = sqlite_url(tmp_path / "nk.sqlite")
    assert converter.convert(accdb_original, url).ok
    rep = converter.convert(modified, url, mode="sync", tables=["PNLCOMP"])
    t = by_name(rep)["PNLCOMP"]
    assert t.status == "error" and "no key for sync" in (t.error or "")
    rep = converter.convert(modified, url, mode="sync", tables=["PNLCOMP"], no_key="replace")
    assert rep.ok, rep.render_text()


def test_append_on_fixture_fresh_equals_full_then_duplicates_error(accdb_original, tmp_path):
    full, app = tmp_path / "f.sqlite", tmp_path / "a.sqlite"
    assert converter.convert(accdb_original, sqlite_url(full)).ok
    rep = converter.convert(accdb_original, sqlite_url(app), mode="append")
    assert rep.ok, rep.render_text()
    for name in ALL_TABLES:
        assert norm(_target(sqlite_url(app), name)) == norm(_target(sqlite_url(full), name)), name
    rep2 = converter.convert(accdb_original, sqlite_url(app), mode="append")
    res = by_name(rep2)
    keyed = [n for n in ALL_TABLES if ba.MANIFEST[n]["pk"]]
    for name in keyed:
        if ba.MANIFEST[name]["count"]:
            assert res[name].status == "error", name
    # PNLCOMP has no key: append just doubles it
    assert res["PNLCOMP"].status == "pass"
    assert res["PNLCOMP"].target_rows == 2 * ba.MANIFEST["PNLCOMP"]["count"]
    for name in keyed:  # rolled back: unchanged
        assert len(_target(sqlite_url(app), name)) == ba.MANIFEST[name]["count"], name
