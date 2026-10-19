"""Reader backed by pyodbc and the Microsoft Access ODBC driver, opened strictly read-only."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyodbc

from acade2sql.diagnostics import require_driver, translate_connect_error
from acade2sql.errors import Acade2SqlError, UnsupportedAccessFeature
from acade2sql.readers.base import (
    ACCESS_SUFFIXES,
    PRIMARY_KEY_INDEX,
    Reader,
    TempCopy,
    build_table_info,
    check_batch_size,
    resolve_table,
    validate_source,
)
from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo

# Requirement 10: no ODBC connection pooling, so closing the reader really releases the Access file
# (and its .ldb lock). Set at import time, before the first pyodbc.connect() in the process.
pyodbc.pooling = False

log = logging.getLogger(__name__)

__all__ = ["ACCESS_SUFFIXES", "PRIMARY_KEY_INDEX", "PyodbcReader", "quote_access"]

_BINARY_TYPES = frozenset({"LONGBINARY", "BINARY", "VARBINARY"})
# The Access ODBC driver reports a Large Number (BIGINT) column as CHAR(4) and returns its values
# as garbage 4-character strings (DECISIONS #45). Such columns are cross-checked against the TDEF.
_SUSPECT_TYPE, _SUSPECT_SIZE = "CHAR", 4
_TDEF_BIGINT = 19
_SQL_NULLABLE = 1


def quote_access(name: str) -> str:
    """Quote an identifier Access-style: ``[name]`` with ``]`` doubled."""
    return "[" + name.replace("]", "]]") + "]"


def _odbc_value(value: str) -> str:
    """Brace-quote a connection-string value when it contains ODBC delimiters."""
    if any(ch in value for ch in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


class PyodbcReader(Reader):
    """Access reader using ``pyodbc`` with ``ReadOnly=1`` and ``readonly=True``."""

    kind = "odbc"

    def __init__(self, path: str | Path, *, copy_to_temp: bool = False) -> None:
        self.path = Path(path)
        self._temp: TempCopy | None = None
        self._conn: pyodbc.Connection | None = None
        self._tables: dict[str, list[str]] | None = None
        self._cursors: set[pyodbc.Cursor] = set()
        self._checked: set[str] = set()  # tables that passed the BIGINT cross-check
        source = validate_source(self.path)
        driver = require_driver()
        try:
            if copy_to_temp:
                self._temp = TempCopy(source)
                source = self._temp.path
            self._source = source  # the file actually opened (the temp copy with copy_to_temp)
            self._conn = self._connect(driver, source)
        except BaseException:
            self.close()
            raise
        log.debug("opened access db", extra={"kv": {"path": str(self.path), "copy": copy_to_temp}})

    # -- lifecycle -----------------------------------------------------------------------------

    @staticmethod
    def _connect(driver: str, source: Path) -> pyodbc.Connection:
        conn_str = f"DRIVER={{{driver}}};DBQ={_odbc_value(str(source))};ReadOnly=1;Exclusive=0"
        try:
            return pyodbc.connect(conn_str, readonly=True, autocommit=True)
        except pyodbc.Error as exc:
            raise translate_connect_error(exc) from exc

    def close(self) -> None:
        for cur in list(self._cursors):  # e.g. abandoned iter_rows generators
            self._release(cur)
        if self._conn is not None:
            try:
                self._conn.close()
            except pyodbc.Error:  # pragma: no cover - already broken connection
                log.debug("error while closing connection", exc_info=True)
            self._conn = None
        if self._temp is not None:
            # The Access driver can hold the copied file for a moment after close: cleanup retries.
            self._temp.cleanup()
            self._temp = None

    def _cursor(self) -> pyodbc.Cursor:
        if self._conn is None:
            raise Acade2SqlError(f"Reader for {self.path} is closed")
        cur = self._conn.cursor()
        self._cursors.add(cur)
        return cur

    def _release(self, cur: pyodbc.Cursor) -> None:
        self._cursors.discard(cur)
        with contextlib.suppress(pyodbc.Error):
            cur.close()

    # -- catalog -------------------------------------------------------------------------------

    def _catalog(self) -> dict[str, list[str]]:
        if self._tables is None:
            cur = self._cursor()
            try:
                rows = cur.tables().fetchall()
            finally:
                self._release(cur)
            tables: dict[str, list[str]] = {"TABLE": [], "SYSTEM TABLE": []}
            for row in rows:
                kind = (row.table_type or "").upper()
                if kind in tables:
                    tables[kind].append(row.table_name)
            self._tables = tables
        return self._tables

    def list_tables(self, include_system: bool = False) -> list[str]:
        catalog = self._catalog()
        names = [n for n in catalog["TABLE"] if not n.lower().startswith("msys")]
        if include_system:
            names += [n for n in catalog["SYSTEM TABLE"] if n not in names]
            names += [n for n in catalog["TABLE"] if n.lower().startswith("msys")]
        return sorted(set(names), key=lambda n: (n.casefold(), n))

    def _resolve(self, name: str) -> str:
        """Map a user-supplied name to the exact user-table name, or raise TableNotFoundError.

        System tables (MSys*) are not resolvable: the driver denies reading them anyway.
        """
        return resolve_table(name, self.list_tables(), self.path)

    def _columns(self, table: str) -> tuple[ColumnInfo, ...]:
        cur = self._cursor()
        try:
            rows = cur.columns(table=table).fetchall()
        finally:
            self._release(cur)
        # SQLColumns treats the table name as a LIKE pattern ('_' is a wildcard): filter exactly.
        rows = [r for r in rows if r.table_name == table]
        rows.sort(key=lambda r: r.ordinal_position)
        return tuple(
            ColumnInfo(
                name=r.column_name,
                ordinal=int(r.ordinal_position),
                access_type=str(r.type_name).upper(),
                data_type=int(r.data_type),
                size=None if r.column_size is None else int(r.column_size),
                decimal_digits=None if r.decimal_digits is None else int(r.decimal_digits),
                nullable=r.nullable == _SQL_NULLABLE,
                autoincrement=str(r.type_name).upper() == "COUNTER",
            )
            for r in rows
        )

    def _indexes(self, table: str) -> tuple[IndexInfo, ...]:
        cur = self._cursor()
        try:
            rows = cur.statistics(table=table).fetchall()
        except pyodbc.Error as exc:
            log.debug("statistics() failed", extra={"kv": {"table": table, "error": exc}})
            return ()
        finally:
            self._release(cur)
        grouped: dict[str, list[tuple[int, str]]] = {}
        unique: dict[str, bool] = {}
        for r in rows:
            if r.index_name is None or r.column_name is None:
                continue
            if r.table_name is not None and r.table_name != table:
                continue
            grouped.setdefault(r.index_name, []).append((int(r.ordinal_position), r.column_name))
            unique[r.index_name] = not r.non_unique
        return tuple(
            IndexInfo(name=n, columns=tuple(c for _, c in sorted(cols)), unique=unique[n])
            for n, cols in grouped.items()
        )

    def _checked_columns(self, table: str) -> tuple[ColumnInfo, ...]:
        """:meth:`_columns`, refusing a table whose CHAR(4) column is really a BIGINT."""
        columns = self._columns(table)
        if table not in self._checked:
            self._check_bigint(table, columns)
            self._checked.add(table)
        return columns

    def _check_bigint(self, table: str, columns: tuple[ColumnInfo, ...]) -> None:
        """Raise :class:`UnsupportedAccessFeature` for a Large Number column the driver misreads.

        Only tables with a column the driver reports as CHAR(4) are checked (cheap pre-filter);
        the real type comes from the file's table definition, parsed read-only by the pure
        reader. When that parse fails the driver's metadata is kept (WARNING).
        """
        suspects = [
            c.name for c in columns if c.access_type == _SUSPECT_TYPE and c.size == _SUSPECT_SIZE
        ]
        if not suspects:
            return
        try:
            from acade2sql.readers.access_parser_reader import column_type_codes

            codes = column_type_codes(self._source, table)
        except Exception as exc:  # never break ODBC reads over the cross-check
            log.warning(
                "cannot cross-check CHAR(4) columns against the table definition; using the "
                "driver's metadata (a Large Number column would be misread)",
                extra={"kv": {"table": table, "columns": suspects, "error": exc}},
            )
            return
        folded = {k.casefold(): v for k, v in codes.items()}
        for name in suspects:
            code = codes.get(name, folded.get(name.casefold()))
            if code == _TDEF_BIGINT:
                raise UnsupportedAccessFeature(
                    f"column {name!r} of table {table!r} is an Access Large Number (BIGINT), "
                    "which the Access ODBC driver misreads; use --reader pure"
                )

    def get_table(self, name: str) -> TableInfo:
        table = self._resolve(name)
        # The driver reports every non-COUNTER column as nullable, but Access PK columns can
        # never hold NULL: build_table_info makes discovered PK columns NOT NULL.
        return build_table_info(table, self._checked_columns(table), self._indexes(table))

    # -- data ----------------------------------------------------------------------------------

    def count_rows(self, name: str) -> int:
        table = self._resolve(name)
        cur = self._cursor()
        try:
            row = cur.execute(f"SELECT COUNT(*) FROM {quote_access(table)}").fetchone()
        finally:
            self._release(cur)
        return int(row[0])

    def iter_rows(self, name: str, batch_size: int = 5000) -> Iterator[list[tuple]]:
        check_batch_size(batch_size)
        table = self._resolve(name)
        return self._stream(table, self._checked_columns(table), batch_size)

    def _stream(
        self, table: str, columns: tuple[ColumnInfo, ...], batch_size: int
    ) -> Iterator[list[tuple]]:
        select = ", ".join(quote_access(c.name) for c in columns) or "*"
        sql = f"SELECT {select} FROM {quote_access(table)}"
        converters = _converters(columns)
        cur = self._cursor()
        total = 0
        try:
            cur.execute(sql)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                batch = [_to_tuple(r, converters) for r in rows]
                total += len(batch)
                yield batch
        finally:
            self._release(cur)
            log.debug("read table", extra={"kv": {"table": table, "rows": total}})


def _to_bytes(value: Any) -> Any:
    return bytes(value) if isinstance(value, (bytearray, memoryview)) else value


Converter = Callable[[Any], Any]


def _converters(columns: tuple[ColumnInfo, ...]) -> list[tuple[int, Converter]]:
    out: list[tuple[int, Converter]] = []
    for i, col in enumerate(columns):
        if col.access_type in _BINARY_TYPES:
            out.append((i, _to_bytes))
    return out


def _to_tuple(row: pyodbc.Row, converters: list[tuple[int, Converter]]) -> tuple:
    if not converters:
        return tuple(row)
    values = list(row)
    for i, fn in converters:
        values[i] = fn(values[i])
    return tuple(values)
