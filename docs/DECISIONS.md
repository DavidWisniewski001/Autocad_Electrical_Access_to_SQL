# Design decisions

Each entry: decision — reason. Newest last.

1. **SQLAlchemy 2.x for writing** — one dialect-aware layer for SQLite/MSSQL/PostgreSQL; identifier
   quoting and DDL compilation come from the dialect instead of string concatenation.
2. **pyodbc + "Microsoft Access Driver (*.mdb, *.accdb)" as primary reader; `access-parser` fallback
   (M5)** — the driver is the most faithful reader; the fallback covers machines without it.
3. **Sources opened with `ReadOnly=1` and `pyodbc.connect(readonly=True)`** — M1 verified no lock file
   is left and size/mtime are unchanged. A `copy_to_temp` option is available for paranoid use.
4. **Table/column names preserved verbatim, quoted per dialect; no sanitising** — sanitising collides
   `ALLEN BRADLEY`/`ALLEN-BRADLEY` and `FERRAZ SHAWMUT`/`FERRAZ-SHAWMUT` (M1).
5. **Text copied verbatim** (no trim/case-fold/numeric coercion) — 2,674 padded values exist and
   numeric-looking identifiers (e.g. `PNEU-ACT.CATALOG`) must stay text.
6. **PK discovery order: unique index named `PrimaryKey` from `statistics()` → single COUNTER column
   (inferred) → none** — the driver does not implement `SQLPrimaryKeys` (M1). `TableInfo` records the
   source of the PK (`discovered` / `inferred` / `none`).
7. **RECNUM (COUNTER) values copied as-is and used as target PK and default sync key** — the only column
   unique in all 285 sample tables; natural keys become non-unique indexes.
8. **MEMO/LONGCHAR → unbounded text** (`TEXT` / `NVARCHAR(MAX)` / `TEXT`) — declared size 1 GB.
9. **Empty tables are created (schema only)** — fidelity; filters can exclude them.
10. **Metadata-driven conversion, no per-table code** — catalog has 43 column shapes, footprint 2.
11. **Driver-reported nullability is unreliable; PK members forced NOT NULL** (M2) — the Access driver
    reports `nullable=1` for `NOT NULL` text columns (verified on synthetic .mdb/.accdb). Columns of a
    discovered `PrimaryKey` index are set `nullable=False` (Access forbids NULL in PKs); COUNTER is
    reported correctly. Other `Required` columns cannot be detected via ODBC.
12. **Values are passed through as pyodbc returns them** (M2) — GUID is the unbraced upper-case string
    (`12345678-...`), YESNO is `bool` (Jet stores a NULL insert as False, so it never reads as NULL),
    LONGBINARY is normalised to `bytes`.
13. **MSys* tables are listed with `include_system=True` but are not resolvable by name** (M2) — the
    driver denies reading them ("no read permission"); `get_table`/`count_rows`/`iter_rows` raise
    `TableNotFoundError`.
14. **`ReadOnly=1;Exclusive=0` still creates `<name>.ldb` next to the source while connected** (M2) —
    removed on close (also for .accdb). Only `Exclusive=1` avoids it, but that locks out other users.
    Kept `Exclusive=0`; use `copy_to_temp=True` when even a transient lock file is unacceptable.
15. **SQLite load tuning: `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` on every connection**
    (M3, event listener in the writer) — fast bulk load while staying crash-consistent (a power loss
    can lose the last commits, never corrupt). The `-wal`/`-shm` files are checkpointed and removed
    when the writer closes; a later *read-only* client can leave them behind, which is harmless.
16. **Bulk insert fast path (SQLite only): SQLAlchemy-compiled INSERT + DBAPI `executemany` with
    positional tuples** (M3) — the INSERT text comes from `table.insert().compile(dialect)` (dialect
    quoting, bound parameters), each column's SQLAlchemy bind processor is applied in Python, then the
    tuples go to `connection.connection.dbapi_connection.cursor().executemany`. Measured on `MS`
    (269,772 rows, 19 cols, 1 index): 54k rows/s vs 26k rows/s for `conn.execute(insert,
    list_of_dicts)`. Restricted to `dialect.name == "sqlite"` (review gate 1): every other dialect
    uses `conn.execute(table.insert(), list_of_dicts)` until its own fast path (e.g. mssql
    `fast_executemany`) is measured in M4.
