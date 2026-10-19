"""``sync`` mode: upsert one table's source rows into the target by key (DECISIONS #25-#28).

One :class:`SyncSession` per table holds one connection. :meth:`SyncSession.load` runs in one
transaction: per source batch it fetches the target rows with the batch's keys (bound
parameters, chunked below SQL Server's 2,100-parameter limit), compares them in Python with the
checksum's per-column value normalisation, UPDATEs only the rows that differ and INSERTs the new
ones. Every source key also goes into a temporary key table, which serves ``--delete-missing``
(``DELETE ... WHERE NOT EXISTS``), the pre-commit verification and the validation queries
restricted to the source keys.

Python and the target may disagree on key equality (a case-insensitive or trailing-space-padding
collation): a target row returned by the key lookup that matches no source key in Python aborts
the table (``SyncKeyError``), and before commit the row counts are verified on the same
connection (``sync verification failed``), so a sync either writes exactly what it reports or
rolls back.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Hashable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from sqlalchemy import (
    Column,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    and_,
    bindparam,
    func,
    inspect,
    literal,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy import exc as sa_exc
from sqlalchemy import types as sat
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.sql.elements import ColumnElement

from acade2sql.errors import Acade2SqlError, SyncKeyError, WriteError
from acade2sql.validate import normalize_value, same_row
from acade2sql.writers.dialects import MSSQL_PK_MAX_BYTES, RowGuard, mssql_key_problem
from acade2sql.writers.sqlalchemy_writer import fetch_batches, inserter

log = logging.getLogger(__name__)

PARAM_LIMIT = 2000  # bound parameters per key lookup (SQL Server allows 2,100 per statement)
_PG_DEFAULT_COLLATION_OID = 100  # pg_collation "default"; 0 = type not collatable

__all__ = ["PARAM_LIMIT", "SyncCounts", "SyncSession", "SyncVerificationError", "same_row"]


class SyncVerificationError(Acade2SqlError):
    """The pre-commit check of a sync failed; the table's transaction is rolled back."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"sync verification failed: {detail}")


