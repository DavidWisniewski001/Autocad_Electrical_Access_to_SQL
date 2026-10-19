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
37. **Pure reader: access-parser for the TDEF only; rows and values are ours** (M5,
    `readers/access_parser_reader.py`) — access-parser 0.0.6 (license: Apache-2.0, per its
    dist-info `LICENSE` file and the "OSI Approved :: Apache Software License" classifier) parses
    table definitions correctly (`parsing_primitives.parse_table_head`/`parse_table_data`), but its
    value layer is unusable for us: `utils.parse_type` guesses text as UTF-8/Latin-1 with
    `errors="ignore"` and ignores compressed-unicode toggles (MS: 260 rows with U+2013 read as
    `'\x13'`), strips NULs, returns dates/CURRENCY as formatted strings, reads BYTE signed and GUIDs
    big-endian, mis-masks overflow-row offsets (`end & 0xff != 0` is `end & 1`) and silently drops
    rows it cannot parse; `AccessParser()` reads the whole file into memory twice. So nothing of it
    is monkeypatched or subclassed: the reader memory-maps the file read-only (`open(path, "rb")` +
    `mmap.ACCESS_READ`, no lock file, nothing written) and owns the header, the catalog
    (`MSysObjects` rows), the usage map (data pages), row slots (`0x8000` deleted/overflow target
    skipped, `0x4000` followed as an overflow pointer), the null mask, fixed offsets, variable
    offsets (Jet 3 jump tables as mdbtools), LVAL chains and every value conversion. Rows stream page
    by page: nothing is cached between calls. One access-parser bug is corrected: the Jet 4 real-index
    flags byte (unique) is 4 bytes later than access-parser reads it. Verified: every table of both
    sample databases has the same schema (names, order, access_type, data_type, size,
    decimal_digits, nullable, indexes, PK) and the same column-aware checksum as ODBC (168/168,
    117/117; `scripts/compare_readers.py`); pure is faster than ODBC there (MS 14 s vs 24 s
    including the checksum).
