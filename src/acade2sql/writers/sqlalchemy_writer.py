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
from typing import TYPE_CHECKING, Any, Literal, get_args

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

from acade2sql.errors import (
    SchemaError,
    TableNotFoundError,
    TargetError,
    UsageError,
    WriteError,
)
from acade2sql.redact import mask_url, redact
from acade2sql.schema import IndexInfo, TableInfo
from acade2sql.typemap import DECIMAL_TEXT_SQLITE, DecimalText, sa_type
from acade2sql.writers.dialects import (
    MSSQL_INDEX_MAX_BYTES,
    MSSQL_PK_MAX_BYTES,
    RowGuard,
    mssql_key_problem,
    row_guard,
)

if TYPE_CHECKING:
    from acade2sql.writers.sync import SyncSession

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
    """Build the SQLAlchemy :class:`Table` (columns, PK, indexes) for ``info`` on ``dialect``.

    The table is created in ``metadata.schema`` (if set). On SQL Server an index that cannot be
    created (an NVARCHAR(max)/VARBINARY(max) column, or a key over 1700 bytes) is left out and
    described in ``table.info["warnings"]``; a primary key SQL Server cannot index (900 bytes)
    is described in ``table.info["error"]`` (raised by ``SqlAlchemyWriter`` when creating it).
    """
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
    warnings: list[str] = []
    table.info["warnings"] = warnings
    if info.primary_key:
        pk_cols = [table.c[n] for n in info.primary_key]
        table.append_constraint(PrimaryKeyConstraint(*pk_cols))
        if dialect == "mssql":
            problem = mssql_key_problem(pk_cols, MSSQL_PK_MAX_BYTES)
            if problem:
                table.info["error"] = (
                    f"primary key ({', '.join(info.primary_key)}) cannot be created on SQL "
                    f"Server: {problem}"
                )
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
        idx_cols = [table.c[c] for c in idx.columns]
        if dialect == "mssql":
            problem = mssql_key_problem(idx_cols, MSSQL_INDEX_MAX_BYTES)
            if problem:
                text = f"index {idx.name} skipped on SQL Server: {problem}"
                warnings.append(text)
                log.warning(text, extra={"kv": {"table": info.name}})
                continue
        Index(
            quoted_name(index_name(info.name, idx.name, used_index_names), quote=True),
            *idx_cols,
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

Mode = Literal["full", "append", "sync"]
MODES: tuple[str, ...] = get_args(Mode)

POSTGRESQL_HINT = "install the PostgreSQL driver: pip install acade2sql[postgresql]"
MSSQL_HINT = (
    "SQL Server needs pyodbc (pip install acade2sql[mssql]) and the Microsoft "
    '"ODBC Driver 18 for SQL Server" (or 17) installed for this Python\'s bitness; name it in '
    "the URL, e.g. mssql+pyodbc://user:pass@host/db?driver=ODBC+Driver+18+for+SQL+Server"
)
_ODBC_DRIVER_MISSING = re.compile(
    r"IM002|Data source name not found|Can't open lib|driver.*not (found|installed)", re.I
)


def dbapi_hint(backend: str) -> str:
    """What to install for ``backend`` ('' when there is nothing specific to say)."""
    return {"postgresql": POSTGRESQL_HINT, "mssql": MSSQL_HINT}.get(backend, "")


CONNECT_TIMEOUT_S = 15
LIBPQ_DRIVERS = frozenset({"psycopg", "psycopg2"})  # accept libpq's ``connect_timeout``
_SCHEMA_FORBIDDEN = {".": "'.'", "[": "'['", "]": "']'", '"': "'\"'", "`": "'`'"}


def check_schema_name(schema: str | None) -> None:
    """Raise :class:`UsageError` for a ``--schema`` that is not one plain identifier.

    Rejected: empty/whitespace-only names and names containing a dot, square brackets, a double
    quote, a backtick or a control character. The schema is quoted as ONE identifier, so a dotted
    or pre-quoted name would not mean what the user expects.
    """
    if schema is None:
        return
    if not schema.strip():
        raise UsageError("--schema must not be empty")
    for ch in schema:
        if ch in _SCHEMA_FORBIDDEN:
            bad = _SCHEMA_FORBIDDEN[ch]
        elif ord(ch) < 32 or 127 <= ord(ch) < 160:
            bad = f"control character U+{ord(ch):04X}"
        else:
            continue
        raise UsageError(
            f"invalid --schema {schema!r}: contains {bad}; give one plain schema name "
            "(it is quoted by acade2sql)"
        )


def _url_sets_timeout(parsed: URL) -> bool:
    """True when the URL already chooses a connect/login timeout (query key or odbc_connect)."""
    for key, value in parsed.query.items():
        values = (value,) if isinstance(value, str) else tuple(value)
        if "timeout" in key.lower() or (
            key.lower() == "odbc_connect" and any("timeout" in v.lower() for v in values)
        ):
            return True
    return False


def _engine_kwargs(parsed: URL) -> dict[str, Any]:
    """Engine options per target: mssql+pyodbc ``fast_executemany``; a 15 s connect timeout for
    server targets unless the URL sets one (an unreachable host otherwise hangs > 60 s)."""
    backend, driver = parsed.get_backend_name(), parsed.get_driver_name()
    kwargs: dict[str, Any] = {}
    if backend == "mssql" and driver == "pyodbc":
        kwargs["fast_executemany"] = True
        if not _url_sets_timeout(parsed):
            kwargs["connect_args"] = {"timeout": CONNECT_TIMEOUT_S}  # ODBC login timeout
    elif (
        backend == "postgresql"
        and _driver_name(parsed) in LIBPQ_DRIVERS
        and not _url_sets_timeout(parsed)
    ):
        kwargs["connect_args"] = {"connect_timeout": CONNECT_TIMEOUT_S}  # libpq keyword
    return kwargs


def _driver_name(parsed: URL) -> str | None:
    """The DBAPI driver of ``parsed`` (the dialect's default when the URL names none)."""
    try:
        return parsed.get_driver_name()
    except (sa_exc.SQLAlchemyError, ImportError):
        return None


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
        engine = create_engine(parsed, echo=echo, **_engine_kwargs(parsed))
    except ImportError as exc:  # the DBAPI module (psycopg, pyodbc, ...) is not installed
        hint = dbapi_hint(parsed.get_backend_name())
        raise TargetError(
            f"Cannot open target {mask_url(parsed)}: {redact(str(exc), parsed)}"
            + (f". {hint}" if hint else "")
        ) from None
    except _BAD_URL_ERRORS as exc:
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


def schema_drift(table: Table, target_columns: Sequence[dict[str, Any]]) -> str | None:
    """Why the existing target table cannot receive ``table``'s columns (None: it can).

    Every source column must exist in the target (exact name). A target NOT NULL column without
    a default (or identity) that the source lacks cannot be filled. Extra nullable target columns
    are fine (left NULL).
    """
    target_names = {str(c["name"]) for c in target_columns}
    source_names = [str(c.name) for c in table.columns]
    missing = [n for n in source_names if n not in target_names]
    required = [
        str(c["name"])
        for c in target_columns
        if str(c["name"]) not in source_names
        and not c.get("nullable", True)
        and c.get("default") is None
        and not c.get("identity")
        and c.get("autoincrement") is not True
        and not c.get("computed")
    ]
    if not missing and not required:
        return None
    parts = []
    if missing:
        parts.append(f"target table {table.name} lacks source column(s) {', '.join(missing)}")
    if required:
        parts.append(
            f"target NOT NULL column(s) without default not in the source: {', '.join(required)}"
        )
    return f"schema drift: {'; '.join(parts)} (re-run with --mode full)"


# -- writer ------------------------------------------------------------------------------------


class SqlAlchemyWriter:
    """Create tables and bulk-insert rows into a SQLAlchemy target. Use as a context manager.

    ``schema``: create/read every table in that (existing) database schema; not for SQLite.
    ``_force_generic_sync``: use the portable SELECT/UPDATE/INSERT sync even where a native upsert
    (``INSERT ... ON CONFLICT``) exists (SQLite, PostgreSQL). Also settable as a class attribute.
    """

    _force_generic_sync: bool = False

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        schema: str | None = None,
        _force_generic_sync: bool | None = None,
    ) -> None:
        check_schema_name(schema)
        self._engine = _create_engine(url, echo)
        self.dialect: str = self._engine.dialect.name
        if schema is not None and self.dialect == "sqlite":
            self._engine.dispose()
            raise UsageError("--schema is not supported for SQLite targets")
        if _force_generic_sync is not None:
            self._force_generic_sync = _force_generic_sync
        self.schema = schema
        self.url: URL = self._engine.url
        self.url_masked: str = mask_url(self._engine.url)
        self._metadata = MetaData(schema=schema)
        self._tables: dict[str, Table] = {}
        self._used_index_names: set[str] = set()
        log.debug("opened target", extra={"kv": {"url": self.url_masked, "schema": schema}})

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
        """Reserve index names of target tables (except those in ``converting``)."""
        insp = inspect(conn)
        for existing in insp.get_table_names(schema=self.schema):
            if existing.casefold() in converting:
                continue
            for idx in insp.get_indexes(existing, schema=self.schema):
                if idx.get("name"):
                    self._used_index_names.add(idx["name"])

    def _build(self, info: TableInfo) -> Table:
        self._forget(info.name)
        table = build_table(
            self._metadata, info, self.dialect, used_index_names=self._used_index_names
        )
        self._tables[info.name] = table
        return table

    def _get(self, info: TableInfo) -> Table:
        table = self._tables.get(info.name)
        return table if table is not None else self._build(info)

    def redact(self, text: str) -> str:
        """``text`` with this target's secrets removed (for messages and logs)."""
        return redact(text, self._engine.url)

    def connect_error(self, exc: BaseException) -> TargetError:
        """``TargetError`` for a failed connection, with a driver hint where it applies."""
        cause = getattr(exc, "orig", None) or exc
        text = self.redact(str(cause))
        message = f"Cannot connect to target {self.url_masked}: {text}"
        if self.dialect == "mssql" and _ODBC_DRIVER_MISSING.search(text):
            message += f". {MSSQL_HINT}"
        return TargetError(message)

    def prepare(self, tables: Sequence[TableInfo], mode: Mode = "full") -> None:
        """Build the SQLAlchemy tables and reserve their index names. Executes no DDL.

        Connects once to reserve the names of existing target indexes (``TargetError`` if the
        target is unreachable): in ``full`` mode those of tables not being converted (the others
        are dropped and re-created), in ``append``/``sync`` mode all of them (nothing is
        dropped). Then call :meth:`create_table` (full) or :meth:`ensure_table` (append/sync)
        for each table right before loading it.
        """
        if mode not in MODES:
            raise ValueError(f"unsupported mode {mode!r}; expected one of {MODES}")
        converting = {t.name.casefold() for t in tables} if mode == "full" else set()
        try:
            with self._engine.connect() as conn:
                self._seed_index_names(conn, converting)
        except (sa_exc.SQLAlchemyError, TypeError) as exc:
            # TypeError: a DBAPI rejecting a connect argument (e.g. an unknown keyword)
            raise self.connect_error(exc) from None
        for info in tables:
            self._build(info)

    def table_warnings(self, info: TableInfo) -> list[str]:
        """Warnings found while building the table (e.g. SQL Server indexes that were skipped)."""
        return list(self._get(info).info.get("warnings", ()))

    def _check_creatable(self, info: TableInfo, table: Table) -> None:
        error = table.info.get("error")
        if error:
            raise WriteError(info.name, SchemaError(error), url=self._engine.url)

    def create_table(self, info: TableInfo) -> None:
        """``full`` DDL for one table: drop it if it exists, then create it and its indexes.

        Raises ``WriteError`` (message redacted) on failure; other target tables are untouched.
        A table not passed to :meth:`prepare` is built (and its index names reserved) here.
        """
        table = self._get(info)
        self._check_creatable(info, table)
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

    def ensure_table(self, info: TableInfo) -> bool:
        """``append``/``sync`` DDL: create the table if missing, else check it for schema drift.

        Returns True when the table was created. Never drops anything. Raises ``WriteError``
        (``schema drift: ...``) when the existing table cannot receive the source columns.
        """
        table = self._get(info)
        try:
            with self._engine.begin() as conn:
                insp = inspect(conn)
                if not insp.has_table(table.name, schema=self.schema):
                    self._check_creatable(info, table)
                    table.create(conn)
                    log.debug("created missing table", extra={"kv": {"table": info.name}})
                    return True
                target_columns = insp.get_columns(table.name, schema=self.schema)
        except sa_exc.SQLAlchemyError as exc:
            raise WriteError(info.name, exc, url=self._engine.url) from exc
        drift = schema_drift(table, target_columns)
        if drift:
            raise WriteError(info.name, SchemaError(drift), url=self._engine.url)
        return False

    def _table(self, name: str) -> Table:
        table = self._tables.get(name)
        if table is not None:
            return table
        for key, t in self._tables.items():
            if key.casefold() == name.casefold():
                return t
        insp = inspect(self._engine)
        if not insp.has_table(name, schema=self.schema):
            raise TableNotFoundError(f"Table {name!r} not found in target {self.url_masked}")
        return Table(
            quoted_name(name, quote=True),
            MetaData(schema=self.schema),
            autoload_with=self._engine,
        )

    # data

    def write_batches(self, info: TableInfo, batches: Iterable[list[tuple]]) -> int:
        """Insert all batches in one transaction; on error roll back and raise ``WriteError``."""
        table = self._get(info)
        try:
            with self._engine.begin() as conn:
                return _load(conn, table, batches, row_guard(table, self.dialect))
        except Exception as exc:  # roll-back already happened in engine.begin()
            raise WriteError(info.name, exc, url=self._engine.url) from exc

    def replace_rows(self, info: TableInfo, batches: Iterable[list[tuple]]) -> tuple[int, int]:
        """Delete every target row, then insert all batches; one transaction.

        Returns ``(deleted, inserted)``. Used by ``sync`` with ``--no-key replace``.
        """
        table = self._get(info)
        try:
            with self._engine.begin() as conn:
                deleted = conn.execute(table.delete()).rowcount
                inserted = _load(conn, table, batches, row_guard(table, self.dialect))
        except Exception as exc:
            raise WriteError(info.name, exc, url=self._engine.url) from exc
        return max(deleted or 0, 0), inserted

    def sync(self, info: TableInfo, key: Sequence[str]) -> SyncSession:
        """A :class:`SyncSession` upserting ``info``'s rows by ``key`` (a context manager)."""
        from acade2sql.writers.sync import SyncSession

        table = self._get(info)
        native = not self._force_generic_sync and self.dialect in NATIVE_UPSERT_DIALECTS
        return SyncSession(
            self._engine,
            table,
            tuple(key),
            native=native,
            guard=row_guard(table, self.dialect),
            table_label=info.name,
            url=self._engine.url,
            column_types=info.column_types,
        )

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
            yield from fetch_batches(conn, stmt, batch_size)


