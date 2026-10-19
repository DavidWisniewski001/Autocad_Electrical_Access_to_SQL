"""Unit tests for acade2sql.typemap (spec M3 section 1). No Access driver needed."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql, sqlite
from sqlalchemy.schema import CreateTable
from sqlalchemy.types import TypeEngine

from acade2sql.schema import ColumnInfo

typemap = pytest.importorskip("acade2sql.typemap")

ALIASES = {
    "TEXT": "VARCHAR", "CHAR": "VARCHAR", "MEMO": "LONGCHAR", "LONGTEXT": "LONGCHAR",
    "LONG": "INTEGER", "YESNO": "BIT", "AUTOINCREMENT": "COUNTER", "NUMERIC": "DECIMAL",
    "MONEY": "CURRENCY", "FLOAT": "DOUBLE", "UNIQUEIDENTIFIER": "GUID",
    "REPLICATIONID": "GUID", "VARBINARY": "LONGBINARY", "BINARY": "LONGBINARY",
    "IMAGE": "LONGBINARY", "OLE": "LONGBINARY",
}  # fmt: skip

CANONICAL = [
    "VARCHAR", "LONGCHAR", "COUNTER", "INTEGER", "SMALLINT", "BYTE", "REAL", "DOUBLE",
    "CURRENCY", "DECIMAL", "DATETIME", "BIT", "GUID", "LONGBINARY",
]  # fmt: skip

DIALECT_OBJ = {
    "sqlite": sqlite.dialect,
    "mssql": mssql.dialect,
    "postgresql": postgresql.dialect,
}


def col(access_type, size=None, digits=None, name="c", nullable=True):
    return ColumnInfo(
        name=name, ordinal=1, access_type=access_type, data_type=0, size=size,
        decimal_digits=digits, nullable=nullable, autoincrement=False,
    )  # fmt: skip


def default_col(t):
    canon = ALIASES.get(t, t)
    if canon == "VARCHAR":
        return col(t, size=60)
    if canon == "DECIMAL":
        return col(t, size=18, digits=4)
    return col(t)


def ddl(columns, dialect):
    md = sa.MetaData()
    cols = [sa.Column(c.name, typemap.sa_type(c, dialect)) for c in columns]
    t = sa.Table("T", md, *cols)
    return str(CreateTable(t).compile(dialect=DIALECT_OBJ[dialect]()))


def one(access_type, dialect, **kw):
    c = col(access_type, **kw)
    return ddl([c], dialect)


def test_dialects_constant():
    assert tuple(typemap.DIALECTS) == ("sqlite", "mssql", "postgresql")


def test_type_map_has_canonical_types():
    for t in CANONICAL:
        assert t in typemap.TYPE_MAP, t
        assert typemap.is_known(t)


def test_aliases_known():
    for alias in ALIASES:
        assert typemap.is_known(alias), alias


def test_unknown_not_known():
    assert not typemap.is_known("FROBNICATE")


@pytest.mark.parametrize("dialect", ["sqlite", "mssql", "postgresql"])
@pytest.mark.parametrize("t", CANONICAL + list(ALIASES))
def test_every_type_gives_type_engine(t, dialect):
    r = typemap.sa_type(default_col(t), dialect)
    assert isinstance(r, TypeEngine)


@pytest.mark.parametrize("dialect", ["sqlite", "mssql", "postgresql"])
def test_aliases_match_canonical_ddl(dialect):
    for alias, canon in ALIASES.items():
        a = ddl([default_col(alias)], dialect)
        b = ddl([default_col(canon)], dialect)
        assert a == b, (alias, canon, a, b)


@pytest.mark.parametrize("dialect", ["sqlite", "mssql", "postgresql"])
def test_create_table_with_every_type_compiles(dialect):
    cols = [
        ColumnInfo(
            name=f"c_{t}", ordinal=i, access_type=t, data_type=0,
            size=60 if t == "VARCHAR" else (18 if t == "DECIMAL" else None),
            decimal_digits=4 if t == "DECIMAL" else None, nullable=True, autoincrement=False,
        )
        for i, t in enumerate(CANONICAL, 1)
    ]  # fmt: skip
    text = ddl(cols, dialect)
    assert "1073741823" not in text
    for c in cols:
        assert c.name in text


def test_no_1073741823_anywhere():
    for dialect in typemap.DIALECTS:
        for t in CANONICAL:
            if t == "DECIMAL" or (t == "VARCHAR" and dialect != "mssql"):
                continue  # not real Access sizes; spec only fixes VARCHAR > 4000 for mssql
            assert "1073741823" not in ddl([col(t, size=1073741823)], dialect), (t, dialect)


def test_varchar_mssql_nvarchar():
    assert "NVARCHAR(60)" in one("VARCHAR", "mssql", size=60)


def test_varchar_other_dialects():
    assert "VARCHAR(60)" in one("VARCHAR", "sqlite", size=60)
    assert "VARCHAR(60)" in one("VARCHAR", "postgresql", size=60)
    assert "NVARCHAR" not in one("VARCHAR", "postgresql", size=60)


def test_longchar():
    assert "NVARCHAR(max)" in one("LONGCHAR", "mssql", size=1073741823)
    assert "TEXT" in one("LONGCHAR", "sqlite", size=1073741823)
    assert "TEXT" in one("LONGCHAR", "postgresql", size=1073741823)


def test_wide_varchar_becomes_max():
    assert "NVARCHAR(max)" in one("VARCHAR", "mssql", size=5000)
    assert "4001" not in one("VARCHAR", "mssql", size=4001)
    assert "NVARCHAR(4000)" in one("VARCHAR", "mssql", size=4000)
    assert "NVARCHAR(max)" in one("VARCHAR", "mssql", size=1073741823)


def test_currency():
    for d in ("mssql", "postgresql"):
        assert "NUMERIC(19, 4)" in one("CURRENCY", d)


def test_decimal_precision():
    for d in ("mssql", "postgresql"):
        assert "NUMERIC(18, 4)" in one("DECIMAL", d, size=18, digits=4)


def test_guid():
    assert "UNIQUEIDENTIFIER" in one("GUID", "mssql")
    assert "UUID" in one("GUID", "postgresql")
    assert "VARCHAR(36)" in one("GUID", "sqlite")


def test_longbinary():
    assert "VARBINARY(max)" in one("LONGBINARY", "mssql")
    assert "BYTEA" in one("LONGBINARY", "postgresql")
    assert "BLOB" in one("LONGBINARY", "sqlite")


def test_other_scalar_types():
    assert "DATETIME2" in one("DATETIME", "mssql")
    assert "TIMESTAMP" in one("DATETIME", "postgresql")
    assert "BIT" in one("BIT", "mssql")
    assert "BOOLEAN" in one("BIT", "postgresql")
    assert "REAL" in one("REAL", "postgresql")
    assert "DOUBLE PRECISION" in one("DOUBLE", "postgresql")
    # SQL Server: DOUBLE PRECISION is the synonym of FLOAT(53)
    dbl = one("DOUBLE", "mssql")
    assert "FLOAT" in dbl or "DOUBLE PRECISION" in dbl
    for d in typemap.DIALECTS:
        assert "SMALLINT" in one("SMALLINT", d)
        assert "SMALLINT" in one("BYTE", d)
        assert "INTEGER" in one("INTEGER", d)
        assert "INTEGER" in one("COUNTER", d)


def test_unknown_type_text_and_warning(caplog):
    caplog.set_level(logging.WARNING)
    for d in typemap.DIALECTS:
        r = typemap.sa_type(col("FROBNICATE_X"), d)
        assert isinstance(r, TypeEngine)
        compiled = ddl([col("FROBNICATE_X")], d)
        assert "TEXT" in compiled or "max" in compiled, compiled
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert any(
        "FROBNICATE_X" in r.getMessage() or "FROBNICATE_X" in str(r.__dict__) for r in warnings
    )
    # one warning per type (not per call / dialect)
    assert len(warnings) == 1


def test_unknown_dialect():
    with pytest.raises(ValueError):
        typemap.sa_type(col("VARCHAR", size=5), "oracle")


# ---- DecimalText -----------------------------------------------------------


def _decimal_text_type():
    return typemap.sa_type(col("DECIMAL", size=28, digits=10), "sqlite")


def test_decimal_text_cache_ok():
    assert getattr(_decimal_text_type(), "cache_ok", None) is True


def test_decimal_text_roundtrip_sqlite(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'd.sqlite'}")
    md = sa.MetaData()
    t = sa.Table(
        "t", md, sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("v", _decimal_text_type()),
        sa.Column("cur", typemap.sa_type(col("CURRENCY"), "sqlite")),
    )  # fmt: skip
    md.create_all(eng)
    values = [
        Decimal("123456789012345.6789"), Decimal("-0.0001"), Decimal("1E-10"),
        Decimal("0"), Decimal("-922337203685477.5808"), Decimal("1.10"), None,
    ]  # fmt: skip
    with eng.begin() as c:
        c.execute(t.insert(), [{"id": i, "v": v, "cur": v} for i, v in enumerate(values)])
    with eng.connect() as c:
        got = c.execute(sa.select(t.c.v, t.c.cur).order_by(t.c.id)).all()
    for (v, cur), exp in zip(got, values, strict=True):
        if exp is None:
            assert v is None and cur is None
        else:
            assert isinstance(v, Decimal) and isinstance(cur, Decimal)
            assert v == exp and cur == exp
    eng.dispose()
