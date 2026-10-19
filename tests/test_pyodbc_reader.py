# ruff: noqa: RUF001, RUF005
from __future__ import annotations

import datetime as dt
import decimal
import itertools
import shutil
from pathlib import Path

import pyodbc
import pytest
from tests.fixtures.build_access import BIG_ROWS, MANIFEST, TYPED_ROWS, big_row

from acade2sql.errors import SourceNotFoundError, TableNotFoundError
from acade2sql.readers import open_reader
from acade2sql.readers.pyodbc_reader import PyodbcReader
from acade2sql.schema import TableInfo

pytestmark = pytest.mark.requires_driver

D = decimal.Decimal


def _all_rows(reader, name, batch_size=5000):
    return list(itertools.chain.from_iterable(reader.iter_rows(name, batch_size=batch_size)))


# ---------------------------------------------------------------- list_tables

def test_list_tables_exact_and_sorted(reader):
    names = reader.list_tables()
    assert names == sorted(names, key=str.casefold)
    assert set(names) == set(MANIFEST)


def test_list_tables_no_system_tables(reader):
    assert not [n for n in reader.list_tables() if n.upper().startswith("MSYS")]


def test_list_tables_include_system(reader):
    with_sys = reader.list_tables(include_system=True)
    assert any(n.startswith("MSys") for n in with_sys)
    assert set(reader.list_tables()) < set(with_sys)


def test_system_tables_not_resolvable(reader):
    # DECISIONS #13: MSys* tables are listed but never resolvable by name.
    with pytest.raises(TableNotFoundError):
        reader.get_table("MSysObjects")
    with pytest.raises(TableNotFoundError):
        reader.count_rows("msysobjects")


@pytest.mark.parametrize("name", [
    "ALLEN BRADLEY", "ALLEN-BRADLEY", "_PINLIST_BRUNDY/HUBBEL", "KRAUS & NAIMER", "DO",
])
def test_hostile_table_names_present(reader, name):
    assert name in reader.list_tables()
    info = reader.get_table(name)
    assert info.name == name
    assert reader.count_rows(name) == 2


def test_hostile_column_names(reader):
    info = reader.get_table("ALLEN BRADLEY")
    assert info.column_names == tuple(MANIFEST["ALLEN BRADLEY"]["columns"])
    rows = _all_rows(reader, "ALLEN BRADLEY")
    assert rows == MANIFEST["ALLEN BRADLEY"]["rows"]


def test_hostile_table_rows(reader):
    for name in ("ALLEN-BRADLEY", "KRAUS & NAIMER", "DO", "_PINLIST_BRUNDY/HUBBEL"):
        assert _all_rows(reader, name) == MANIFEST[name]["rows"], name


# ------------------------------------------------------------------ get_table

def test_get_table_returns_tableinfo(reader):
    info = reader.get_table("COMP")
    assert isinstance(info, TableInfo)
    assert info.name == "COMP"


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_columns_match_manifest(reader, name):
    info = reader.get_table(name)
    assert list(info.column_names) == MANIFEST[name]["columns"]
    assert [c.ordinal for c in info.columns] == list(range(1, len(info.columns) + 1))


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_primary_key_matches_manifest(reader, name):
    info = reader.get_table(name)
    assert info.primary_key == MANIFEST[name]["pk"]
    assert info.primary_key_source == MANIFEST[name]["pk_source"]


def test_pk_discovered_declared_single(reader):
    info = reader.get_table("COMP")
    assert (info.primary_key, info.primary_key_source) == (("HDL",), "discovered")
    pk_idx = [i for i in info.indexes if i.name == "PrimaryKey"]
    assert len(pk_idx) == 1 and pk_idx[0].unique and pk_idx[0].columns == ("HDL",)


def test_pk_discovered_composite_non_counter_key_order(reader):
    info = reader.get_table("TERMS")
    assert info.primary_key == ("HDL", "TERM")
    assert info.primary_key_source == "discovered"


def test_pk_discovered_on_counter_column(reader):
    info = reader.get_table("TYPED")
    assert info.primary_key == ("ID",)
    assert info.primary_key_source == "discovered"


def test_pk_inferred_from_counter(reader):
    for name in ("WIRENO", "WFRM2ALL", "NETS", "XX", "_PINLIST_XX"):
        info = reader.get_table(name)
        assert info.primary_key == ("RECNUM",), name
        assert info.primary_key_source == "inferred", name
        assert info.column("RECNUM").autoincrement
        assert info.column("RECNUM").access_type == "COUNTER"


