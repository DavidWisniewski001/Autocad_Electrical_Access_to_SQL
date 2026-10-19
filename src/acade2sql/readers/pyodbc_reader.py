"""Reader backed by pyodbc and the Microsoft Access ODBC driver, opened strictly read-only."""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyodbc

from acade2sql.diagnostics import require_driver, translate_connect_error
from acade2sql.errors import Acade2SqlError, SourceNotFoundError, TableNotFoundError
from acade2sql.readers.base import Reader
from acade2sql.schema import ColumnInfo, IndexInfo, PrimaryKeySource, TableInfo

log = logging.getLogger(__name__)

ACCESS_SUFFIXES = frozenset({".mdb", ".accdb"})
PRIMARY_KEY_INDEX = "primarykey"
_BINARY_TYPES = frozenset({"LONGBINARY", "BINARY", "VARBINARY"})
_SQL_NULLABLE = 1


def quote_access(name: str) -> str:
    """Quote an identifier Access-style: ``[name]`` with ``]`` doubled."""
    return "[" + name.replace("]", "]]") + "]"


def _odbc_value(value: str) -> str:
    """Brace-quote a connection-string value when it contains ODBC delimiters."""
    if any(ch in value for ch in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _validate_source(path: Path) -> Path:
    if path.suffix.lower() not in ACCESS_SUFFIXES:
        raise SourceNotFoundError(f"Not an Access database (expected .mdb or .accdb): {path}")
    if not path.is_file():
        raise SourceNotFoundError(f"Access database not found: {path}")
    return path.resolve()


class PyodbcReader(Reader):
    """Access reader using ``pyodbc`` with ``ReadOnly=1`` and ``readonly=True``."""

    def __init__(self, path: str | Path, *, copy_to_temp: bool = False) -> None:
        self.path = Path(path)
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._conn: pyodbc.Connection | None = None
        self._tables: dict[str, list[str]] | None = None
        self._cursors: set[pyodbc.Cursor] = set()
        source = _validate_source(self.path)
        driver = require_driver()
        try:
            if copy_to_temp:
                source = self._copy_to_temp(source)
            self._conn = self._connect(driver, source)
        except BaseException:
            self.close()
            raise
        log.debug("opened access db", extra={"kv": {"path": str(self.path), "copy": copy_to_temp}})

    # -- lifecycle -----------------------------------------------------------------------------

    def _copy_to_temp(self, source: Path) -> Path:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="acade2sql-")
        target = Path(self._tmpdir.name) / source.name
        shutil.copy2(source, target)
        log.debug("copied source to temp", extra={"kv": {"temp": str(target)}})
        return target

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
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

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
        known = self.list_tables()
        if name in known:
            return name
        folded = name.casefold()
        matches = [n for n in known if n.casefold() == folded]
        if len(matches) == 1:
            return matches[0]
        raise TableNotFoundError(f"Table {name!r} not found in {self.path}")

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

    @staticmethod
    def _primary_key(
        columns: tuple[ColumnInfo, ...], indexes: tuple[IndexInfo, ...]
    ) -> tuple[tuple[str, ...], PrimaryKeySource]:
        """DECISIONS #6: 'PrimaryKey' unique index -> single COUNTER column -> none."""
        for idx in indexes:
            if idx.unique and idx.name.casefold() == PRIMARY_KEY_INDEX:
                return idx.columns, "discovered"
        counters = [c.name for c in columns if c.autoincrement]
        if len(counters) == 1:
            return (counters[0],), "inferred"
        return (), "none"

    def get_table(self, name: str) -> TableInfo:
        table = self._resolve(name)
        columns = self._columns(table)
        indexes = self._indexes(table)
        pk, source = self._primary_key(columns, indexes)
        if source == "discovered":
            # The driver reports every non-COUNTER column as nullable, but Access PK columns
            # can never hold NULL.
            pk_cols = set(pk)
            columns = tuple(replace(c, nullable=False) if c.name in pk_cols else c for c in columns)
        return TableInfo(
            name=table,
            columns=columns,
            primary_key=pk,
            primary_key_source=source,
            indexes=indexes,
        )

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
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError(f"batch_size must be an integer >= 1, got {batch_size!r}")
        table = self._resolve(name)
        return self._stream(table, self._columns(table), batch_size)

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