17. **Exact decimals on SQLite are stored as TEXT with declared type `DECIMAL_TEXT`** (M3) — the name
    keeps TEXT affinity (contains "TEXT") and is registered in the writer engine's (instance-level)
    `ischema_names`, so a reflected table (e.g. a fresh writer's `read_rows`) still returns `Decimal`.
18. **The implementation module is `acade2sql.converter`; `acade2sql.convert` is the function**
    (M3, revised at review gate 1) — `from acade2sql import convert, resolve_target` gives the plain
    functions. There is no module named `acade2sql.convert` (the earlier callable-module hack was
    removed); patch `acade2sql.converter.<name>` in tests.
19. **Index names are reserved database-wide, including indexes of unrelated target tables** (M3) —
    `prepare` seeds `used_index_names` from the target's existing indexes on tables not being
    converted, so a colliding name gets the hash suffix instead of failing `CREATE INDEX`.
20. **Errors before vs. after the target is modified** (M3, revised at review gate 1) — bad source,
    missing driver, unknown table names, bad target URL (`ArgumentError`, `ValueError`, `TypeError`,
    `NoSuchModuleError`, missing DBAPI module), unreachable target, uncreatable folder: raised before
    any DDL (`TargetError` for target problems), CLI exit 1, no traceback, no report.
    `SqlAlchemyWriter.prepare` only builds the MetaData and reserves index names; each table is
    dropped/created by `create_table(info)` right before it is loaded. From then on every failure is
    per table: a DDL, read or write failure becomes `status="error"` with a redacted message prefixed
    `read failed:` (Access side, incl. a failing source batch iterator) or `write failed:` (target
    side, incl. DDL), and the run continues. The report is always written when `report_path` is
    given; the CLI exits 3 if any table is not `pass`. An empty selection prints
    `warning: no tables selected` to stderr and exits 0.
21. **An unmapped Access type fails the table** (review gate 1) — the column is still created as
    unbounded text (WARNING logged), but `TableResult.status` is `fail` with
    `unmapped type <T> in column <C> stored as text` in `TableResult.warnings`, even when counts and
    checksums match: text storage of an unknown type is not a verified conversion. Fix by adding the
    type to `typemap.TYPE_MAP`/`ALIASES`. Integer additions: `BIGINT`→BigInteger, `TINYINT`→BYTE
    (SmallInteger), `INT`→INTEGER.
22. **Secrets are redacted wherever a target URL or its errors can surface** (review gate 1) —
    `acade2sql.redact.mask_url` hides the userinfo password, every query parameter whose key matches
    `(?i)(pass|pwd|secret|token|key)`, and `PWD=`/`PASSWORD=` values (braced or plain) inside
    `odbc_connect`; `redact(text, url)` removes the raw and URL-encoded secret values of `url` plus
    any `scheme://user:pw@`, `PWD=` or `PWD%3D` value found in the text. Used for the report target,
    log lines, `WriteError`/`TargetError` messages and the CLI `error:` line; target exceptions are
    raised `from None` so `--verbose` tracebacks do not chain the unredacted original.
23. **`pyodbc.pooling = False` at reader import** (requirement 10) — a pooled ODBC connection would
    keep the Access file and its `.ldb` open after `close()`.
24. **`resolve_target` builds SQLite URLs with `URL.create("sqlite", database=...)`** (review gate 1)
    — the path is escaped (`%` → `%25`) and never URL-decoded, so `a%20b.sqlite` opens exactly that
    file. Reports and logs show the plain path (`mask_url`).
25. **Sync compares in Python and UPDATEs only changed rows** (M4, `writers/sync.py`) — per source
    batch the target rows with the batch's keys are fetched (single key: bound `IN`; composite
    key: row-value `IN` on SQLite/PostgreSQL, OR-of-ANDs on SQL Server, which has no row-value
    `IN`; chunks of at most 2,000 parameters, under SQL Server's 2,100 limit). Rows and keys are
    compared with the checksum's column-aware normalisation (#36: `''` != NULL, padding and case
    significant, Decimal scale ignored, GUID case/braces ignored only in GUID columns, REAL at
    float32), so `unchanged` is exact and a second identical sync writes nothing (`updated=0`).
    Source keys are checked unique and non-NULL in Python while streaming (`SyncKeyError` → whole
    table rolled back). *Collation guard* (review gate 2): the lookup runs under the target's
    collation; a returned target row whose key equals no source key of the batch in Python (e.g.
    `m1` vs `M1` under NOCASE / SQL Server `_CI_`, or `m1` vs `m1 ` under RTRIM / padding) means the
    target merges keys the source distinguishes → `SyncKeyError` "target key 'm1' matches source
    key 'M1' under the target's collation; keys that differ only by case/trailing spaces can't be
    synced; re-run with --mode full", table `error`, target unchanged. Two target rows for one key
    → `SyncKeyError` (key not unique in the target). The opposite case (Python folds what the
    target distinguishes: a GUID key stored as `{guid}` in a SQLite text column, a SQLite
    `DECIMAL_TEXT` key `1.5` vs source `1.50`) is not an error: the lookup misses, the source row
    is inserted under its own key and `--delete-missing` removes the old one (`inserted=1
    deleted=1`). *Pre-commit verification* (same connection, before commit; failure →
    `WriteError` `sync verification failed: ...`, rolled back): the table grew by exactly
    `inserted` rows; a generic `UPDATE` executemany hit exactly one row per changed source row
    (checked when the DBAPI reports a sane rowcount ≥ 0; mssql `fast_executemany` may report -1 →
    skipped); after delete-missing, the target rows matching the temp key table number exactly the
    source keys. So the counters are exact on both paths, or the table is `error`.