def test_pk_none(reader):
    info = reader.get_table("PNLCOMP")
    assert info.primary_key == ()
    assert info.primary_key_source == "none"
    assert not any(c.autoincrement for c in info.columns)


def test_index_non_unique_composite(reader):
    for name in ("XX", "_PINLIST_XX"):
        info = reader.get_table(name)
        idx = {i.name: i for i in info.indexes}
        assert "IDX_CAT" in idx, name
        assert idx["IDX_CAT"].columns == ("CATALOG", "ASSEMBLYCODE")
        assert idx["IDX_CAT"].unique is False


def test_no_indexes_when_none_declared(reader):
    assert reader.get_table("PNLCOMP").indexes == ()


def test_varchar_size_and_nullability(reader):
    info = reader.get_table("COMP")
    hdl = info.column("HDL")
    # NOTE: the Access ODBC driver reports NOT NULL columns as nullable (SQLColumns), only
    # COUNTER is reported NOT NULL, so nullability of HDL is deliberately not asserted.
    assert hdl.access_type == "VARCHAR" and hdl.size == 20
    tag = info.column("TAGNAME")
    assert tag.size == 60 and tag.nullable is True and tag.autoincrement is False


def test_memo_type(reader):
    assert reader.get_table("XX").column("NOTES").access_type == "LONGCHAR"


def test_counter_not_nullable(reader):
    assert reader.get_table("WIRENO").column("RECNUM").nullable is False


def test_typed_access_types(reader):
    info = reader.get_table("TYPED")
    got = {c.name: c.access_type for c in info.columns}
    assert got == MANIFEST["TYPED"]["access_types"]


def test_typed_decimal_precision_scale(reader):
    c = reader.get_table("TYPED").column("DEC")
    assert (c.size, c.decimal_digits) == (18, 4)


def test_typed_data_type_is_int(reader):
    for c in reader.get_table("TYPED").columns:
        assert isinstance(c.data_type, int)


def test_case_insensitive_lookup(reader):
    assert reader.get_table("comp").name == "COMP"
    assert reader.get_table("allen bradley").name == "ALLEN BRADLEY"
    assert reader.count_rows("wireno") == 3
    assert _all_rows(reader, "nets") == MANIFEST["NETS"]["rows"]


def test_unknown_table(reader):
    with pytest.raises(TableNotFoundError):
        reader.get_table("NOPE")
    with pytest.raises(TableNotFoundError):
        reader.count_rows("NOPE")
    with pytest.raises(TableNotFoundError):
        list(reader.iter_rows("NOPE"))


# ---------------------------------------------------------------- count / iter

@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_count_rows(reader, name):
    assert reader.count_rows(name) == MANIFEST[name]["count"]


@pytest.mark.parametrize("batch", [1, 7, 5000, 100_000])
def test_iter_rows_complete_and_ordered_across_batches(reader, batch):
    batches = list(reader.iter_rows("BIG", batch_size=batch))
    assert all(isinstance(b, list) for b in batches)
    assert all(type(r) is tuple for b in batches for r in b)
    assert all(1 <= len(b) <= batch for b in batches)
    # every batch but the last is full
    assert all(len(b) == batch for b in batches[:-1])
    rows = [r for b in batches for r in b]
    assert len(rows) == BIG_ROWS == reader.count_rows("BIG")
    assert [r[0] for r in rows] == list(range(1, BIG_ROWS + 1))
    for i in (1, 2, 6, 7, 8, 4999, 5000, 5001, 10000, 10001, BIG_ROWS - 1, BIG_ROWS):
        assert rows[i - 1] == (i,) + big_row(i), i


def test_iter_rows_batch_boundaries_default(reader):
    sizes = [len(b) for b in reader.iter_rows("BIG")]
    assert sizes == [5000, 5000, BIG_ROWS - 10000]


def test_iter_rows_exact_multiple_has_no_empty_batch(reader):
    # COMP has 5 rows: batch_size 5 -> one batch, no trailing empty batch
    assert [len(b) for b in reader.iter_rows("COMP", batch_size=5)] == [5]
    assert [len(b) for b in reader.iter_rows("COMP", batch_size=2)] == [2, 2, 1]


def test_iter_rows_empty_table_yields_nothing(reader):
    assert list(reader.iter_rows("EMPTYFAM")) == []
    assert reader.count_rows("EMPTYFAM") == 0
    assert reader.get_table("EMPTYFAM").column_names == ("CATALOG", "RECNUM")