38. **Jet 4 compressed unicode: one `0x00` toggles; the first code unit of a run is data** (M5,
    `decode_jet_text`) — raw bytes from Access (fixture UNIRUNS, MS) show `FF FE 41 00 13 20 00 42`
    for `A–B`: in uncompressed mode a single `0x00` at a code-unit boundary switches back (there is
    no `00 00` terminator, as M5_SPEC §1 assumed). Rules: start compressed (byte = U+00xx); `00` →
    uncompressed; the first UTF-16LE unit after a switch is always data (so `00 4E` = U+4E00 is
    not a toggle); later a `00` at a unit boundary switches back, and a `00 00` pair is accepted as
    one switch (the spec's form; Access never writes an empty compressed run mid-value). Without
    `FF FE`: UTF-16LE. Jet 3: the header code page (cp1252 default). All decoding is strict: an odd
    byte count, a lone surrogate or an undefined code-page byte raises `Acade2SqlError` naming the
    table and column (the table becomes `error` in convert); never `ignore`/`replace`.
    Final review finding: Access stores a value *uncompressed* (plain UTF-16LE, no `FF FE`)
    whenever compression would be ambiguous: any `0x00` byte inside a non-Latin-1 code unit (e.g.
    U+4E00), a NUL character, 1-character values and multi-chunk MEMOs. So the defensive branches
    above (first unit after a switch is data, `00 00` as one switch) never trigger on
    Access-written data; they stay as the strict, non-lossy reading of the format.
39. **Pure reader reproduces the ODBC value contract and metadata** (M5) — DATETIME: days since
    1899-12-30, date part truncated toward zero, time = absolute fraction (also before 1899),
    truncated to whole seconds after adding 1e-4 s: measured on the driver (10.9998 s → 10,
    10.9999 s → 11, 0.99999999 days → 23:59:59). BIT from the null mask (never None); BYTE unsigned;
    CURRENCY `Decimal` with 4 decimals; DECIMAL `Decimal` with the column scale (exact digit tuple,
    no context rounding); GUID `UUID(bytes_le=...)` upper-case unbraced; LONGBINARY/BINARY `bytes`;
    NUL characters kept. Metadata: ODBC type names/codes/sizes (VARCHAR size in characters,
    LONGCHAR/LONGBINARY 1073741823, DECIMAL precision/scale from the TDEF); `nullable` as the driver
    reports it (only COUNTER and BIT NOT NULL, discovered PK columns NOT NULL, #11); indexes from the
    TDEF logical indexes (foreign-key reference entries skipped), so PK discovery is identical.
    Catalog objects with flags `0x00030000` (MSysComplexType_* templates) are not listed, as with
    SQLTables. `count_rows` counts live row slots from the usage map; a difference from the TDEF
    row count is an error (#43). Type 9 is `BINARY`/-2 when fixed-length
    (zero-padded, as the driver returns it) and `VARBINARY`/-3 otherwise (verified on a scratch
    .mdb/.accdb); type 10 is `CHAR`/-8 when fixed-length (`CHAR(n)`), else `VARCHAR`/-9, as the
    driver reports them. BIGINT (type 19) is fixed 8-byte little-endian; the variable-length form
    that `CREATE TABLE ... BIGINT` through the ODBC driver produces is decoded as little-endian
    int64 when the column length is 8 (any other length: the table is refused up front, #41). The
    driver itself reports that variable form as `CHAR(4)` and returns the 8 bytes as a 4-character
    string, so this is the one known deliberate difference between the readers (pure is right).
    Rows written before a variable-length column was added (`ALTER TABLE ... ADD COLUMN`) carry no
    variable-length offset table: it is parsed only when the row's column count exceeds the lowest
    variable-length column number (verified on .mdb and .accdb against ODBC). Corrupt offsets
    (not monotonic, past the row data, or a present column without a slot) raise; a value is never
    returned as NULL or `''` in their place. **Dates before 1753-01-01 follow the driver, not Access.** The ODBC driver counts
    back from 1753-01-01 in 365-day years and maps the day of the year with that year's Gregorian
    month lengths (so 31 December of a leap year never occurs): serial -53689 → 1752-12-30
    (proleptic Gregorian, which Access/VBA displays: 1752-12-31), `#01/01/1000#` → 0999-07-02,
    serial -657434 (Access's documented minimum 100-01-01) → 0098-11-26. The pure reader emulates
    this exactly (`_driver_date_before_1753`; 0 mismatches on 21,382 sampled serials from 0098 to
    1753, incl. time parts) so that both readers give the same values (sync across readers, the
    equivalence contract); both therefore differ from what Access displays for such dates. From
    1753-01-01 on the plain proleptic Gregorian conversion is what the driver does. The sample
    databases contain no dates before 1753.
40. **Reader selection and lazy imports** (M5) — `open_reader(path, copy_to_temp=, reader="auto"|
    "odbc"|"pure")`; `auto` → odbc when `acade2sql.readers.diagnose_driver()` (delegates to
    `diagnostics.diagnose_driver` at call time) is ok, else pure with exactly one WARNING (speed
    and how to install the driver). Invalid values → `ValueError` (`convert`: `UsageError`). The
    reader modules are imported lazily, so the pure path never imports pyodbc (works where pyodbc
    or unixODBC is missing). `diagnostics` imports pyodbc only inside `diagnose_driver`; when that
    import fails (`ImportError`/`OSError`) the driver counts as unavailable ("pyodbc not
    importable", with an install hint that needs no pyodbc): `auto` → pure with the warning,
    `doctor` marks the odbc reader unusable, `--reader odbc` raises `DriverNotFoundError`. `Reader.kind` (`"odbc"`/`"pure"`) is recorded as `"reader"` in the
    report JSON and `inspect --json`. access-parser is a core dependency (`>=0.0.6,<0.1`, plus
    `construct`, imported directly for `ConstructError`); the `fallback` extra remains, empty.
41. **What the pure reader refuses** (M5) — `UnsupportedAccessFeature(Acade2SqlError)` with the
    advice to use `--reader odbc` (or, for a password, to remove it: the ODBC reader has no password
    option either): password-protected databases (password field of the RC4-masked header, key
    0x6B39DAC7, Jet 4 XOR-masked with the creation date; verified on a .mdb and an .accdb protected
    with `ALTER DATABASE PASSWORD`), RC4-encoded databases
    (non-zero encoding key), an unreadable catalog page (ACCDB encryption, corruption), and per
    table (`get_table`, so convert marks the table `error`): attachment/multi-value (type 18),
    calculated fields (Jet 4 extended flags `0xC0`), Date/Time Extended (type 20), a
    variable-length BIGINT whose length is not 8, and unknown types (all before the target table is
    created).
    Garbage or empty files, and a file another program holds exclusively (`PermissionError`), raise
    `Acade2SqlError`. Jet 3 (Access 97) decoding follows mdbtools but has no sample file to verify
    it.
42. **CLI and API polish** (M5) — `acade2sql doctor` (Python version/bitness, Access drivers incl.
    other-bitness registry probe, usable readers, psycopg/pyodbc versions, SQL Server ODBC drivers,
    package version; exit 0 when a reader is usable). Progress `[i/n] NAME ROWS rows S.Ss` on
    stderr only, after each table of a real run (`convert(progress=callback)`), on by default when
    stderr is a TTY, `--progress`/`--no-progress`; ROWS is `source_rows` (else `target_rows`).
    `--tables-file`/`--exclude-file`: UTF-8 (BOM tolerated), one name per line, stripped, blank and
    `#` lines ignored, added to `--tables`/`--exclude`; an unreadable file is a usage error (exit
    2), a tables file with no names too. `--checksum --dry-run` prints `warning: --checksum is
    ignored with --dry-run` (exit unchanged). `redact()`: structured positions (userinfo, `PWD=`,
    encoded `PWD%3D`, secret-looking `?key=value` query values) are always masked; free-text
    occurrences of URL secrets only when the secret has ≥ 4 characters; a `PWD=`/`PASSWORD=`
    right after `?`/`&` belongs to the query rule (stops at `&`: `?password=ab&sslmode=require` →
    `?password=***&sslmode=require`), while ODBC `PWD=a&b` is masked up to `;`. Dry-run sync of a
    keyless table under `--no-key error` lists no actions and prints no DDL for it (the real run
    stops before touching the target); when no statement remains the DDL section reads `-- DDL:
    none (no table would be created)`. `doctor` lists every SQL Server ODBC driver but only "ODBC
    Driver 17/18 for SQL Server" count as ok; legacy ones (the built-in `SQL Server`, Native
    Client) are shown as "not supported by the writer (install ODBC Driver 17 or 18 for SQL
    Server)". `python -m acade2sql` runs the CLI. `scripts/compare_readers.py` exits 0 (identical),
    1 (differences found) or 2 (bad arguments, or a source missing/unopenable; the other sources are
    still compared and `--json` is still written).
    `UsageError`, `SchemaError`, `SyncKeyError`, `TargetDataError` are exported from `acade2sql`.
43. **The pure reader never returns a partial table** (final review) — `count_rows` and
    `iter_rows` raise `SourceIntegrityError(Acade2SqlError)` naming the table, both row counts and
    the skipped pages when a page listed in the table's usage map is not a data page owned by the
    table (raised before any row is yielded), or when the live rows found differ from the row
    count stored in the TDEF (raised after the last row, inside the table's write transaction).
    The table becomes `error`; sync rolls back, so `--delete-missing` can never act on a partial
    read (reviewer repro: owner of the only data page set to 0x7777 → sync ends `error`, target
    still 45 rows). The TDEF count is trusted because no valid file was found where it differs: 0
    mismatches on the 285 sample tables, on every reviewer/scratch database, and on scratch files
    with 1/3 of the rows deleted without compaction, rows grown into overflow rows, MEMOs moved to
    LVAL pages, and reads while a writer connection was still open (.mdb and .accdb; both readers
    agreed at every step). Every other failure while locating rows (short usage-map record,
    overflow slot under 4 bytes, bad page) is an `Acade2SqlError` naming the table and page.
    **Live writers**: when `<name>.ldb`/`<name>.laccdb` exists next to the source, the pure reader
    logs one WARNING (someone has the database open, a write in progress can make a table
    inconsistent: close ACADE or use `--copy-to-temp`); with `copy_to_temp=True` the copy is
    taken first as always and only an INFO is logged.
44. **Dates before 1753 get a per-table warning** (final review) — both readers return the
    driver's calendar there (#39). While rows are written (`modes.load`, reader-neutral) any
    DATETIME value before 1753-01-01 adds the table warning "dates before 1753 are read using the
    ODBC driver's calendar and may differ from what Access displays"; the status is unchanged.
45. **The ODBC reader refuses Access Large Number (BIGINT) columns** (0.3.0 release fix) — the
    Access ODBC driver reports a Large Number column (TDEF type 19) as `CHAR` with
    `column_size` 4 (`data_type` -8) and returns each value as a garbage 4-character string; the
    garbage was converted and even passed `--checksum` (source-as-read == target). Now
    `PyodbcReader.get_table` and `iter_rows` cross-check every column the driver reports as
    CHAR(4) (cheap pre-filter: other tables are never cross-checked, and a table is checked once
    per reader) against the file's table definition, parsed read-only by the pure reader
    (`access_parser_reader.column_type_codes`: `_JetFile` + catalog + TDEF, no driver, no
    lock-file warning, no rejection of types the pure reader cannot read). It reads the file the
    driver opened, i.e. the temporary copy with `copy_to_temp`. A real BIGINT raises
    `UnsupportedAccessFeature` "column 'C' of table 'T' is an Access Large Number (BIGINT), which
    the Access ODBC driver misreads; use --reader pure", so convert marks the table `error`
    (exit 3) before its target table is created; the pure reader reads the values exactly. A
    genuine CHAR(4) text column passes. If the cross-check itself fails (a file the pure parser
    cannot handle) a WARNING is logged and the driver's metadata is used, so ODBC reads of such
    files keep working. `UnsupportedAccessFeature` moved to `acade2sql.errors` (re-exported from
    `acade2sql.readers.access_parser_reader`); it and `SourceIntegrityError` are exported from
    `acade2sql`.
