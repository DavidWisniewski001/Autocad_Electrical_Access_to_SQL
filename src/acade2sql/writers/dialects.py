"""Per-dialect limits: SQL Server index key sizes, PostgreSQL identifier length and NUL in text."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from sqlalchemy import Column, Table
from sqlalchemy import types as sat
from sqlalchemy.dialects import mssql

from acade2sql.errors import SchemaError, TargetDataError
from acade2sql.schema import TableInfo

MSSQL_INDEX_MAX_BYTES = 1700  # non-clustered index key limit (SQL Server 2016+)
MSSQL_PK_MAX_BYTES = 900  # clustered index key limit (the PK is clustered by default)
PG_IDENTIFIER_MAX_BYTES = 63  # NAMEDATALEN - 1; longer names are silently truncated

RowGuard = Callable[[list[tuple]], None]


def _numeric_bytes(precision: int | None) -> int:
    p = precision or 18
    if p <= 9:
        return 5
    if p <= 19:
        return 9
    if p <= 28:
        return 13
    return 17


def mssql_key_bytes(type_: sat.TypeEngine) -> int | None:
    """Bytes one value of ``type_`` takes in a SQL Server index key; ``None`` for (max) types."""
    t = type_.impl_instance if isinstance(type_, sat.TypeDecorator) else type_
    if isinstance(t, sat._Binary):
        length = getattr(t, "length", None)
        return None if length in (None, "max") else int(length)
    if isinstance(t, sat.String):
        if isinstance(t, sat.Text) or t.length is None:
            return None
        wide = isinstance(t, (sat.Unicode, mssql.NVARCHAR, mssql.NCHAR))
        return 2 * t.length if wide else t.length
    if isinstance(t, mssql.UNIQUEIDENTIFIER | sat.Uuid):
        return 16
    if isinstance(t, sat.Boolean | mssql.BIT):
        return 1
    if isinstance(t, sat.SmallInteger):
        return 2
    if isinstance(t, sat.BigInteger):
        return 8
    if isinstance(t, sat.Integer):
        return 4
    if isinstance(t, sat.Float):
        return 4 if t.precision is not None and t.precision <= 24 else 8
    if isinstance(t, sat.Numeric):
        return _numeric_bytes(t.precision)
    return 8  # DATETIME2(7) and anything else fixed-size


def mssql_key_problem(columns: Sequence[Column], limit: int) -> str | None:
    """Why ``columns`` cannot be a SQL Server index key within ``limit`` bytes (None: they can)."""
    total = 0
    for col in columns:
        size = mssql_key_bytes(col.type)
        if size is None:
            return f"column {col.name} is NVARCHAR(max)/VARBINARY(max)"
        total += size
    if total > limit:
        return f"key size {total} bytes exceeds the {limit}-byte limit"
    return None


def pg_long_identifiers(infos: Iterable[TableInfo], schema: str | None = None) -> list[str]:
    """Table/column (and schema) names longer than 63 UTF-8 bytes, as ``table.column`` labels."""
    out: list[str] = []
    if schema and len(schema.encode("utf-8")) > PG_IDENTIFIER_MAX_BYTES:
        out.append(f"schema {schema}")
    for info in infos:
        if len(info.name.encode("utf-8")) > PG_IDENTIFIER_MAX_BYTES:
            out.append(f"table {info.name}")
        for col in info.columns:
            if len(col.name.encode("utf-8")) > PG_IDENTIFIER_MAX_BYTES:
                out.append(f"column {info.name}.{col.name}")
    return out


def check_pg_identifiers(infos: Iterable[TableInfo], schema: str | None = None) -> None:
    """Raise :class:`SchemaError` naming every identifier PostgreSQL would truncate."""
    long_names = pg_long_identifiers(infos, schema)
    if long_names:
        raise SchemaError(
            f"identifier(s) longer than {PG_IDENTIFIER_MAX_BYTES} bytes are not supported by "
            f"PostgreSQL (they would be truncated and could collide): {'; '.join(long_names)}. "
            "Exclude or rename these tables."
        )


def _noop_guard(_batch: list[tuple]) -> None:
    return None


def row_guard(table: Table, dialect: str) -> RowGuard:
    """A per-batch check of values the target cannot store (PostgreSQL: NUL in text)."""
    if dialect != "postgresql":
        return _noop_guard
    text_cols = [
        (i, col.name)
        for i, col in enumerate(table.columns)
        if isinstance(col.type, sat.String)
        or (isinstance(col.type, sat.TypeDecorator) and isinstance(col.type.impl, sat.String))
    ]
    if not text_cols:
        return _noop_guard

    def guard(batch: list[tuple]) -> None:
        for row in batch:
            for i, name in text_cols:
                value = row[i]
                if isinstance(value, str) and "\x00" in value:
                    raise TargetDataError(
                        f"NUL character in text column {name} (not supported by PostgreSQL)"
                    )

    return guard