def test_iter_rows_is_lazy_iterator(reader):
    it = reader.iter_rows("BIG", batch_size=10)
    assert iter(it) is it
    assert len(next(it)) == 10


@pytest.mark.parametrize("bad", [0, -1])
def test_iter_rows_bad_batch_size(reader, bad):
    with pytest.raises(ValueError):
        list(reader.iter_rows("COMP", batch_size=bad))


@pytest.mark.parametrize("name", [
    "COMP", "PNLCOMP", "TERMS", "WIRENO", "WFRM2ALL", "NETS", "XX", "_PINLIST_XX",
])
def test_manifest_rows_exact(reader, name):
    assert _all_rows(reader, name) == MANIFEST[name]["rows"]


def test_row_column_order_follows_ordinal(reader):
    # XX has RECNUM last, _PINLIST_XX has it first
    assert _all_rows(reader, "XX")[0][-1] == 1
    assert _all_rows(reader, "_PINLIST_XX")[0][0] == 1


# --------------------------------------------------------------- value contract

def _xx(reader):
    return {r[0]: r for r in _all_rows(reader, "XX")}


def test_null_vs_empty_string_preserved(reader):
    rows = _xx(reader)
    assert rows["P-005"][2] == "" and rows["P-005"][3] == "" and rows["P-005"][4] == ""
    assert rows["P-006"][2] is None and rows["P-006"][3] is None and rows["P-006"][4] is None
    comp = _all_rows(reader, "COMP")
    assert comp[0][5] is None and comp[0][6] == ""


def test_padding_preserved(reader):
    r = _xx(reader)["P-000"]
    assert r[2] == "  ABC  " and r[3] == "  ABC  " and r[4] == "  ABC  "
    typed = _all_rows(reader, "TYPED")
    assert typed[3][-1] == " pad "


def test_unicode_preserved(reader):
    r = _xx(reader)
    assert r["P-001"][2] == "µ · – Ω °"
    assert r["P-002"][2] == "日本語テキスト"
    assert r["P-003"][2] == "emoji \U0001F600 ok"
    assert _all_rows(reader, "TYPED")[0][-1] == "µΩ°"


def test_crlf_preserved(reader):
    r = _xx(reader)["P-004"]
    assert r[2] == "line1\r\nline2" and r[4] == "line1\r\nline2"


def test_long_memo_preserved(reader):
    from tests.fixtures.build_access import LONG_MEMO

    notes = _xx(reader)["P-007"][4]
    assert len(notes) == len(LONG_MEMO) > 6000
    assert notes == LONG_MEMO


def test_null_in_untouched_column(reader):
    assert _xx(reader)["P-010"][4] is None


def test_typed_rows_exact(reader):
    rows = _all_rows(reader, "TYPED")
    assert len(rows) == len(TYPED_ROWS)
    for got, exp in zip(rows, MANIFEST["TYPED"]["rows"], strict=True):
        assert got == exp


def test_typed_python_types(reader):
    cols = reader.get_table("TYPED").column_names
    rows = [dict(zip(cols, r, strict=True)) for r in _all_rows(reader, "TYPED")]
    r1 = rows[0]
    assert type(r1["DT"]) is dt.datetime
    assert type(r1["YN"]) is bool and r1["YN"] is True
    assert type(r1["CUR"]) is decimal.Decimal and r1["CUR"] == D("1234.5678")
    assert type(r1["DEC"]) is decimal.Decimal and r1["DEC"] == D("12345678901234.1234")
    assert type(r1["BIN"]) is bytes and r1["BIN"] == b"\x00\x01\xff"
    assert type(r1["DBL"]) is float and r1["DBL"] == 1.5e300
    assert type(r1["L"]) is int and type(r1["S"]) is int and type(r1["B"]) is int
    assert type(r1["TXT"]) is str


def test_bit_true_false_null(reader):
    cols = reader.get_table("TYPED").column_names
    i = cols.index("YN")
    vals = [r[i] for r in _all_rows(reader, "TYPED")]
    assert vals == [True, False, False, True]  # YESNO cannot hold NULL
    assert vals[0] is True and vals[1] is False