26. **Native vs generic sync** (M4) — the generic path (SELECT + `UPDATE` executemany + INSERT,
    the M3 SQLite fast path for INSERT) runs everywhere and is the only path on SQL Server. On
    SQLite/PostgreSQL the changed + new rows are written with one `INSERT ... ON CONFLICT (key) DO
    UPDATE` instead, but only when the target has a PK/unique constraint/unique index on exactly the
    key columns that is not partial (`WHERE`) and whose columns use the default collation (SQLite:
    `pragma_index_list`/`pragma_index_xinfo`, `coll` must be BINARY, an `INTEGER PRIMARY KEY` rowid
    alias qualifies; PostgreSQL: `pg_index.indpred IS NULL` and `indcollation` only 0/default,
    queried with bound parameters; a catalog error → generic). Otherwise generic. If the target
    still rejects the statement with an `ON CONFLICT` OperationalError/ProgrammingError on its first
    execution, the table falls back to the generic path (INFO log; PostgreSQL runs that first
    execution in a SAVEPOINT; SQLite needs none because the error is raised while preparing, and a
    pysqlite SAVEPOINT could run outside the transaction). Counting is identical on both paths
    because the comparison happens before writing, and the pre-commit check (#25) proves it.
    `SqlAlchemyWriter(_force_generic_sync=True)` (also settable as an instance/class attribute)
    forces the generic path.
27. **A temporary key table per synced table** (M4) — every source key is inserted into
    `TEMPORARY` table `acade2sql_keys_<random>` (SQL Server: `#acade2sql_keys_<random>`, string
    columns `COLLATE DATABASE_DEFAULT` against tempdb collation conflicts; a PK only when
    `mssql_key_problem(key, 900)` is None, else a heap) on the session's single connection. It
    serves `--delete-missing` (`DELETE ... WHERE NOT EXISTS (SELECT ... FROM keys WHERE t.k =
    keys.k)`, target column on the left so SQLite applies the target column's collation, as the
    lookup does; no NOT IN lists, verified with 60k and 126k keys), the pre-commit check and
    validation:
    `count(target rows whose key is in the temp table) == source_rows` always; with `--checksum`
    the checksum of exactly those target rows is compared with the source checksum, so extra
    target rows do not fail a sync without `--delete-missing` (they are reported as
    `extra_rows`). With `--delete-missing` also `target_rows == source_rows`. Load +
    delete-missing are one transaction; the temp table is dropped when the session ends.
28. **Append/sync never drop; schema drift fails the table** (M4) — an existing table is reflected;
    a source column missing from it (exact name) or a target NOT NULL column without default/
    identity that the source lacks → `write failed: schema drift: ... (re-run with --mode full)`.
    Extra nullable target columns are kept (NULL for inserted rows, untouched on update). A missing
    table is created with its indexes. In append/sync mode `prepare` reserves the index names of
    *all* existing target tables. Append does not compute checksums (warning
    `checksum not computed in append mode`). Append reads `target_rows_before` outside the insert
    transaction (single-writer assumption: a concurrent writer can make the count check fail but
    cannot corrupt data). A table created by `ensure_table` stays (empty) if its load then rolls
    back.
29. **Keyless tables in sync** (M4) — key = `--key TABLE=...` > global `--key COL,...` (only for
    tables having all those columns, matched case-insensitively) > the table's primary key.
    None → `--no-key error` (default; message `no key for sync; use --key or --no-key replace`) or
    `--no-key replace` (DELETE all + INSERT in one transaction; counters `deleted`/`inserted`).
    A `--key TABLE=` naming a table that is not converted logs a warning.
30. **SQL Server index limits are checked at build time** (M4) — byte sizes per type (NVARCHAR(n)
    = 2n, VARCHAR(n) = n, integer 2/4/8, DATETIME2/FLOAT 8, REAL 4, NUMERIC 5-17, BIT 1,
    UNIQUEIDENTIFIER 16, (max) types unindexable). A secondary index with a (max) column or
    > 1700 bytes is skipped with a `TableResult.warnings` entry (`index X skipped on SQL Server:
    ...`); a PK with a (max) column or > 900 bytes makes the table `error` before any DDL. On
    default_cat no index is affected (largest key: 288 bytes).
31. **PostgreSQL pre-flight and NUL** (M4) — table/column/schema names > 63 UTF-8 bytes raise
    `SchemaError` (exit 1) after the writer is opened and before `prepare` (no DDL); `--dry-run`
    checks it too. Text values containing `\x00` are rejected per batch before they reach the
    server: `write failed: NUL character in text column <C> (not supported by PostgreSQL)`.
    `WriteError.detail` is the plain message when the cause is an `Acade2SqlError`.
32. **`--dry-run` uses `create_mock_engine`** (M4, `plan.py`) — no engine, connection, DBAPI import
    or file creation; DDL is compiled by the URL's dialect (`DROP TABLE IF EXISTS` on all three
    dialects, like the real run's checkfirst drop). Statuses are `planned` (counted as ok);
    planning errors (mssql PK too large, no sync key, unknown `--key` column) are `error`; a table
    with an unmapped type is `fail` (#21), so the dry run exits 3 as the real run would. `key` is
    None (JSON `null`) for a table without a sync key, as in the real run.
    Unsupported dialects → `TargetError`. `--no-counts` (API `counts=False`) skips source counts
    and is a usage error without `--dry-run`.
33. **Option errors are `UsageError(ValueError)`** (M4) — invalid mode; `--key`, `--delete-missing`
    or `--no-key` (API: `no_key != "error"`) without sync; `--schema` with SQLite; a `--schema` that
    is empty/whitespace or contains `.`, `[`, `]`, `"`, a backtick or a control character (it is
    quoted as one identifier; the message names the character); `--no-counts` without
    `--dry-run`: raised by `convert`
    before the source or target is opened; the CLI prints `error: ...` and exits 2. A missing DBAPI
    module → `TargetError` naming `pip install acade2sql[postgresql]` (or, for mssql, pyodbc and
    "ODBC Driver 18 for SQL Server"); an ODBC "IM002"/driver-not-found connect error on mssql gets
    the same driver hint.
34. **mssql+pyodbc engines use `fast_executemany=True`; server targets get a 15 s connect
    timeout** (M4) — PostgreSQL with a libpq driver (`+psycopg`, `+psycopg2`, or plain
    `postgresql://` resolving to one of them): `connect_args={"connect_timeout": 15}`; never for
    pg8000, asyncpg or other drivers (they reject the keyword). mssql+pyodbc:
    `connect_args={"timeout": 15}` (ODBC login timeout). Skipped when the URL already has a query
    key containing `timeout` (or `odbc_connect` mentions one): setting any timeout in the URL is
    how the user disables ours. An explicit connect argument overrides `PGCONNECT_TIMEOUT` and a
    DSN's `LoginTimeout`. A `TypeError` from the first connect in `prepare` (a driver rejecting an
    argument) becomes `TargetError`. An unreachable host otherwise hung for more than 60 s before
    `TargetError`.
35. **`--tables`/`--exclude` accept space- and comma-separated names, repeatable** (M4) —
    `nargs="+", action="extend"` with every value split on commas: `--tables A B`,
    `--tables A --tables B` and `--tables A,B` all work (README form kept). The list is greedy, so
    the source path must come before `--tables`/`--exclude`. An empty selection prints
    `warning: no tables selected` once (the API logs it at INFO).
36. **Value normalisation is column-aware** (review gate 2; supersedes the GUID rule of M3_SPEC §3)
    — `validate.normalize_value(v, access_type=None)`, `validate.table_checksum(rows,
    column_types=None)` and `same_row(a, b, column_types=None)` (`validate`, re-exported by
    `writers.sync`) take `ColumnInfo.access_type` values in column order (`TableInfo.column_types`,
    ordinal order; aliases resolved by `typemap.canonical_type`). GUID folding (upper-case, one
    balanced brace pair stripped; `uuid.UUID` values as their upper-case string) applies only to
    GUID columns; every other string compares exactly, so a GUID-shaped value whose case changed
    in a text column is an update and a checksum mismatch. REAL (Single) floats are compared
    rounded to float32 (`struct` `'f'`) on both sides, so PostgreSQL `float4` values returned in
    text format match the pyodbc values. With `column_types=None` (legacy callers) there is no
    GUID or REAL folding at all. Full, sync (rows, keys and the checksum restricted to the source
    keys) and `--no-key replace` pass the types; append computes no checksum.