@dataclass
class SyncCounts:
    """Row counters of one sync."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


def _show(key: tuple) -> str:
    return repr(key[0]) if len(key) == 1 else repr(key)


def _loose(key: tuple) -> tuple:
    """The key as a case-insensitive, trailing-space-insensitive collation sees it."""
    return tuple(v.rstrip().casefold() if isinstance(v, str) else v for v in key)


def _temp_type(type_: sat.TypeEngine, dialect: str) -> sat.TypeEngine:
    t = type_.copy()
    if dialect == "mssql" and isinstance(t, sat.String):
        t.collation = "DATABASE_DEFAULT"  # tempdb may have another collation than the database
    return t


def _is_on_conflict_error(exc: sa_exc.DBAPIError) -> bool:
    return "ON CONFLICT" in str(getattr(exc, "orig", None) or exc).upper()


class SyncSession:
    """Upsert rows of ``table`` by ``key`` on one connection. Use as a context manager.

    ``column_types``: ``ColumnInfo.access_type`` of each table column, in column order; it
    selects the per-column comparison (GUID folding, REAL at float32; see
    :func:`acade2sql.validate.normalize_value`). None compares every value exactly.
    """

    def __init__(
        self,
        engine: Engine,
        table: Table,
        key: Sequence[str],
        *,
        native: bool,
        guard: RowGuard,
        table_label: str,
        url: URL | None = None,
        column_types: Sequence[str] | None = None,
    ) -> None:
        names = [c.key for c in table.columns]
        missing = [k for k in key if k not in names]
        if not key or missing:
            raise SyncKeyError(
                f"key column(s) {', '.join(missing) or '(none)'} not found in table {table_label}"
            )
        if column_types is not None and len(column_types) != len(names):
            raise ValueError(f"{len(column_types)} column types for {len(names)} columns")
        self._engine = engine
        self._table = table
        self._key = tuple(key)
        self._kpos = [names.index(k) for k in key]
        self._kcols = [table.c[k] for k in key]
        self._non_key = [c for c in table.columns if c.key not in self._key]
        self._types = tuple(column_types) if column_types is not None else None
        self._key_types: tuple[str | None, ...] = (
            tuple(self._types[i] for i in self._kpos) if self._types else (None,) * len(key)
        )
        self._native_requested = native
        self._guard = guard
        self._label = table_label
        self._url = url
        self._conn: Connection | None = None
        self._temp: Table | None = None
        self._native: Any | None = None
        self._native_checked = False
        self.native_used = False

    # lifecycle

    def __enter__(self) -> SyncSession:
        self._conn = self._engine.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            self._drop_temp(conn)
        finally:
            conn.close()
            self._conn = None

    @property
    def conn(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("SyncSession used outside its 'with' block")
        return self._conn

    @property
    def _dialect(self) -> str:
        return self.conn.dialect.name

    # key identity

    def _norm_key(self, values: tuple) -> Hashable:
        """Hashable key identity, using the checksum's per-column normalisation."""
        if len(values) == 1 and type(values[0]) is int:
            return values[0]
        return tuple(normalize_value(v, t) for v, t in zip(values, self._key_types, strict=True))

    # temp key table

    def _make_temp(self) -> Table:
        dialect = self._dialect
        suffix = uuid.uuid4().hex[:12]
        cols = [
            Column(f"k{i}", _temp_type(c.type, dialect), nullable=False)
            for i, c in enumerate(self._kcols)
        ]
        md = MetaData()
        if dialect == "mssql":
            temp = Table(f"#acade2sql_keys_{suffix}", md, *cols)
            # The PK is a clustered index: only within SQL Server's 900-byte limit and without
            # (max) columns; otherwise a heap (correctness does not depend on the index).
            indexable = mssql_key_problem(list(temp.columns), MSSQL_PK_MAX_BYTES) is None
        else:
            temp = Table(f"acade2sql_keys_{suffix}", md, *cols, prefixes=["TEMPORARY"])
            indexable = True
        if indexable:
            temp.append_constraint(PrimaryKeyConstraint(*temp.columns))
        return temp

    def _drop_temp(self, conn: Connection) -> None:
        if self._temp is None:
            return
        try:
            if conn.in_transaction():
                conn.rollback()
            with conn.begin():
                self._temp.drop(conn)
        except sa_exc.SQLAlchemyError as exc:  # already gone (rolled back / connection lost)
            log.debug("temp key table not dropped", extra={"kv": {"error": type(exc).__name__}})
        self._temp = None

    def _in_source(self) -> ColumnElement[bool]:
        """EXISTS (the target row's key is in the temp table of source keys).

        The target column is on the left so that SQLite compares with the target column's
        collation, as the key lookup and the target's unique index do.
        """
        if self._temp is None:
            raise RuntimeError("load() has not run")
        temp = self._temp
        cond = and_(*(c == temp.c[f"k{i}"] for i, c in enumerate(self._kcols)))
        return select(literal(1)).select_from(temp).where(cond).correlate(self._table).exists()

    # key lookup

    def _key_filter(self, keys: list[tuple]) -> ColumnElement[bool]:
        if len(self._kcols) == 1:
            return self._kcols[0].in_([k[0] for k in keys])
        if self._dialect != "mssql":
            return tuple_(*self._kcols).in_(keys)
        # SQL Server has no row-value IN: OR of ANDs (bounded by PARAM_LIMIT parameters)
        return or_(*(and_(*(c == v for c, v in zip(self._kcols, k, strict=True))) for k in keys))

    def _fetch_existing(self, keys: list[tuple]) -> dict[Hashable, tuple]:
        """Target rows with one of ``keys``, by normalised key.

        Raises ``SyncKeyError`` when the target returns a row whose key equals none of ``keys``
        in Python (the target's collation treats keys as equal that the source distinguishes)
        or two rows for one key (the key is not unique in the target).
        """
        wanted = {self._norm_key(k) for k in keys}
        out: dict[Hashable, tuple] = {}
        chunk = max(1, PARAM_LIMIT // len(self._kcols))
        cols = list(self._table.columns)
        for start in range(0, len(keys), chunk):
            stmt = select(*cols).where(self._key_filter(keys[start : start + chunk]))
            for row in self.conn.execute(stmt):
                values = tuple(row)
                tkey = tuple(values[i] for i in self._kpos)
                nk = self._norm_key(tkey)
                if nk not in wanted:
                    raise SyncKeyError(self._collation_message(tkey, keys))
                if nk in out:
                    raise SyncKeyError(
                        f"the target has more than one row with key {_show(tkey)} for sync key "
                        f"({', '.join(self._key)}); the key must be unique in the target"
                    )
                out[nk] = values
        return out

    def _collation_message(self, target_key: tuple, keys: list[tuple]) -> str:
        loose = _loose(target_key)
        match = next((k for k in keys if _loose(k) == loose), None)
        source = f"source key {_show(match)}" if match is not None else "a source key"
        return (
            f"target key {_show(target_key)} matches {source} under the target's collation; "
            "keys that differ only by case/trailing spaces can't be synced; re-run with "
            "--mode full"
        )

    def _check_keys(self, keys: list[tuple], seen: set[Hashable]) -> list[Hashable]:
        normed = []
        cols = ", ".join(self._key)
        for k in keys:
            if any(v is None for v in k):
                raise SyncKeyError(f"NULL value in sync key ({cols}) of source row with key {k!r}")
            nk = self._norm_key(k)
            if nk in seen:
                raise SyncKeyError(f"duplicate source key {k!r} for sync key ({cols})")
            seen.add(nk)
            normed.append(nk)
        return normed

    # native upsert selection (runs before the load transaction)

    def _native_insert(self) -> Any | None:
        """``INSERT ... ON CONFLICT (key) DO UPDATE`` when the target has a usable unique key."""
        if not self._native_requested:
            return None
        dialect = self.conn.dialect
        if isinstance(dialect, SQLiteDialect):
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif isinstance(dialect, PGDialect):
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            return None
        try:
            unique = self._key_is_unique_in_target()
        except sa_exc.SQLAlchemyError as exc:
            log.info(
                "cannot inspect target unique keys, generic sync",
                extra={"kv": {"table": self._label, "error": type(exc).__name__}},
            )
            unique = False
        finally:
            if self.conn.in_transaction():
                self.conn.rollback()
        if not unique:
            log.debug(
                "no usable unique key in target, generic sync", extra={"kv": {"t": self._label}}
            )
            return None
        stmt = dialect_insert(self._table)
        if not self._non_key:
            return stmt.on_conflict_do_nothing(index_elements=self._kcols)
        return stmt.on_conflict_do_update(
            index_elements=self._kcols,
            set_={c: stmt.excluded[c.key] for c in self._non_key},
        )

    def _key_is_unique_in_target(self) -> bool:
        """A PK / unique constraint / unique index on exactly the key columns that ON CONFLICT
        can use: not partial, and no column with an explicit non-default collation."""
        if isinstance(self.conn.dialect, SQLiteDialect):
            return self._sqlite_unique_key()
        insp = inspect(self.conn)
        name, schema = self._table.name, self._table.schema
        wanted = set(self._key)
        candidates: list[str | None] = []
        pk = insp.get_pk_constraint(name, schema=schema)
        if set(pk.get("constrained_columns") or ()) == wanted:
            candidates.append(pk.get("name"))
        for uc in insp.get_unique_constraints(name, schema=schema):
            if set(uc.get("column_names") or ()) == wanted:
                candidates.append(uc.get("name"))
        for ix in insp.get_indexes(name, schema=schema):
            options = ix.get("dialect_options") or {}
            if (
                ix.get("unique")
                and set(ix.get("column_names") or ()) == wanted
                and not any(options.get(o) for o in ("postgresql_where", "sqlite_where"))
            ):
                candidates.append(ix.get("name"))
        if isinstance(self.conn.dialect, PGDialect):
            return any(n is not None and self._pg_index_usable(n) for n in candidates)
        return bool(candidates)

    def _sqlite_unique_key(self) -> bool:
        conn = self.conn
        name = self._table.name
        wanted = set(self._key)
        indexes = conn.execute(
            text('SELECT name, "unique", origin, partial FROM pragma_index_list(:t)'),
            {"t": name},
        ).all()
        if not any(origin == "pk" for _n, _u, origin, _p in indexes):
            # INTEGER PRIMARY KEY (rowid alias) has no index entry: usable when it is the key
            pk = inspect(conn).get_pk_constraint(name).get("constrained_columns") or []
            if set(pk) == wanted:
                return True
        for ix_name, unique, _origin, partial in indexes:
            if not unique or partial:
                continue
            cols = conn.execute(
                text("SELECT name, coll FROM pragma_index_xinfo(:i) WHERE key = 1"),
                {"i": ix_name},
            ).all()
            if any(c is None for c, _coll in cols):  # expression index
                continue
            if {c for c, _coll in cols} == wanted and all(
                (coll or "BINARY").upper() == "BINARY" for _c, coll in cols
            ):
                return True
        return False

    def _pg_index_usable(self, index_name: str) -> bool:
        """The PostgreSQL index exists, is not partial and uses only default collations."""
        prep = self.conn.dialect.identifier_preparer
        qualified = prep.quote_identifier(index_name)
        if self._table.schema:
            qualified = prep.quote_identifier(self._table.schema) + "." + qualified
        row = self.conn.execute(
            text(
                "SELECT x.indpred IS NULL AND NOT EXISTS ("
                " SELECT 1 FROM unnest(x.indcollation::oid[]) AS c(coll)"
                " WHERE c.coll NOT IN (0, :default_coll))"
                " FROM pg_catalog.pg_index x"
                " WHERE x.indexrelid = to_regclass(CAST(:ix AS text))"
            ),
            {"ix": qualified, "default_coll": _PG_DEFAULT_COLLATION_OID},
        ).first()
        return bool(row is not None and row[0])

    # writes

    def _update_stmt(self) -> tuple[Any, list[str]]:
        taken = {c.key for c in self._table.columns}
        binds = []
        for i in range(len(self._kcols)):
            b = f"_acade2sql_k{i}"
            while b in taken:
                b += "_"
            binds.append(b)
        where = and_(
            *(c == bindparam(b, type_=c.type) for c, b in zip(self._kcols, binds, strict=True))
        )
        return self._table.update().where(where), binds

    def load(self, batches: Iterable[list[tuple]], delete_missing: bool = False) -> SyncCounts:
        """Upsert all ``batches`` in one transaction; optionally delete target rows not in them.

        Before commit the result is verified on the same connection: the table grew by exactly
        the inserted rows, every UPDATE hit exactly its row, and exactly one target row matches
        each source key. Raises ``WriteError`` (transaction rolled back) on any failure,
        including NULL or duplicate source keys, a target collation that merges distinct keys,
        and a failed verification (``sync verification failed: ...``).
        """
        conn = self.conn
        if conn.in_transaction():
            conn.rollback()
        try:
            native = self._native_insert()  # catalog queries; leaves no transaction open
            with conn.begin():
                counts, n_keys = self._load(conn, batches, native)
                if delete_missing:
                    counts.deleted = self._delete_missing(conn)
                self._verify_keys(n_keys)
        except WriteError:
            raise
        except Exception as exc:
            raise WriteError(self._label, exc, url=self._url) from exc
        log.debug(
            "synced table",
            extra={"kv": {"table": self._label, "native": self.native_used, **counts.__dict__}},
        )
        return counts

    def _load(
        self, conn: Connection, batches: Iterable[list[tuple]], native: Any | None
    ) -> tuple[SyncCounts, int]:
        counts = SyncCounts()
        self._temp = self._make_temp()
        self._temp.create(conn)
        self._native = native
        self._native_checked = False
        self.native_used = native is not None
        rows_before = self.count_all()
        insert_new, close_new = inserter(conn, self._table)
        insert_keys, close_keys = inserter(conn, self._temp)
        seen: set[Hashable] = set()
        try:
            for batch in batches:
                if not batch:
                    continue
                self._guard(batch)
                keys = [tuple(row[i] for i in self._kpos) for row in batch]
                normed = self._check_keys(keys, seen)
                existing = self._fetch_existing(keys)
                new: list[tuple] = []
                changed: list[tuple] = []
                for row, nk in zip(batch, normed, strict=True):
                    old = existing.get(nk)
                    if old is None:
                        new.append(row)
                    elif not self._non_key or same_row(row, old, self._types):
                        counts.unchanged += 1
                    else:
                        changed.append(row)
                self._write(conn, changed, new, insert_new)
                insert_keys(keys)
                counts.inserted += len(new)
                counts.updated += len(changed)
        finally:
            close_new()
            close_keys()
        added = self.count_all() - rows_before
        if added != counts.inserted:
            raise SyncVerificationError(
                f"the table gained {added} rows but {counts.inserted} were counted as inserted "
                "(the target matched keys differently than the comparison)"
            )
        return counts, len(seen)

    def _write(
        self,
        conn: Connection,
        changed: list[tuple],
        new: list[tuple],
        insert_new: Any,
    ) -> None:
        if not (changed or new):
            return
        if self._native is not None:
            try:
                self._write_native(conn, changed + new)
                return
            except (sa_exc.OperationalError, sa_exc.ProgrammingError) as exc:
                if self._native_checked or not _is_on_conflict_error(exc):
                    raise
                log.info(
                    "native upsert rejected by the target, generic sync",
                    extra={"kv": {"table": self._label, "error": type(exc).__name__}},
                )
                self._native = None
                self.native_used = False
        self._write_generic(conn, changed, new, insert_new)

    def _write_native(self, conn: Connection, rows: list[tuple]) -> None:
        col_keys = [c.key for c in self._table.columns]
        params = [dict(zip(col_keys, r, strict=True)) for r in rows]
        if self._native_checked or conn.dialect.name != "postgresql":
            # SQLite rejects a non-matching ON CONFLICT when preparing the statement: nothing
            # ran and the transaction is intact. (No SAVEPOINT: pysqlite may issue it before
            # its implicit BEGIN, and RELEASE would then commit.)
            conn.execute(self._native, params)
        else:  # PostgreSQL aborts the transaction on error: probe inside a savepoint
            with conn.begin_nested():
                conn.execute(self._native, params)
        self._native_checked = True

    def _write_generic(
        self, conn: Connection, changed: list[tuple], new: list[tuple], insert_new: Any
    ) -> None:
        if changed:
            update, binds = self._update_stmt()
            col_keys = [c.key for c in self._table.columns]
            non_key_pos = [i for i, k in enumerate(col_keys) if k not in self._key]
            params = [
                {
                    **{col_keys[i]: r[i] for i in non_key_pos},
                    **{b: r[p] for b, p in zip(binds, self._kpos, strict=True)},
                }
                for r in changed
            ]
            result = conn.execute(update, params)
            rowcount = result.rowcount
            sane = len(params) == 1 or result.supports_sane_multi_rowcount()
            if sane and rowcount is not None and rowcount >= 0 and rowcount != len(params):
                raise SyncVerificationError(
                    f"UPDATE changed {rowcount} rows for {len(params)} changed source rows"
                )
        if new:
            insert_new(new)

    def _delete_missing(self, conn: Connection) -> int:
        stmt = self._table.delete().where(~self._in_source())
        return max(conn.execute(stmt).rowcount or 0, 0)

    def _verify_keys(self, n_keys: int) -> None:
        """Before commit: exactly one target row per source key (under the target's collation)."""
        matching = self.count_matching()
        if matching != n_keys:
            raise SyncVerificationError(
                f"{matching} target rows match the {n_keys} source keys (duplicate target keys, "
                "or keys the target's collation treats as equal)"
            )

    # validation queries (after load, same connection: the temp table lives there)

    def count_all(self) -> int:
        stmt = select(func.count()).select_from(self._table)
        return int(self.conn.execute(stmt).scalar_one())

    def count_matching(self) -> int:
        """Target rows whose key is one of the source keys."""
        stmt = select(func.count()).select_from(self._table).where(self._in_source())
        return int(self.conn.execute(stmt).scalar_one())

    def read_matching(self, batch_size: int = 5000) -> Iterator[list[tuple]]:
        """Batches of the target rows whose key is one of the source keys (source columns)."""
        stmt = select(*self._table.columns).where(self._in_source())
        yield from fetch_batches(self.conn, stmt, batch_size)
