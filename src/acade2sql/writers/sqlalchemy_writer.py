"""Write Access tables to any SQLAlchemy target: DDL from :class:`TableInfo`, batched inserts.

Identifiers are passed verbatim to SQLAlchemy with ``quote=True``; the dialect quotes them. No SQL
text is ever assembled from identifiers here.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from sqlalchemy import (
    Column,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.sql.elements import quoted_name

from acade2sql.errors import TableNotFoundError, TargetError, WriteError
from acade2sql.redact import mask_url, redact
from acade2sql.schema import IndexInfo, TableInfo
from acade2sql.typemap import DECIMAL_TEXT_SQLITE, DecimalText, sa_type

log = logging.getLogger(__name__)

INDEX_NAME_MAX = 60
_HASH_LEN = 8
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_SQLITE_PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL")

BindProcessor = Callable[[Any], Any]


# -- DDL ---------------------------------------------------------------------------------------


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:_HASH_LEN]


def index_name(table: str, index: str, used: set[str]) -> str:
    """``ix_<table>_<index>`` sanitised to ``[A-Za-z0-9_]``, <= 60 chars, unique within ``used``.

    An 8-hex-char hash suffix is added when the name is truncated or already used (compared
    case-insensitively, as SQLite and most servers do). The chosen name is added to ``used``.
    """
    base = _UNSAFE.sub("_", f"ix_{table}_{index}")
    taken = {u.casefold() for u in used}
    name = base
    if len(base) > INDEX_NAME_MAX or base.casefold() in taken:
        stem = base[: INDEX_NAME_MAX - _HASH_LEN - 1]
        name = f"{stem}_{_short_hash(table, index)}"
        n = 1
        while name.casefold() in taken:  # astronomically rare: hash collision
            name = f"{stem}_{_short_hash(table, index, str(n))}"
            n += 1
    used.add(name)
    return name


def _is_pk_index(idx: IndexInfo, info: TableInfo) -> bool:
    """The PK index is a unique index on exactly the primary-key columns (in key order)."""
    return bool(info.primary_key) and idx.unique and tuple(idx.columns) == tuple(info.primary_key)


def build_table(
    metadata: MetaData, info: TableInfo, dialect: str, *, used_index_names: set[str]
) -> Table:
    """Build the SQLAlchemy :class:`Table` (columns, PK, indexes) for ``info`` on ``dialect``."""
    pk = set(info.primary_key)
    columns = [
        Column(
            quoted_name(c.name, quote=True),
            sa_type(c, dialect),
            nullable=False if c.name in pk else c.nullable,
            autoincrement=False,
            quote=True,
        )
        for c in sorted(info.columns, key=lambda c: c.ordinal)
    ]
    table = Table(quoted_name(info.name, quote=True), metadata, *columns, quote=True)
    if info.primary_key:
        table.append_constraint(PrimaryKeyConstraint(*(table.c[n] for n in info.primary_key)))
    for idx in info.indexes:
        if _is_pk_index(idx, info):
            continue
        missing = [c for c in idx.columns if c not in table.c]
        if missing or not idx.columns:
            log.warning(
                "skipping index with unknown columns",
                extra={"kv": {"table": info.name, "index": idx.name, "missing": missing}},
            )
            continue
        Index(
            quoted_name(index_name(info.name, idx.name, used_index_names), quote=True),
            *(table.c[c] for c in idx.columns),
            unique=idx.unique,
            info={"source_name": idx.name},
        )
    return table


# -- engine ------------------------------------------------------------------------------------


def _sqlite_on_connect(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    try:
        for pragma in _SQLITE_PRAGMAS:
            cur.execute(pragma)
    finally:
        cur.close()


_BAD_URL_ERRORS = (sa_exc.ArgumentError, sa_exc.NoSuchModuleError, ValueError, TypeError)


def _create_engine(url: str, echo: bool) -> Engine:
    try:
        parsed = make_url(url)
    except _BAD_URL_ERRORS as exc:
        raise TargetError(f"Invalid target URL {mask_url(url)}: {redact(str(exc), url)}") from None
    db = parsed.database or ""
    if parsed.get_backend_name() == "sqlite" and db not in ("", ":memory:") and ":" not in db[2:]:
        folder = Path(db).expanduser().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TargetError(f"Cannot create target folder {folder}: {exc}") from exc
    try:
        engine = create_engine(parsed, echo=echo)
    except (*_BAD_URL_ERRORS, ImportError) as exc:
        raise TargetError(
            f"Cannot open target {mask_url(parsed)}: {redact(str(exc), parsed)}"
        ) from None
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_on_connect)
        # Instance-level copy: reflect DECIMAL_TEXT columns back as DecimalText (exact Decimal).
        engine.dialect.ischema_names = {
            **engine.dialect.ischema_names,
            DECIMAL_TEXT_SQLITE: DecimalText,
        }
    return engine


# -- writer ------------------------------------------------------------------------------------


class SqlAlchemyWriter:
    """Create tables and bulk-insert rows into a SQLAlchemy target. Use as a context manager."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._engine = _create_engine(url, echo)
        self.dialect: str = self._engine.dialect.name
        self.url: URL = self._engine.url
        self.url_masked: str = mask_url(self._engine.url)
        self._metadata = MetaData()
        self._tables: dict[str, Table] = {}
        self._used_index_names: set[str] = set()
        log.debug("opened target", extra={"kv": {"url": self.url_masked}})

    # lifecycle

    def __enter__(self) -> SqlAlchemyWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Dispose the engine (closes pooled connections; SQLite checkpoints its WAL)."""
        self._engine.dispose()

    @property
    def engine(self) -> Engine:
        return self._engine

    # DDL

    def _forget(self, name: str) -> None:
        old = self._tables.pop(name, None)
        if old is not None:
            for idx in old.indexes:
                self._used_index_names.discard(str(idx.name))
            self._metadata.remove(old)

    def _seed_index_names(self, conn: Connection, converting: set[str]) -> None:
        """Reserve index names of target tables that are not being converted."""
        insp = inspect(conn)
        for existing in insp.get_table_names():
            if existing.casefold() in converting:
                continue
            for idx in insp.get_indexes(existing):
                if idx.get("name"):
                    self._used_index_names.add(idx["name"])

    def _build(self, info: TableInfo) -> Table:
        self._forget(info.name)
        table = build_table(
            self._metadata, info, self.dialect, used_index_names=self._used_index_names
        )
        self._tables[info.name] = table
        return table

    def redact(self, text: str) -> str:
        """``text`` with this target's secrets removed (for messages and logs)."""
        return redact(text, self._engine.url)

    def prepare(self, tables: Sequence[TableInfo], mode: Literal["full"] = "full") -> None:
        """Build the SQLAlchemy tables and reserve their index names. Executes no DDL.

        Connects once to reserve the names of existing indexes on target tables that are not being
        converted (``TargetError`` if the target is unreachable). Then call :meth:`create_table`
        for each table right before loading it.
        """
        if mode != "full":
            raise ValueError(f"unsupported mode {mode!r} (M3 supports only 'full')")
        try:
            with self._engine.connect() as conn:
                self._seed_index_names(conn, {t.name.casefold() for t in tables})
        except sa_exc.SQLAlchemyError as exc:
            cause = getattr(exc, "orig", None) or exc
            raise TargetError(
                f"Cannot connect to target {self.url_masked}: {self.redact(str(cause))}"
            ) from None
        for info in tables:
            self._build(info)

    def create_table(self, info: TableInfo) -> None:
        """``full`` DDL for one table: drop it if it exists, then create it and its indexes.

        Raises ``WriteError`` (message redacted) on failure; other target tables are untouched.
        A table not passed to :meth:`prepare` is built (and its index names reserved) here.
        """
        table = self._tables.get(info.name)
        if table is None:
            table = self._build(info)
        try:
            with self._engine.begin() as conn:
                table.drop(conn, checkfirst=True)
                table.create(conn)  # also creates the table's indexes
        except sa_exc.SQLAlchemyError as exc:
            raise WriteError(info.name, exc, url=self._engine.url) from exc
        log.debug(
            "created table",
            extra={"kv": {"table": info.name, "indexes": len(table.indexes)}},
        )

    def _table(self, name: str) -> Table:
        table = self._tables.get(name)
        if table is not None:
            return table
        for key, t in self._tables.items():
            if key.casefold() == name.casefold():
                return t
        insp = inspect(self._engine)
        if not insp.has_table(name):
            raise TableNotFoundError(f"Table {name!r} not found in target {self.url_masked}")
        return Table(quoted_name(name, quote=True), MetaData(), autoload_with=self._engine)

    # data

    def write_batches(self, info: TableInfo, batches: Iterable[list[tuple]]) -> int:
        """Insert all batches in one transaction; on error roll back and raise ``WriteError``."""
        table = self._tables.get(info.name)
        if table is None:
            table = self._build(info)
        total = 0
        try:
            with self._engine.begin() as conn:
                insert, close = _inserter(conn, table)
                try:
                    for batch in batches:
                        if batch:
                            insert(batch)
                            total += len(batch)
                finally:
                    close()
        except Exception as exc:  # roll-back already happened in engine.begin()
            raise WriteError(info.name, exc, url=self._engine.url) from exc
        return total

    def count_rows(self, name: str) -> int:
        table = self._table(name)
        with self._engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(table)).scalar_one())

    def read_rows(self, name: str, batch_size: int = 5000) -> Iterator[list[tuple]]:
        """Batches of tuples in column order, ordered by PK (if any), typed via SQLAlchemy."""
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
        table = self._table(name)
        stmt = select(*table.columns)
        if table.primary_key.columns:
            stmt = stmt.order_by(*table.primary_key.columns)
        with self._engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(stmt)
            while True:
                rows = result.fetchmany(batch_size)
                if not rows:
                    break
                yield [tuple(r) for r in rows]


