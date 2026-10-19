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