NATIVE_UPSERT_DIALECTS = frozenset({"sqlite", "postgresql"})


def fetch_batches(conn: Connection, stmt: Any, batch_size: int) -> Iterator[list[tuple]]:
    """Stream ``stmt``'s rows on ``conn`` as lists of tuples (``fetchmany``)."""
    result = conn.execution_options(stream_results=True).execute(stmt)
    while True:
        rows = result.fetchmany(batch_size)
        if not rows:
            break
        yield [tuple(r) for r in rows]


def _load(conn: Connection, table: Table, batches: Iterable[list[tuple]], guard: RowGuard) -> int:
    """Insert ``batches`` on ``conn`` (inside the caller's transaction); returns the row count."""
    total = 0
    insert, close = inserter(conn, table)
    try:
        for batch in batches:
            if batch:
                guard(batch)
                insert(batch)
                total += len(batch)
    finally:
        close()
    return total


def _noop() -> None:
    return None


def inserter(
    conn: Connection, table: Table
) -> tuple[Callable[[list[tuple]], None], Callable[[], None]]:
    """Return ``(insert, close)``: ``insert`` adds one batch of positional tuples to ``table``.

    SQLite fast path: the INSERT is compiled once by SQLAlchemy (dialect quoting, bound
    parameters) and the tuples go straight to the DBAPI cursor's ``executemany``, after applying
    each column's SQLAlchemy bind processor. Every other dialect (and any non-positional
    compilation) uses ``conn.execute(table.insert(), list_of_dicts)`` (on mssql+pyodbc with
    ``fast_executemany``).
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