def _noop() -> None:
    return None


def _inserter(
    conn: Connection, table: Table
) -> tuple[Callable[[list[tuple]], None], Callable[[], None]]:
    """Return a function inserting one batch of positional tuples into ``table``.

    SQLite fast path: the INSERT is compiled once by SQLAlchemy (dialect quoting, bound
    parameters) and the tuples go straight to the DBAPI cursor's ``executemany``, after applying
    each column's SQLAlchemy bind processor. Every other dialect (and any non-positional
    compilation) uses ``conn.execute(table.insert(), list_of_dicts)``.
    """
    dialect = conn.dialect
    keys = [c.key for c in table.columns]
    stmt = table.insert()
    compiled = stmt.compile(dialect=dialect, column_keys=keys)
    fast = dialect.name == "sqlite" and compiled.positional
    if not fast or list(compiled.positiontup or ()) != keys:

        def insert_dicts(batch: list[tuple]) -> None:
            conn.execute(stmt, [dict(zip(keys, row, strict=True)) for row in batch])

        return insert_dicts, _noop

    sql = compiled.string
    procs: list[tuple[int, BindProcessor]] = []
    for i, col in enumerate(table.columns):
        proc = col.type.dialect_impl(dialect).bind_processor(dialect)
        if proc is not None:
            procs.append((i, proc))
    cursor = conn.connection.dbapi_connection.cursor()

    def insert_tuples(batch: list[tuple]) -> None:
        if procs:
            batch = [_apply(row, procs) for row in batch]
        cursor.executemany(sql, batch)

    return insert_tuples, cursor.close


def _apply(row: tuple, procs: list[tuple[int, BindProcessor]]) -> tuple:
    values = list(row)
    for i, proc in procs:
        if values[i] is not None:
            values[i] = proc(values[i])
    return tuple(values)
