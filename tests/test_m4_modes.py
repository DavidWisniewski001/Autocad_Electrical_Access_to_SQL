"""M4 modes (append / sync) on SQLite with an in-memory source (docs/M4_SPEC.md section 3).

Every test that uses the ``impl`` fixture runs twice: once on the default (possibly native
``ON CONFLICT``) path and once with ``writer._force_generic_sync = True``; outcomes must match.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from tests.fakes import (
    FakeReader,
    by_name,
    dump,
    make_info,
    sql,
    sqlite_url,
    std_info,
    std_rows,
)

from acade2sql import cli, converter

NO_DELETES = (0, None)


def conv(tmp_path, reader, install_reader, name="o.sqlite", **kw):
    install_reader(reader)
    return converter.convert("fake.mdb", sqlite_url(tmp_path / name), **kw)


def one(rep, name="T"):
    return by_name(rep)[name]


def reader_t(rows, name="T", info=None):
    return FakeReader({name: (info or std_info(name), rows)})


# ---- sync: counters, values, idempotency ------------------------------------------------------


def v1():
    return std_rows(range(1, 6))


def v2():
    """1 unchanged; 2 label+unicode; 3 NULL->''; 4 trailing space; 5 deleted; 6, 7 new."""
    return [
        (1, "label1", "c1", None),
        (2, "label2 µ", "Ω2", None),
        (3, "label3", "c3", ""),
        (4, "label4", "c4 ", None),
        (6, "label6", "c6", "new"),
        (7, "label7", "c7", None),
    ]


@pytest.fixture
def filled(tmp_path, install_reader, impl):
    rep = conv(tmp_path, reader_t(v1()), install_reader, checksum=True)
    assert rep.ok
    return tmp_path / "o.sqlite"


def test_sync_without_delete_reports_extra(tmp_path, install_reader, filled):
    rep = conv(tmp_path, reader_t(v2()), install_reader, mode="sync", checksum=True)
    t = one(rep)
    assert rep.ok, rep.render_text()
    assert t.status == "pass"
    assert (t.inserted, t.updated, t.unchanged) == (2, 3, 1)
    assert t.deleted in NO_DELETES
    assert t.extra_rows == 1
    assert t.source_rows == 6
    assert t.target_rows == 7
    assert t.checksum_match is True
    rows = dump(filled, "T")
    assert rows == [
        (1, "label1", "c1", None),
        (2, "label2 µ", "Ω2", None),
        (3, "label3", "c3", ""),  # NULL -> '' kept distinct
        (4, "label4", "c4 ", None),  # trailing space kept
        (5, "label5", "c5", None),  # extra row retained
        (6, "label6", "c6", "new"),
        (7, "label7", "c7", None),
    ]
    assert rep.mode == "sync"


def test_sync_with_delete_missing(tmp_path, install_reader, filled):
    rep = conv(
        tmp_path, reader_t(v2()), install_reader, mode="sync", checksum=True, delete_missing=True
    )
    t = one(rep)
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated, t.deleted, t.unchanged) == (2, 3, 1, 1)
    assert t.extra_rows in NO_DELETES
    assert (t.source_rows, t.target_rows) == (6, 6)
    assert t.checksum_match is True
    assert dump(filled, "T") == sorted(v2())


def test_sync_second_run_is_noop(tmp_path, install_reader, filled):
    for _ in range(2):
        rep = conv(
            tmp_path,
            reader_t(v2()),
            install_reader,
            mode="sync",
            checksum=True,
            delete_missing=True,
        )
    t = one(rep)
    assert rep.ok
    assert (t.inserted, t.updated, t.deleted, t.unchanged) == (0, 0, 0, 6)
    assert dump(filled, "T") == sorted(v2())


def test_sync_null_to_empty_and_empty_to_null_are_updates(tmp_path, install_reader, impl):
    conv(tmp_path, reader_t([(1, "a", "x", None), (2, "b", "y", "")]), install_reader)
    rep = conv(
        tmp_path,
        reader_t([(1, "a", "x", ""), (2, "b", "y", None)]),
        install_reader,
        mode="sync",
        checksum=True,
    )
    t = one(rep)
    assert (t.inserted, t.updated, t.unchanged) == (0, 2, 0)
    assert dump(tmp_path / "o.sqlite", "T") == [(1, "a", "x", ""), (2, "b", "y", None)]


def test_sync_creates_missing_target_table(tmp_path, install_reader, impl):
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="sync", checksum=True)
    t = one(rep)
    assert rep.ok
    assert (t.inserted, t.updated, t.unchanged) == (5, 0, 0)
    assert dump(tmp_path / "o.sqlite", "T") == v1()


def test_sync_composite_key(tmp_path, install_reader, impl):
    info = make_info(
        "C",
        [("a", "VARCHAR", 10, False), ("b", "VARCHAR", 10, False), ("v", "VARCHAR", 10)],
        pk=("a", "b"),
    )
    r1 = [("x", "1", "p"), ("x", "2", "q"), ("y", "1", "r")]
    r2 = [("x", "1", "p"), ("x", "2", "Q"), ("z", "9", "s")]
    conv(tmp_path, reader_t(r1, "C", info), install_reader)
    rep = conv(
        tmp_path,
        reader_t(r2, "C", info),
        install_reader,
        mode="sync",
        checksum=True,
        delete_missing=True,
    )
    t = one(rep, "C")
    assert (t.inserted, t.updated, t.deleted, t.unchanged) == (1, 1, 1, 1)
    assert dump(tmp_path / "o.sqlite", "C") == sorted(r2)


def test_sync_empty_source_with_delete_missing_empties_target(tmp_path, install_reader, impl):
    conv(tmp_path, reader_t(v1()), install_reader)
    rep = conv(tmp_path, reader_t([]), install_reader, mode="sync", delete_missing=True)
    t = one(rep)
    assert t.status == "pass"
    assert (t.source_rows, t.target_rows, t.deleted) == (0, 0, 5)


def test_sync_small_batches(tmp_path, install_reader, filled):
    rep = conv(
        tmp_path,
        reader_t(v2()),
        install_reader,
        mode="sync",
        batch_size=2,
        checksum=True,
        delete_missing=True,
    )
    t = one(rep)
    assert (t.inserted, t.updated, t.deleted, t.unchanged) == (2, 3, 1, 1)
    assert dump(filled, "T") == sorted(v2())


def test_generic_path_does_not_use_on_conflict(tmp_path, install_reader, monkeypatch):
    from tests.conftest import patch_writer

    patch_writer(monkeypatch, True)
    seen: list[str] = []

    def spy(conn, cursor, statement, *_a):
        seen.append(statement)

    conv(tmp_path, reader_t(v1()), install_reader)
    event.listen(Engine, "before_cursor_execute", spy)
    try:
        rep = conv(tmp_path, reader_t(v2()), install_reader, mode="sync", delete_missing=True)
    finally:
        event.remove(Engine, "before_cursor_execute", spy)
    assert rep.ok
    assert seen, "expected statements to be observed"
    assert not [s for s in seen if "ON CONFLICT" in s.upper()]


# ---- sync: keys -------------------------------------------------------------------------------


def two_tables(a_rows, b_rows):
    return FakeReader({"A": (std_info("A"), a_rows), "B": (std_info("B"), b_rows)})


def test_global_key_applies_to_tables_having_the_column(tmp_path, install_reader, impl):
    cols = [("id", "COUNTER", None, False), ("label", "VARCHAR", 50), ("code", "VARCHAR", 20)]
    ia = make_info("A", cols)
    ib = make_info("B", [("id", "COUNTER", None, False), ("label", "VARCHAR", 50)])
    a1 = [(1, "l1", "k1"), (2, "l2", "k2")]
    b1 = [(1, "l1"), (2, "l2")]
    r1 = FakeReader({"A": (ia, a1), "B": (ib, b1)})
    conv(tmp_path, r1, install_reader)
    # A: same codes but new ids -> with key=code that is 2 updates; B has no 'code': uses its PK
    a2 = [(11, "l1", "k1"), (12, "l2", "k2")]
    b2 = [(1, "l1"), (3, "l3")]
    rep = conv(
        tmp_path,
        FakeReader({"A": (ia, a2), "B": (ib, b2)}),
        install_reader,
        mode="sync",
        key=["code"],
        delete_missing=True,
        checksum=True,
    )
    a, b = one(rep, "A"), one(rep, "B")
    assert a.status == b.status == "pass", rep.render_text()
    assert (a.inserted, a.updated, a.deleted) == (0, 2, 0)
    assert (b.inserted, b.updated, b.deleted, b.unchanged) == (1, 0, 1, 1)
    assert dump(tmp_path / "o.sqlite", "A") == sorted(a2)


def test_table_key_override_case_insensitive_beats_global(tmp_path, install_reader, impl):
    cols = [("id", "COUNTER", None, False), ("label", "VARCHAR", 50), ("code", "VARCHAR", 20)]
    ia, ib = make_info("Alpha", cols), make_info("Beta", cols)
    # 'label' repeats in Alpha but 'code' is unique; Beta has unique labels
    alpha = [(1, "dup", "k1"), (2, "dup", "k2")]
    beta = [(1, "u1", "k1"), (2, "u2", "k2")]
    rd = FakeReader({"Alpha": (ia, alpha), "Beta": (ib, beta)})
    conv(tmp_path, rd, install_reader)
    rep = conv(
        tmp_path,
        rd,
        install_reader,
        mode="sync",
        key=["label", "alpha=code"],
    )
    a, b = one(rep, "Alpha"), one(rep, "Beta")
    assert rep.ok, rep.render_text()
    assert (a.inserted, a.updated, a.unchanged) == (0, 0, 2)
    assert (b.inserted, b.updated, b.unchanged) == (0, 0, 2)
    # without the override the global key 'label' is not unique in Alpha
    rep2 = conv(tmp_path, rd, install_reader, mode="sync", key=["label"])
    assert one(rep2, "Alpha").status == "error"
    assert one(rep2, "Beta").status == "pass"


def test_table_key_composite_override(tmp_path, install_reader, impl):
    info = make_info(
        "T", [("id", "COUNTER", None, False), ("a", "VARCHAR", 5), ("b", "VARCHAR", 5)]
    )
    rows = [(1, "x", "1"), (2, "x", "2")]
    rd = reader_t(rows, "T", info)
    conv(tmp_path, rd, install_reader)
    rep = conv(tmp_path, rd, install_reader, mode="sync", key=["T=a,b"], checksum=True)
    assert rep.ok, rep.render_text()
    assert one(rep).unchanged == 2


def test_duplicate_source_keys_error_and_rollback(tmp_path, install_reader, filled):
    before = dump(filled, "T")
    bad = [
        (1, "CHANGED", "c1", None),
        (2, "b", "c2", None),
        (6, "n", "c6", None),
        (2, "dup", "z", None),
    ]
    rep = conv(tmp_path, reader_t(bad), install_reader, mode="sync", batch_size=2)
    t = one(rep)
    assert t.status == "error"
    assert "duplicate" in (t.error or "").lower()
    assert dump(filled, "T") == before
    assert rep.ok is False


def test_null_source_key_error_and_rollback(tmp_path, install_reader, impl):
    cols = [("id", "COUNTER", None, False), ("code", "VARCHAR", 20), ("v", "VARCHAR", 20)]
    info = make_info("T", cols)
    rd1 = reader_t([(1, "a", "x"), (2, "b", "y")], "T", info)
    conv(tmp_path, rd1, install_reader)
    before = dump(tmp_path / "o.sqlite", "T")
    rd2 = reader_t([(1, "a", "CHANGED"), (2, "b", "y"), (3, None, "z")], "T", info)
    rep = conv(tmp_path, rd2, install_reader, mode="sync", key=["code"], batch_size=2)
    t = one(rep)
    assert t.status == "error"
    assert "null" in (t.error or "").lower()
    assert dump(tmp_path / "o.sqlite", "T") == before


def test_unknown_key_column_is_error(tmp_path, install_reader, impl):
    conv(tmp_path, reader_t(v1()), install_reader)
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="sync", key=["T=nope"])
    t = one(rep)
    assert t.status == "error"
    assert "nope" in (t.error or "")


def keyless():
    info = make_info("K", [("a", "VARCHAR", 10), ("b", "VARCHAR", 10)], pk=())
    return info


def test_no_key_default_error_leaves_target_unchanged(tmp_path, install_reader, impl):
    rows = [("a", "1"), ("a", "1"), ("b", None)]
    conv(tmp_path, reader_t(rows, "K", keyless()), install_reader)
    before = dump(tmp_path / "o.sqlite", "K")
    rep = conv(tmp_path, reader_t([("z", "9")], "K", keyless()), install_reader, mode="sync")
    t = one(rep, "K")
    assert t.status == "error"
    assert "no key for sync" in (t.error or "")
    assert "--no-key replace" in (t.error or "")
    assert dump(tmp_path / "o.sqlite", "K") == before


def test_no_key_replace_replaces_all_rows(tmp_path, install_reader, impl):
    conv(tmp_path, reader_t([("a", "1"), ("b", "2"), ("c", "3")], "K", keyless()), install_reader)
    new = [("z", "9"), ("z", "9"), ("y", None)]  # duplicates are fine without a key
    rep = conv(
        tmp_path,
        reader_t(new, "K", keyless()),
        install_reader,
        mode="sync",
        no_key="replace",
        checksum=True,
    )
    t = one(rep, "K")
    assert t.status == "pass", t
    assert (t.source_rows, t.target_rows) == (3, 3)
    assert sorted(dump(tmp_path / "o.sqlite", "K"), key=repr) == sorted(new, key=repr)


# ---- sync: schema drift -----------------------------------------------------------------------


def test_drift_target_missing_source_column(tmp_path, install_reader, impl):
    narrow = make_info("T", [("id", "COUNTER", None, False), ("label", "VARCHAR", 50)])
    conv(tmp_path, reader_t([(1, "a"), (2, "b")], "T", narrow), install_reader)
    before = dump(tmp_path / "o.sqlite", "T")
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="sync")
    t = one(rep)
    assert t.status == "error"
    assert "schema drift" in (t.error or "")
    assert "--mode full" in (t.error or "")
    assert dump(tmp_path / "o.sqlite", "T") == before


def test_drift_extra_nullable_target_column_allowed(tmp_path, install_reader, impl):
    conv(tmp_path, reader_t(v1()), install_reader)
    db = tmp_path / "o.sqlite"
    sql(db, 'ALTER TABLE "T" ADD COLUMN extra TEXT')
    sql(db, "UPDATE \"T\" SET extra = 'keep'")
    rep = conv(
        tmp_path, reader_t(v2()), install_reader, mode="sync", checksum=True, delete_missing=True
    )
    t = one(rep)
    assert t.status == "pass", t
    assert t.checksum_match is True
    got = {r[0]: r[-1] for r in dump(db, "T")}
    assert got[1] == "keep"  # existing rows keep their extra value
    assert got[6] is None and got[7] is None  # inserted rows leave it NULL


def test_drift_extra_not_null_target_column_without_default_is_error(
    tmp_path, install_reader, impl
):
    db = tmp_path / "o.sqlite"
    sql(
        db,
        'CREATE TABLE "T" (id INTEGER NOT NULL PRIMARY KEY, label VARCHAR(50), '
        "code VARCHAR(20), note TEXT, extra TEXT NOT NULL)",
    )
    sql(db, "INSERT INTO \"T\" VALUES (1, 'a', 'c1', NULL, 'x')")
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="sync")
    t = one(rep)
    assert t.status == "error"
    assert "schema drift" in (t.error or "")
    assert dump(db, "T") == [(1, "a", "c1", None, "x")]


def test_drift_also_applies_to_append(tmp_path, install_reader, impl):
    narrow = make_info("T", [("id", "COUNTER", None, False), ("label", "VARCHAR", 50)])
    conv(tmp_path, reader_t([(1, "a")], "T", narrow), install_reader)
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="append")
    t = one(rep)
    assert t.status == "error" and "schema drift" in (t.error or "")


# ---- append -----------------------------------------------------------------------------------


def test_append_fresh_target_equals_full(tmp_path, install_reader):
    rows = v2()
    conv(tmp_path, reader_t(rows), install_reader, name="full.sqlite")
    rep = conv(tmp_path, reader_t(rows), install_reader, name="app.sqlite", mode="append")
    t = one(rep)
    assert rep.ok, rep.render_text()
    assert t.target_rows_before in (0, None)
    assert (t.source_rows, t.target_rows) == (6, 6)
    assert dump(tmp_path / "app.sqlite", "T") == dump(tmp_path / "full.sqlite", "T")
    assert rep.mode == "append"


def test_append_onto_filled_table_adds_rows(tmp_path, install_reader):
    conv(tmp_path, reader_t(std_rows(range(1, 4))), install_reader)
    rep = conv(tmp_path, reader_t(std_rows(range(10, 13))), install_reader, mode="append")
    t = one(rep)
    assert t.status == "pass", t
    assert t.target_rows_before == 3
    assert t.target_rows == 6
    assert t.source_rows == 3
    assert len(dump(tmp_path / "o.sqlite", "T")) == 6


def test_append_duplicate_key_errors_and_target_unchanged(tmp_path, install_reader):
    conv(tmp_path, reader_t(v1()), install_reader)
    before = dump(tmp_path / "o.sqlite", "T")
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="append", batch_size=2)
    t = one(rep)
    assert t.status == "error"
    assert rep.ok is False
    assert dump(tmp_path / "o.sqlite", "T") == before


def test_append_duplicate_midway_rolls_back_earlier_batches(tmp_path, install_reader):
    conv(tmp_path, reader_t(std_rows([1, 2])), install_reader)
    before = dump(tmp_path / "o.sqlite", "T")
    rows = std_rows([10, 11, 12, 13]) + std_rows([2])  # collision only in the last batch
    rep = conv(tmp_path, reader_t(rows), install_reader, mode="append", batch_size=2)
    assert one(rep).status == "error"
    assert dump(tmp_path / "o.sqlite", "T") == before


def test_append_without_pk_doubles_rows_and_full_rerun_is_idempotent(tmp_path, install_reader):
    rows = [("a", "1"), ("a", "1"), ("b", None)]
    conv(tmp_path, reader_t(rows, "K", keyless()), install_reader)
    conv(tmp_path, reader_t(rows, "K", keyless()), install_reader)  # full re-run
    assert len(dump(tmp_path / "o.sqlite", "K")) == 3
    rep = conv(tmp_path, reader_t(rows, "K", keyless()), install_reader, mode="append")
    t = one(rep, "K")
    assert t.status == "pass"
    assert (t.target_rows_before, t.target_rows, t.source_rows) == (3, 6, 3)
    assert len(dump(tmp_path / "o.sqlite", "K")) == 6


def test_append_checksum_not_computed_warns(tmp_path, install_reader):
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="append", checksum=True)
    t = one(rep)
    assert t.status == "pass"
    assert t.checksum_match is None
    assert any("checksum" in w.lower() for w in t.warnings), t.warnings


def test_append_creates_missing_table(tmp_path, install_reader):
    rep = conv(tmp_path, reader_t(v1()), install_reader, mode="append")
    assert rep.ok
    assert dump(tmp_path / "o.sqlite", "T") == v1()


def test_append_never_drops_existing_table_extra_data(tmp_path, install_reader):
    conv(tmp_path, reader_t(std_rows([1])), install_reader)
    db = tmp_path / "o.sqlite"
    sql(db, 'ALTER TABLE "T" ADD COLUMN extra TEXT')
    sql(db, "UPDATE \"T\" SET extra='keep'")
    rep = conv(tmp_path, reader_t(std_rows([2])), install_reader, mode="append")
    assert rep.ok
    assert [r[-1] for r in dump(db, "T")] == ["keep", None]


def test_full_mode_unchanged_by_new_fields(tmp_path, install_reader):
    rep = conv(tmp_path, reader_t(v1()), install_reader, checksum=True)
    t = one(rep)
    assert t.status == "pass"
    assert t.inserted is None and t.updated is None and t.deleted is None
    assert "inserted=" not in rep.render_text()


# ---- scale ------------------------------------------------------------------------------------


@pytest.mark.slow
def test_delete_missing_scales_to_tens_of_thousands_of_keys(tmp_path, install_reader, impl):
    n = 60_000
    info = make_info("S", [("id", "COUNTER", None, False), ("v", "VARCHAR", 20)])
    first = [(i, f"v{i}") for i in range(1, n + 1)]
    conv(tmp_path, reader_t(first, "S", info), install_reader, batch_size=5000)
    # source: ids 10_001..70_000 (50_000 overlapping keys + 10_000 new), 1_000 of them changed
    second = [(i, f"v{i}" + ("x" if i % 50 == 0 and i <= n else "")) for i in range(10_001, 70_001)]
    changed = sum(1 for i in range(10_001, n + 1) if i % 50 == 0)
    rep = conv(
        tmp_path,
        reader_t(second, "S", info),
        install_reader,
        mode="sync",
        batch_size=5000,
        delete_missing=True,
        checksum=True,
    )
    t = one(rep, "S")
    assert t.status == "pass", t
    assert t.inserted == 10_000
    assert t.deleted == 10_000
    assert t.updated == changed
    assert t.unchanged == 50_000 - changed
    assert t.source_rows == t.target_rows == 60_000
    assert sql(tmp_path / "o.sqlite", 'SELECT COUNT(*), MIN(id), MAX(id) FROM "S"') == [
        (60_000, 10_001, 70_000)
    ]


# ---- CLI exit codes ---------------------------------------------------------------------------


def run_cli(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_sync_roundtrip_and_exit_codes(capsys, tmp_path, install_reader, impl):
    db = str(tmp_path / "c.sqlite")
    install_reader(reader_t(v1()))
    assert run_cli(capsys, "convert", "fake.mdb", "--target", db)[0] == 0
    install_reader(reader_t(v2()))
    rc, out, err = run_cli(
        capsys,
        "convert",
        "fake.mdb",
        "--target",
        db,
        "--mode",
        "sync",
        "--delete-missing",
        "--checksum",
    )
    assert rc == 0, err
    line = next(ln for ln in out.splitlines() if ln.startswith("PASS T"))
    assert "inserted=2" in line and "updated=3" in line and "deleted=1" in line
    # append of the same rows now collides -> exit 3
    rc, out, _ = run_cli(capsys, "convert", "fake.mdb", "--target", db, "--mode", "append")
    assert rc == 3
    assert out.splitlines()[0].startswith("ERROR")


def test_cli_key_and_no_key_flags(capsys, tmp_path, install_reader, impl):
    db = str(tmp_path / "k.sqlite")
    install_reader(reader_t([("a", "1")], "K", keyless()))
    assert run_cli(capsys, "convert", "fake.mdb", "--target", db)[0] == 0
    rc, out, _ = run_cli(capsys, "convert", "fake.mdb", "--target", db, "--mode", "sync")
    assert rc == 3 and "no key for sync" in out
    rc, out, _ = run_cli(
        capsys, "convert", "fake.mdb", "--target", db, "--mode", "sync", "--no-key", "replace"
    )
    assert rc == 0, out
    rc, out, _ = run_cli(
        capsys, "convert", "fake.mdb", "--target", db, "--mode", "sync", "--key", "K=a"
    )
    assert rc == 0, out
