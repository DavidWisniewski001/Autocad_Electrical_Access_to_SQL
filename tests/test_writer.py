"""Unit tests for acade2sql.writers.sqlalchemy_writer (spec M3 section 2); no driver needed."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from acade2sql.errors import Acade2SqlError
from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo

sw = pytest.importorskip("acade2sql.writers.sqlalchemy_writer")
build_table = sw.build_table
SqlAlchemyWriter = sw.SqlAlchemyWriter

DIALECTS = {"sqlite": sqlite.dialect, "mssql": mssql.dialect, "postgresql": postgresql.dialect}
HOSTILE_TABLES = [
    "ALLEN BRADLEY", "ALLEN-BRADLEY", "KRAUS & NAIMER", "DO", "Order", "DESC", "RANGE",
    "_PINLIST_BRUNDY/HUBBEL", 'we"ird', "it's", "MiXeD Case", "[br]acket", "µΩ°",
]  # fmt: skip
HOSTILE_COLUMNS = [
    "DESC",
    "RANGE",
    "DO",
    "DUCT SIZE W x H",
    "COIL/VOLTAGE/STYLE",
    "MiXed",
    'q"uote',
]


def prep(w, infos, **kw):
    """prepare() no longer runs DDL (review gate 1): follow it with create_table per table."""
    w.prepare(infos, **kw)
    for info in infos:
        w.create_table(info)


def column(name, ordinal, access_type="VARCHAR", size=30, digits=None, nullable=True, auto=False):
    return ColumnInfo(
        name=name, ordinal=ordinal, access_type=access_type, data_type=0, size=size,
        decimal_digits=digits, nullable=nullable, autoincrement=auto,
    )  # fmt: skip


def hostile_info(table: str) -> TableInfo:
    cols = [column("RECNUM", 1, "COUNTER", None, nullable=False, auto=True)]
    cols += [column(n, i, "VARCHAR", 20) for i, n in enumerate(HOSTILE_COLUMNS, 2)]
    return TableInfo(
        name=table,
        columns=tuple(cols),
        primary_key=("RECNUM",),
        primary_key_source="inferred",
        indexes=(IndexInfo("IDX_CAT", ("DESC", "RANGE"), False),),
    )


def typed_info(name="TYPED") -> TableInfo:
    spec = [
        ("ID", "COUNTER", None, None), ("L", "INTEGER", None, None), ("S", "SMALLINT", None, None),
        ("B", "BYTE", None, None), ("DBL", "DOUBLE", None, None), ("RL", "REAL", None, None),
        ("CUR", "CURRENCY", None, None), ("DT", "DATETIME", None, None), ("YN", "BIT", None, None),
        ("G", "GUID", None, None), ("DEC", "DECIMAL", 18, 4), ("BIN", "LONGBINARY", None, None),
        ("TXT", "VARCHAR", 20, None), ("MEMO", "LONGCHAR", 1073741823, None),
    ]  # fmt: skip
    cols = tuple(
        column(n, i, t, s, d, nullable=(n != "ID"), auto=(t == "COUNTER"))
        for i, (n, t, s, d) in enumerate(spec, 1)
    )
    return TableInfo(name, cols, ("ID",), "discovered", ())


TYPED_ROWS = [
    (1, 2147483647, -32768, 0, 1.5e300, 3.5, Decimal("1234.5678"), dt.datetime(1900, 1, 1),
     True, "12345678-1234-1234-1234-123456789ABC", Decimal("12345678901234.1234"),
     b"\x00\x01\xff", "µΩ°", "x" * 6400),
    (2, -5, 32767, 255, -2.5e-300, -0.25, Decimal("-922337203685477.5808"),
     dt.datetime(2099, 12, 31, 23, 59, 59), False, "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
     Decimal("-0.0001"), b"\x00", "", "line1\r\nline2 \U0001F600 日本語"),
    (3, None, None, None, None, None, None, None, None, None, None, None, None, None),
    (4, 0, 0, 0, 0.0, 0.0, Decimal("0"), dt.datetime(1899, 12, 30, 13, 45, 30, 123000),
     False, None, Decimal("0"), b"", " pad ", ""),
]  # fmt: skip


def simple_info(name, *, pk=("ID",), indexes=()):
    cols = (
        column("ID", 1, "COUNTER", None, nullable=False, auto=True),
        column("V", 2, "VARCHAR", 30),
    )
    return TableInfo(name, cols, pk, "inferred" if pk else "none", tuple(indexes))


# ---- build_table -------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("tname", HOSTILE_TABLES)
def test_build_table_quotes_hostile_names(dialect, tname):
    d = DIALECTS[dialect]()
    info = hostile_info(tname)
    md = sa.MetaData()
    t = build_table(md, info, dialect, used_index_names=set())
    prep = d.identifier_preparer
    ddl = str(CreateTable(t).compile(dialect=d))
    assert prep.quote_identifier(tname) in ddl
    for c in info.column_names:
        assert prep.quote_identifier(c) in ddl, c
    assert t.name == tname
    assert [c.name for c in t.columns] == list(info.column_names)
    for ix in t.indexes:
        text = str(CreateIndex(ix).compile(dialect=d))
        assert prep.quote_identifier(tname) in text


@pytest.mark.parametrize("dialect", DIALECTS)
def test_reserved_words_quoted(dialect):
    d = DIALECTS[dialect]()
    info = TableInfo(
        "DESC",
        (
            column("DESC", 1, "VARCHAR", 5),
            column("RANGE", 2, "VARCHAR", 5),
            column("DO", 3, "VARCHAR", 5),
        ),
        (),
        "none",
        (),
    )
    t = build_table(sa.MetaData(), info, dialect, used_index_names=set())
    ddl = str(CreateTable(t).compile(dialect=d))
    q = d.identifier_preparer.quote_identifier
    for n in ("DESC", "RANGE", "DO"):
        assert q(n) in ddl
    # case is preserved too (no folding to lower case)
    t2 = build_table(
        sa.MetaData(),
        TableInfo("MiXeD", (column("CoL", 1, "VARCHAR", 5),), (), "none", ()),
        dialect,
        used_index_names=set(),
    )
    ddl2 = str(CreateTable(t2).compile(dialect=d))
    assert q("MiXeD") in ddl2 and q("CoL") in ddl2


def test_columns_nullability_and_order():
    info = TableInfo(
        "T",
        (
            column("B", 2, "VARCHAR", 5, nullable=True),
            column("A", 1, "VARCHAR", 5, nullable=False),
        ),
        (),
        "none",
        (),
    )
    t = build_table(sa.MetaData(), info, "sqlite", used_index_names=set())
    # columns are taken in the order of TableInfo.columns (already ordinal-ordered by readers)
    assert {c.name: c.nullable for c in t.columns} == {"A": False, "B": True}


def test_pk_constraint_and_autoincrement_off():
    info = TableInfo(
        "T",
        (
            column("ID", 1, "COUNTER", None, nullable=True, auto=True),
            column("K2", 2, "VARCHAR", 10, nullable=True),
            column("V", 3, "VARCHAR", 10),
        ),
        ("ID", "K2"),
        "discovered",
        (),
    )
    t = build_table(sa.MetaData(), info, "sqlite", used_index_names=set())
    assert [c.name for c in t.primary_key.columns] == ["ID", "K2"]
    assert t.c.ID.nullable is False and t.c.K2.nullable is False  # PK members are NOT NULL
    assert t.c.ID.autoincrement is False
    assert "AUTOINCREMENT" not in str(CreateTable(t).compile(dialect=sqlite.dialect())).upper()
    assert "IDENTITY" not in str(CreateTable(t).compile(dialect=mssql.dialect())).upper()
    assert "SERIAL" not in str(CreateTable(t).compile(dialect=postgresql.dialect())).upper()


def test_no_pk_has_empty_pk():
    t = build_table(sa.MetaData(), simple_info("NOPK", pk=()), "sqlite", used_index_names=set())
    assert list(t.primary_key.columns) == []


def test_pk_index_not_duplicated():
    info = simple_info(
        "T", indexes=[IndexInfo("PrimaryKey", ("ID",), True), IndexInfo("IX_V", ("V",), False)]
    )
    t = build_table(sa.MetaData(), info, "sqlite", used_index_names=set())
    assert len(t.indexes) == 1
    (ix,) = t.indexes
    assert ix.info["source_name"] == "IX_V"
    assert [c.name for c in ix.columns] == ["V"]


def test_unique_index_stays_unique_and_column_order():
    info = TableInfo(
        "T",
        (
            column("ID", 1, "COUNTER", None, nullable=False, auto=True),
            column("A", 2),
            column("B", 3),
        ),
        ("ID",),
        "inferred",
        (IndexInfo("U", ("B", "A"), True),),
    )
    t = build_table(sa.MetaData(), info, "sqlite", used_index_names=set())
    (ix,) = t.indexes
    assert ix.unique is True
    assert [c.name for c in ix.columns] == ["B", "A"]


def test_index_names_unique_across_tables_with_same_source_name():
    used: set[str] = set()
    md = sa.MetaData()
    tables = []
    for name in ("XX", "_PINLIST_XX", "XX2"):
        info = simple_info(name, indexes=[IndexInfo("IDX_CAT", ("V",), False)])
        tables.append(build_table(md, info, "sqlite", used_index_names=used))
    names = [ix.name for t in tables for ix in t.indexes]
    assert len(names) == 3 and len(set(names)) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", n) and len(n) <= 60 for n in names)
    assert all(ix.info["source_name"] == "IDX_CAT" for t in tables for ix in t.indexes)
    assert set(names) <= used


def test_same_table_and_index_name_twice_still_unique():
    """The same names offered twice must not collide through used_index_names."""
    used: set[str] = set()
    info = simple_info("XX", indexes=[IndexInfo("IDX_CAT", ("V",), False)])
    a = build_table(sa.MetaData(), info, "sqlite", used_index_names=used)
    b = build_table(sa.MetaData(), info, "sqlite", used_index_names=used)
    assert next(iter(a.indexes)).name != next(iter(b.indexes)).name


def test_index_name_pattern():
    info = simple_info("My Table-1", indexes=[IndexInfo("idx cat/1", ("V",), False)])
    (ix,) = build_table(sa.MetaData(), info, "sqlite", used_index_names=set()).indexes
    assert re.fullmatch(r"[A-Za-z0-9_]+", ix.name)
    assert ix.name.startswith("ix_")
    assert ix.name == "ix_My_Table_1_idx_cat_1"


def test_long_index_names_truncated_with_hash():
    used: set[str] = set()
    md = sa.MetaData()
    long_a = "T" * 80
    infos = [
        simple_info(long_a, indexes=[IndexInfo("I" * 80, ("V",), False)]),
        simple_info(long_a + "x", indexes=[IndexInfo("I" * 80, ("V",), False)]),
        simple_info("T" * 70, indexes=[IndexInfo("J" * 70, ("V",), False)]),
    ]
    names = []
    for info in infos:
        (ix,) = build_table(md, info, "sqlite", used_index_names=used).indexes
        names.append(ix.name)
        assert len(ix.name) <= 60
        assert re.fullmatch(r"[A-Za-z0-9_]+", ix.name)
        assert re.search(r"[0-9a-f]{8}$", ix.name), ix.name
        assert ix.info["source_name"] == info.indexes[0].name
    assert len(set(names)) == 3


def test_build_table_does_not_modify_other_dialect_defaults():
    # smoke: all three dialects accept every type, via the typemap
    info = typed_info()
    for dialect, d in DIALECTS.items():
        t = build_table(sa.MetaData(), info, dialect, used_index_names=set())
        text = str(CreateTable(t).compile(dialect=d()))
        assert "1073741823" not in text


# ---- SqlAlchemyWriter on SQLite ---------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "out.sqlite"


@pytest.fixture
def url(db_path):
    return "sqlite:///" + db_path.as_posix()


@pytest.fixture
def writer(url):
    with SqlAlchemyWriter(url) as w:
        yield w


def sqlite_rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_writer_dialect(writer):
    assert writer.dialect == "sqlite"


def test_roundtrip_typed(writer):
    info = typed_info()
    prep(writer, [info])
    n = writer.write_batches(info, iter([TYPED_ROWS[:2], TYPED_ROWS[2:]]))
    assert n == 4
    assert writer.count_rows("TYPED") == 4
    got = [r for b in writer.read_rows("TYPED", batch_size=3) for r in b]
    assert [r[0] for r in got] == [1, 2, 3, 4]  # ordered by PK
    for g, e in zip(got, TYPED_ROWS, strict=True):
        assert len(g) == len(e)
        for gv, ev, c in zip(g, e, info.columns, strict=True):
            assert type(gv) is type(ev), (c.name, gv, ev)
            assert gv == ev, (c.name, gv, ev)


def test_read_rows_batches_and_order(writer):
    info = simple_info("T")
    prep(writer, [info])
    rows = [(i, f"v{i}") for i in range(1, 26)]
    writer.write_batches(info, [rows[10:], rows[:10]])
    batches = list(writer.read_rows("T", batch_size=7))
    assert [len(b) for b in batches] == [7, 7, 7, 4]
    assert [r for b in batches for r in b] == rows


def test_empty_table(writer):
    info = simple_info("EMPTY")
    prep(writer, [info])
    assert writer.write_batches(info, iter([])) == 0
    assert writer.count_rows("EMPTY") == 0
    assert list(writer.read_rows("EMPTY")) == [] or all(not b for b in writer.read_rows("EMPTY"))


def test_null_vs_empty_string_and_padding(writer):
    info = simple_info("S")
    prep(writer, [info])
    writer.write_batches(info, [[(1, None), (2, ""), (3, " a "), (4, "a")]])
    got = {r[0]: r[1] for b in writer.read_rows("S") for r in b}
    assert got == {1: None, 2: "", 3: " a ", 4: "a"}


def test_no_pk_table_keeps_duplicates(writer):
    info = TableInfo("NOPK", (column("A", 1, "INTEGER", None), column("B", 2)), (), "none", ())
    prep(writer, [info])
    rows = [(1, "a"), (1, "a"), (None, None)]
    writer.write_batches(info, [rows])
    assert writer.count_rows("NOPK") == 3
    got = sorted((r for b in writer.read_rows("NOPK") for r in b), key=repr)
    assert got == sorted(rows, key=repr)


def test_hostile_names_roundtrip(writer):
    infos = [hostile_info(n) for n in ("ALLEN BRADLEY", "Order", "DO", 'we"ird', "it's")]
    prep(writer, infos)
    for info in infos:
        row = (1, *(f"v{i}" for i in range(len(HOSTILE_COLUMNS))))
        writer.write_batches(info, [[row]])
        assert writer.count_rows(info.name) == 1
        assert [r for b in writer.read_rows(info.name) for r in b] == [row]


def test_pk_and_indexes_in_sqlite_file(writer, db_path):
    a = simple_info("XX", indexes=[IndexInfo("IDX_CAT", ("V",), False)])
    b = simple_info("YY", indexes=[IndexInfo("IDX_CAT", ("V",), True)])
    c = TableInfo(
        "COMPOSITE",
        (
            column("A", 1, "VARCHAR", 5, nullable=False),
            column("B", 2, "VARCHAR", 5, nullable=False),
        ),
        ("A", "B"),
        "discovered",
        (),
    )
    prep(writer, [a, b, c])
    writer.close()
    cols = sqlite_rows(db_path, 'PRAGMA table_info("XX")')  # cid, name, type, notnull, dflt, pk
    assert [r[1] for r in cols] == ["ID", "V"]
    assert [r[5] for r in cols] == [1, 0]
    comp = sqlite_rows(db_path, 'PRAGMA table_info("COMPOSITE")')
    assert sorted((r[1], r[5]) for r in comp) == [("A", 1), ("B", 2)]
    all_ix = {}
    for t in ("XX", "YY"):
        for r in sqlite_rows(
            db_path, f'PRAGMA index_list("{t}")'
        ):  # seq, name, unique, origin, partial
            if r[3] == "c":
                all_ix[r[1]] = (t, bool(r[2]))
    assert len(all_ix) == 2  # two created indexes, distinct database-wide names
    assert sorted(v for v in all_ix.values()) == [("XX", False), ("YY", True)]
    names = [
        r[0]
        for r in sqlite_rows(
            db_path, "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    ]
    assert len(names) == len(set(names)) == 2


def test_pk_not_autoincrement_in_file(writer, db_path):
    info = simple_info("T")
    prep(writer, [info])
    writer.write_batches(info, [[(10, "a"), (20, "b")]])  # source values inserted as-is
    writer.close()
    assert sqlite_rows(db_path, "SELECT ID FROM T ORDER BY ID") == [(10,), (20,)]
    sql = sqlite_rows(db_path, "SELECT sql FROM sqlite_master WHERE name='T'")[0][0]
    assert "AUTOINCREMENT" not in sql.upper()


def test_sqlite_pragmas(writer, db_path):
    info = simple_info("T")
    prep(writer, [info])
    writer.write_batches(info, [[(1, "a")]])
    writer.close()
    assert sqlite_rows(db_path, "PRAGMA journal_mode")[0][0].lower() == "wal"


def test_reprepare_full_mode_drops_and_recreates(writer):
    info = simple_info("T", indexes=[IndexInfo("IX", ("V",), False)])
    prep(writer, [info], mode="full")
    writer.write_batches(info, [[(1, "a"), (2, "b")]])
    assert writer.count_rows("T") == 2
    prep(writer, [info], mode="full")
    assert writer.count_rows("T") == 0
    writer.write_batches(info, [[(1, "a"), (2, "b")]])
    assert writer.count_rows("T") == 2


def test_reprepare_with_new_writer_instance(url, db_path):
    info = simple_info("T", indexes=[IndexInfo("IX", ("V",), False)])
    for _ in range(2):
        with SqlAlchemyWriter(url) as w:
            prep(w, [info])
            w.write_batches(info, [[(1, "a"), (2, "b")]])
            assert w.count_rows("T") == 2
    assert sqlite_rows(db_path, "SELECT COUNT(*) FROM T") == [(2,)]


def test_unrelated_table_survives(url, db_path):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE keepme (a INTEGER, b TEXT)")
    con.execute("INSERT INTO keepme VALUES (1, 'x')")
    con.commit()
    con.close()
    info = simple_info("T")
    with SqlAlchemyWriter(url) as w:
        prep(w, [info])
        w.write_batches(info, [[(1, "a")]])
        prep(w, [info])
    assert sqlite_rows(db_path, "SELECT a, b FROM keepme") == [(1, "x")]


def test_preexisting_same_name_table_replaced(url, db_path):
    con = sqlite3.connect(db_path)
    con.execute('CREATE TABLE "T" (junk TEXT)')
    con.execute("INSERT INTO T VALUES ('x')")
    con.commit()
    con.close()
    info = simple_info("T")
    with SqlAlchemyWriter(url) as w:
        prep(w, [info])
        assert w.count_rows("T") == 0
        w.write_batches(info, [[(1, "a")]])
        assert w.count_rows("T") == 1


def test_duplicate_pk_raises_write_error_and_rolls_back(writer):
    WriteError = pytest.importorskip("acade2sql.errors").WriteError
    info = simple_info("T")
    prep(writer, [info])
    batches = [[(1, "a"), (2, "b")], [(3, "c"), (2, "dup")]]
    with pytest.raises(WriteError) as ei:
        writer.write_batches(info, iter(batches))
    assert isinstance(ei.value, Acade2SqlError)
    assert ei.value.table == "T"
    assert ei.value.cause is not None
    assert writer.count_rows("T") == 0


def test_wrong_arity_raises_write_error_and_rolls_back(writer):
    WriteError = pytest.importorskip("acade2sql.errors").WriteError
    info = simple_info("T")
    prep(writer, [info])
    with pytest.raises(WriteError):
        writer.write_batches(info, [[(1, "a")], [(2, "b", "extra")]])
    assert writer.count_rows("T") == 0


def test_writer_usable_after_failure(writer):
    WriteError = pytest.importorskip("acade2sql.errors").WriteError
    bad, good = simple_info("BAD"), simple_info("GOOD")
    prep(writer, [bad, good])
    with pytest.raises(WriteError):
        writer.write_batches(bad, [[(1, "a"), (1, "b")]])
    assert writer.write_batches(good, [[(1, "a")]]) == 1
    assert writer.count_rows("GOOD") == 1 and writer.count_rows("BAD") == 0


def test_not_null_violation_raises_write_error(writer):
    WriteError = pytest.importorskip("acade2sql.errors").WriteError
    info = TableInfo(
        "T",
        (column("ID", 1, "COUNTER", None, nullable=False), column("V", 2, nullable=False)),
        ("ID",),
        "inferred",
        (),
    )
    prep(writer, [info])
    with pytest.raises(WriteError):
        writer.write_batches(info, [[(1, None)]])
    assert writer.count_rows("T") == 0


def test_sql_injection_like_values_are_bound(writer):
    info = simple_info("T")
    prep(writer, [info])
    evil = "x'); DROP TABLE T; --"
    writer.write_batches(info, [[(1, evil), (2, ":name"), (3, "%s")]])
    assert [r[1] for b in writer.read_rows("T") for r in b] == [evil, ":name", "%s"]


def test_close_is_idempotent_and_context_manager(url):
    w = SqlAlchemyWriter(url)
    w.close()
    w.close()
    with SqlAlchemyWriter(url) as w2:
        assert w2.dialect == "sqlite"


def test_write_error_attributes():
    WriteError = pytest.importorskip("acade2sql.errors").WriteError
    cause = RuntimeError("boom")
    e = WriteError("TBL", cause)
    assert e.table == "TBL" and e.cause is cause
    assert isinstance(e, Acade2SqlError)
    assert "TBL" in str(e)