def test_datetime_extremes_and_time_only(reader):
    cols = reader.get_table("TYPED").column_names
    i = cols.index("DT")
    vals = [r[i] for r in _all_rows(reader, "TYPED")]
    assert vals[0] == dt.datetime(1900, 1, 1)
    assert vals[1] == dt.datetime(2099, 12, 31, 23, 59, 59)
    assert vals[2] == dt.datetime(1899, 12, 30, 13, 45, 30)  # time-only value
    assert vals[3] is None


def test_binary_with_nul_and_empty(reader):
    cols = reader.get_table("TYPED").column_names
    i = cols.index("BIN")
    vals = [r[i] for r in _all_rows(reader, "TYPED")]
    assert vals[0] == b"\x00\x01\xff"
    assert vals[1] == b"\x00"
    assert vals[2] is None
    assert vals[3] in (b"", None)  # Access may normalise empty OLE to NULL


def test_guid_value(reader):
    cols = reader.get_table("TYPED").column_names
    i = cols.index("G")
    vals = [r[i] for r in _all_rows(reader, "TYPED")]
    assert str(vals[0]).strip("{}").upper() == "12345678-1234-1234-1234-123456789ABC"
    assert vals[2] is None


# -------------------------------------------------------------- source handling

def test_missing_file(tmp_path):
    with pytest.raises(SourceNotFoundError):
        open_reader(tmp_path / "nope.mdb")
    with pytest.raises(SourceNotFoundError):
        PyodbcReader(tmp_path / "nope.accdb")


def test_wrong_suffix(tmp_path):
    p = tmp_path / "data.txt"
    p.write_text("x")
    with pytest.raises(SourceNotFoundError):
        open_reader(p)


def test_directory_is_not_a_source(tmp_path):
    with pytest.raises(SourceNotFoundError):
        open_reader(tmp_path)


def test_context_manager_and_idempotent_close(access_db):
    r = open_reader(access_db)
    assert Path(r.path).name == access_db.name
    with r as same:
        assert same is r
        assert r.count_rows("COMP") == 5
    r.close()
    r.close()  # idempotent


def test_explicit_close_idempotent(access_db):
    r = open_reader(access_db)
    r.close()
    r.close()


def test_use_after_close_fails_cleanly(access_db):
    r = open_reader(access_db)
    r.close()
    with pytest.raises(Exception) as ei:
        r.count_rows("COMP")
    assert not isinstance(ei.value, (AttributeError, TypeError))


def _lock_files(path: Path):
    return [p for p in path.parent.iterdir() if p.suffix.lower() in (".ldb", ".laccdb")]


def test_read_only_file_unchanged_and_no_lock_left(access_db):
    before = access_db.stat()
    with open_reader(access_db) as r:
        for t in r.list_tables():
            r.get_table(t)
            _all_rows(r, t)
    after = access_db.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert _lock_files(access_db) == []


def test_writing_through_reader_connection_is_impossible(access_db, tmp_path):
    copy = tmp_path / access_db.name
    shutil.copy2(access_db, copy)
    before = copy.stat()
    with open_reader(copy) as r:
        conn = getattr(r, "_conn", None)
        assert conn is not None, "PyodbcReader no longer exposes _conn; update this test"
        with pytest.raises(pyodbc.Error):
            conn.cursor().execute("INSERT INTO NETS (NETNAME, SHEET) VALUES ('X', '9')")
        with pytest.raises(pyodbc.Error):
            conn.cursor().execute("DROP TABLE NETS")
    after = copy.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    with open_reader(copy) as r:
        assert r.count_rows("NETS") == 2
        assert "NETS" in r.list_tables()


def test_copy_to_temp(access_db):
    before = access_db.stat()
    with open_reader(access_db, copy_to_temp=True) as r:
        assert r.count_rows("BIG") == BIG_ROWS
        assert _all_rows(r, "TERMS") == MANIFEST["TERMS"]["rows"]
        # source must not even be opened -> no lock file next to it
        assert _lock_files(access_db) == []
    after = access_db.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert _lock_files(access_db) == []


def test_copy_to_temp_cleans_up(access_db):
    import tempfile

    tmp = Path(tempfile.gettempdir())
    before = {p.name for p in tmp.glob("acade2sql-*")}
    r = open_reader(access_db, copy_to_temp=True)
    r.close()
    after = {p.name for p in tmp.glob("acade2sql-*")}
    assert after <= before


def test_two_readers_concurrently(access_db):
    with open_reader(access_db) as a, open_reader(access_db) as b:
        assert a.count_rows("COMP") == b.count_rows("COMP") == 5
