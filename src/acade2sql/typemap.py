"""Table-driven mapping of Access column types to SQLAlchemy types, per target dialect.

``TYPE_MAP`` is keyed by :attr:`ColumnInfo.access_type` (the upper-case ODBC ``type_name``). Each
entry holds one factory per dialect; :func:`sa_type` is the only lookup function.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import types as sat
from sqlalchemy.dialects import mssql, postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles

from acade2sql.schema import ColumnInfo

log = logging.getLogger(__name__)

DIALECTS = ("sqlite", "mssql", "postgresql")

MSSQL_MAX_NVARCHAR = 4000
DEFAULT_DECIMAL_PRECISION = 18
DEFAULT_DECIMAL_SCALE = 0

Factory = Callable[[ColumnInfo], sat.TypeEngine]


DECIMAL_TEXT_SQLITE = "DECIMAL_TEXT"


class DecimalTextStorage(sat.Text):
    """Storage type of :class:`DecimalText`: ``TEXT``, declared ``DECIMAL_TEXT`` on SQLite.

    The declared name still has TEXT affinity (it contains "TEXT") and lets reflection map the
    column back to :class:`DecimalText` (see ``SqlAlchemyWriter``).
    """


@compiles(DecimalTextStorage, "sqlite")
def _compile_decimal_text_sqlite(_type: DecimalTextStorage, _compiler: Any, **_kw: Any) -> str:
    return DECIMAL_TEXT_SQLITE


class DecimalText(sat.TypeDecorator):
    """Exact decimals on SQLite: stored as canonical TEXT, returned as :class:`decimal.Decimal`.

    SQLite's NUMERIC affinity goes through a double and loses digits; text keeps every digit.
    """

    impl = DecimalTextStorage
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, float):
            return str(Decimal(repr(value)))
        return str(Decimal(str(value)))

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))

    @property
    def python_type(self) -> type:
        return Decimal


@dataclass(frozen=True)
class TypeRule:
    """One factory per dialect, each turning a :class:`ColumnInfo` into a SQLAlchemy type."""

    sqlite: Factory
    mssql: Factory
    postgresql: Factory

    def for_dialect(self, dialect: str) -> Factory:
        return getattr(self, dialect)


def _const(type_: Callable[[], sat.TypeEngine]) -> Factory:
    return lambda _col: type_()


def _varchar_len(col: ColumnInfo) -> int | None:
    return col.size if col.size and col.size > 0 else None


def _string(col: ColumnInfo) -> sat.TypeEngine:
    return sat.String(_varchar_len(col))


def _nvarchar(col: ColumnInfo) -> sat.TypeEngine:
    n = _varchar_len(col)
    if n is None or n > MSSQL_MAX_NVARCHAR:
        return mssql.NVARCHAR()  # NVARCHAR(max)
    return mssql.NVARCHAR(n)


def _precision_scale(col: ColumnInfo) -> tuple[int, int]:
    precision = col.size if col.size and col.size > 0 else DEFAULT_DECIMAL_PRECISION
    scale = col.decimal_digits if col.decimal_digits is not None else DEFAULT_DECIMAL_SCALE
    return precision, max(0, min(scale, precision))


def _numeric(col: ColumnInfo) -> sat.TypeEngine:
    p, s = _precision_scale(col)
    return sat.Numeric(p, s, asdecimal=True)


def _unbounded_text(dialect: str) -> sat.TypeEngine:
    return mssql.NVARCHAR() if dialect == "mssql" else sat.Text()


_TEXT = TypeRule(_const(sat.Text), _const(mssql.NVARCHAR), _const(sat.Text))
_INT = TypeRule(_const(sat.Integer), _const(sat.Integer), _const(sat.Integer))
_SMALL = TypeRule(_const(sat.SmallInteger), _const(sat.SmallInteger), _const(sat.SmallInteger))
_BIG = TypeRule(_const(sat.BigInteger), _const(sat.BigInteger), _const(sat.BigInteger))

TYPE_MAP: dict[str, TypeRule] = {
    "VARCHAR": TypeRule(_string, _nvarchar, _string),
    "LONGCHAR": _TEXT,
    "COUNTER": _INT,
    "INTEGER": _INT,
    "SMALLINT": _SMALL,
    "BYTE": _SMALL,
    "BIGINT": _BIG,
    "REAL": TypeRule(
        lambda _c: sat.Float(24, asdecimal=False), _const(mssql.REAL), _const(postgresql.REAL)
    ),
    "DOUBLE": TypeRule(
        lambda _c: sat.Double(asdecimal=False),
        lambda _c: sat.Double(asdecimal=False),
        _const(postgresql.DOUBLE_PRECISION),
    ),
    "CURRENCY": TypeRule(
        _const(DecimalText),
        lambda _c: sat.Numeric(19, 4, asdecimal=True),
        lambda _c: sat.Numeric(19, 4, asdecimal=True),
    ),
    "DECIMAL": TypeRule(_const(DecimalText), _numeric, _numeric),
    "DATETIME": TypeRule(
        _const(sat.DateTime), _const(mssql.DATETIME2), lambda _c: postgresql.TIMESTAMP()
    ),
    "BIT": TypeRule(_const(sat.Boolean), _const(mssql.BIT), _const(sat.Boolean)),
    "GUID": TypeRule(
        lambda _c: sat.String(36),
        lambda _c: mssql.UNIQUEIDENTIFIER(as_uuid=False),
        lambda _c: sat.Uuid(as_uuid=False),
    ),
    "LONGBINARY": TypeRule(
        _const(sat.LargeBinary), lambda _c: mssql.VARBINARY("max"), _const(sat.LargeBinary)
    ),
}

ALIASES: dict[str, str] = {
    "TEXT": "VARCHAR",
    "CHAR": "VARCHAR",
    "MEMO": "LONGCHAR",
    "LONGTEXT": "LONGCHAR",
    "LONG": "INTEGER",
    "INT": "INTEGER",
    "TINYINT": "BYTE",
    "YESNO": "BIT",
    "AUTOINCREMENT": "COUNTER",
    "NUMERIC": "DECIMAL",
    "MONEY": "CURRENCY",
    "FLOAT": "DOUBLE",
    "UNIQUEIDENTIFIER": "GUID",
    "REPLICATIONID": "GUID",
    "VARBINARY": "LONGBINARY",
    "BINARY": "LONGBINARY",
    "IMAGE": "LONGBINARY",
    "OLE": "LONGBINARY",
}

_warned_unknown: set[str] = set()


def _canonical(access_type: str) -> str:
    key = (access_type or "").strip().upper()
    return ALIASES.get(key, key)


def is_known(access_type: str) -> bool:
    """True if ``access_type`` (or one of its aliases) has an entry in :data:`TYPE_MAP`."""
    return _canonical(access_type) in TYPE_MAP


def sa_type(col: ColumnInfo, dialect: str) -> sat.TypeEngine:
    """SQLAlchemy type for ``col`` on ``dialect``; unknown Access types become unbounded text."""
    if dialect not in DIALECTS:
        raise ValueError(f"unsupported dialect {dialect!r}; expected one of {DIALECTS}")
    rule = TYPE_MAP.get(_canonical(col.access_type))
    if rule is None:
        if col.access_type not in _warned_unknown:
            _warned_unknown.add(col.access_type)
            log.warning(
                "unknown Access type, mapping to unbounded text",
                extra={"kv": {"access_type": col.access_type, "column": col.name}},
            )
        return _unbounded_text(dialect)
    return rule.for_dialect(dialect)(col)
