# Changelog

All notable changes to acade2sql are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Desktop window** (milestone M7): `acade2sql-gui` (gui-script, no console), `acade2sql gui` and
  `python -m acade2sql.gui`. tkinter/ttk only, no new dependency. Source and target pickers (SQLite
  file, SQL Server and PostgreSQL connection fields, or a custom URL), every convert option, Dry
  run (plan and DDL), Convert, Check setup (`doctor`), List tables, per-table progress, results
  table, log pane, Cancel and Save report. Work runs in a background thread; passwords are masked
  wherever a URL or error is shown. See [USAGE.md](USAGE.md#use-the-desktop-window).
- `convert()` hooks, backwards compatible (the CLI does not use them):
  `table_started(name, index, total)` before each table, and `cancel()` polled before each table and
  after each source batch; cancelled tables get `status="error"`, `error="cancelled by user"`.
- `acade2sql.converter.sqlite_url(path)`: public name of the SQLite URL helper.

Still open from the plan: verification of the SQL Server and PostgreSQL targets against live
servers, and a test with a real ACADE project database.

## [0.3.0] - 2026-10-19

Milestone M5: a reader that needs no Access driver, reader selection, `doctor`, CLI polish and the
0.2.0 backlog. See [M5_SPEC.md](M5_SPEC.md) and [DECISIONS.md](DECISIONS.md) #37 to #44.

### Added
- **Pure-Python Access reader** (`--reader pure`). It reads `.mdb` and `.accdb` files without the
  Microsoft Access ODBC driver and without pyodbc. It returns the same tables, column types,
  indexes, primary keys and values as the ODBC reader.
  - It decodes Jet 4 compressed Unicode text itself. access-parser's own text decoder corrupts it
    (for example U+2013 `–` read as `'\x13'`) and silently drops undecodable bytes. Text that cannot
    be decoded makes the table `error`; it is never replaced or dropped.
  - It opens the file read-only (memory-mapped) and never creates a `.ldb` / `.laccdb` lock file.
  - It never returns a partial table. If the pages it finds for a table do not add up to the row
    count stored in the table definition, or a page listed for the table belongs to another one, it
    raises `SourceIntegrityError` and the table becomes `error` (a sync of that table is rolled back).
  - It refuses, with a clear error and never wrong data: password-protected databases, encrypted
    (RC4-encoded or ACCDB-encrypted) databases, and per table, attachment / multi-value fields,
    calculated fields and Date/Time Extended fields.
  - If a lock file (`.ldb` / `.laccdb`) next to the source shows that the database is open in
    another program, it logs a warning recommending to close AutoCAD Electrical / Access or to use
    `--copy-to-temp`.
- `--reader {auto,odbc,pure}` on `inspect` and `convert` (default `auto`), and `reader=` in the
  Python API (`convert()`, `open_reader()`). `auto` uses the ODBC reader when the driver is usable,
  otherwise the pure reader with one warning.
- `"reader": "odbc" | "pure"` in the JSON report and in `inspect --json`.
- `acade2sql doctor`: Python version and bitness, the Access ODBC drivers (also a driver of the
  other bitness), which readers are usable, psycopg and pyodbc versions, SQL Server ODBC drivers,
  and the acade2sql version. Exit 0 when at least one Access reader is usable, else 1.
- `python -m acade2sql` runs the CLI.
- `--progress` / `--no-progress` for `convert`: one line per finished table on stderr
  (`[5/19] BIG 12345 rows 0.2s`). On by default when stderr is a terminal.
- `--tables-file PATH` / `--exclude-file PATH`: table names from a UTF-8 file, one exact name per
  line (commas are part of the name), blank lines and `#` comments ignored. They add to `--tables`
  / `--exclude`. This is the way to select a table whose name contains a comma.
- `scripts/compare_readers.py`: compares the pure and ODBC readers table by table (schema and
  checksum of all rows). Exit 0 identical, 1 differences, 2 usage error.
- A per-table warning for dates before 1753-01-01: `dates before 1753 are read using the ODBC
  driver's calendar and may differ from what Access displays`. The status is not affected.
- `UsageError`, `SchemaError`, `SyncKeyError` and `TargetDataError` can be imported from
  `acade2sql`.

### Changed
- `access-parser>=0.0.6,<0.1` (Apache-2.0) and `construct` are now core dependencies. The
  `fallback` extra still exists but installs nothing.
- When the Access driver (or pyodbc) is missing, `inspect` and `convert` no longer stop with exit 1:
  the default `--reader auto` falls back to the pure reader with a warning. `--reader odbc` keeps
  the 0.2.0 behaviour (exit 1 with the installer to use).
- `--help` lists `doctor`.

### Fixed
- Short passwords are masked in all structured positions (URL user info, `PWD=`, `password=` and
  other secret query values) no matter how short. Free-text occurrences are masked when the secret
  has 4 or more characters, so a 2-character password no longer masks unrelated text.
- A `PWD=` / `password=` query value right after `?` or `&` is masked only up to the next `&`
  (`?password=***&sslmode=require`), so following parameters stay readable.
- `--dry-run --mode sync` of a table without a key (`--no-key error`) no longer lists the
  `replace-rows` action or prints DDL for that table. If no statement remains, the DDL section
  reads `-- DDL: none (no table would be created)`.
- `--checksum` together with `--dry-run` now prints `warning: --checksum is ignored with --dry-run`
  (the exit code is unchanged).

### Verified
- `scripts/compare_readers.py` on both sample databases: `footprint_lookup.accdb` 117/117 tables
  identical (ODBC 3.4 s, pure 3.7 s), `default_cat.mdb` 168/168 identical (ODBC 128.7 s, pure
  81.6 s, both including checksums). This includes the 260 rows of table `MS` that contain U+2013
  `–`.
- `acade2sql convert default_cat.mdb --reader pure --checksum`: 168/168 tables pass.
- Synthetic `.mdb` and `.accdb` fixtures (19 tables, all Access types): pure and ODBC readers
  identical; full conversion with `--reader pure --checksum` passes every table.

### Known limitations
- **The ODBC driver misreads Access "Large Number" (`BIGINT`) columns** (it reports them as
  `CHAR(4)`). The ODBC reader detects this from the file's table definition and refuses the table,
  with status `error` and a "use --reader pure" message, instead of writing garbage. Use
  `--reader pure` for files with Large Number columns. The ACADE sample databases have none.
- Dates before 1753: both readers use the ODBC driver's calendar, which can differ from what Access
  displays (for example `#1700-06-15#` reads as 1700-06-02). The table gets a warning.
- Jet 3 (Access 97) files: supported by the pure reader, but not tested (no sample file).
- The pure reader memory-maps the source file and streams rows page by page. It does not read one
  table into memory as a whole, but it has only been measured on the two sample databases (up to
  about 645 MB).
- SQL Server and PostgreSQL still have not been tested against a live server.
- The writer does not support the legacy `SQL Server` ODBC driver or SQL Server Native Client;
  `doctor` marks them "not supported by the writer". Use ODBC Driver 17 or 18 for SQL Server.
- No real ACADE project database has been tested yet.

## [0.2.0] - 2026-10-19

Milestone M4: server targets, incremental modes and dry runs. See [M4_SPEC.md](M4_SPEC.md) and
[DECISIONS.md](DECISIONS.md) #25 to #36.

### Added
- **SQL Server target** (`mssql+pyodbc://...`). Needs pyodbc (already a dependency) and the Microsoft
  "ODBC Driver 18 for SQL Server" (or 17). Bulk inserts use `fast_executemany`. The `mssql` extra
  exists but installs nothing extra.
- **PostgreSQL target** (`postgresql+psycopg://...`), with the `postgresql` install extra
  (`pip install -e ".[postgresql]"`, installs `psycopg[binary]>=3.1`).
- A missing database driver gives exit 1 with what to install (`pip install acade2sql[postgresql]`,
  or pyodbc plus "ODBC Driver 18 for SQL Server").
- A 15-second connect timeout for SQL Server and PostgreSQL targets, so an unreachable server fails
  quickly. Any `...timeout...` key in the URL replaces it.
- SQL Server key-size rules: a secondary index with an `NVARCHAR(max)` / `VARBINARY(max)` column or
  a key over 1,700 bytes is skipped with a warning; a primary key that is unindexable or over 900
  bytes makes that table `error`.
- PostgreSQL checks: table, column or schema names longer than 63 bytes stop the run before anything
  is written (exit 1); text containing a NUL character makes that table `error`.
- `--schema NAME`: create and load the tables in an existing schema (SQL Server and PostgreSQL only).
- `--mode append`: insert all source rows into existing tables (missing tables are created). Never
  drops a table. Validation: `target rows after = target rows before + source rows`.
- `--mode sync`: update changed rows and insert new ones, matched by a key, in one transaction per
  table. Only rows whose values differ are updated, so a repeated sync writes nothing.
  - `--key COL[,COL...]` (every table that has those columns) and `--key TABLE=COL[,COL...]`,
    repeatable; the default key is the table's primary key.
  - `--no-key {error,replace}`: what to do with a table that has no key (default `error`).
  - `--delete-missing`: also delete target rows whose key is no longer in the source.
- Sync safeguards: source keys must be unique and non-NULL; the counters are re-checked on the same
  connection before commit; schema drift (a source column missing in the target, or a target
  `NOT NULL` column the source does not have) makes the table `error` with
  `(re-run with --mode full)`.
- `--dry-run`: print the plan per table (actions, sync key, warnings) and the full DDL for the
  target dialect without connecting to the target. Works for SQLite, SQL Server and PostgreSQL URLs
  even when psycopg or the SQL Server ODBC driver is not installed.
- `--no-counts` (with `--dry-run`): skip the source row counts.
- New report fields: `dry_run`, `schema`, `ddl`, `summary.planned`, and per table
  `target_rows_before`, `inserted`, `updated`, `deleted`, `unchanged`, `extra_rows`, `key`,
  `actions`. New table status `planned`.
- Console lines show the counters, for example
  `PASS COMP source=5 target=5 inserted=0 updated=2 deleted=0 extra=0`.

### Changed
- `--tables` and `--exclude` accept space-separated names, repeated options and comma-separated
  lists: `--tables A B`, `--tables A --tables B` and `--tables A,B` are equivalent. An empty name
  (`A,,B`) is a usage error (exit 2).
- The `--checksum` comparison is column-aware: GUID case and braces are ignored only in GUID
  (Replication ID) columns, and Single (`REAL`) values are compared at single precision.
- `--dry-run` with `--mode full` prints `DROP TABLE IF EXISTS` for each table on every dialect.
- `PASS` lines can carry `warnings=N` (for example a skipped SQL Server index, or `--checksum` in
  append mode). The status is not affected.
- `--help` now shows the `--batch-size` default.
- Internal: the conversion module is now `acade2sql/converter.py`. `from acade2sql import convert`
  is unchanged.

### Fixed
- A sync no longer merges rows when the target's collation treats keys as equal that the source
  distinguishes (case-insensitive SQL Server or SQLite `NOCASE` columns, trailing spaces). The table
  is reported as `error` (`... keys that differ only by case/trailing spaces can't be synced; re-run
  with --mode full`) and the target table is left unchanged.
- In 0.1.0 the checksum ignored the case and braces of GUID-shaped text in any column, so such a
  change in an ordinary text column went undetected. It is now a checksum mismatch (and an update in
  sync).
- Single (`REAL`) values read back from PostgreSQL no longer cause false checksum mismatches.
- `--schema` values that are empty or contain `.`, `[`, `]`, `"`, a backtick or a control character
  are rejected with exit 2 instead of producing an unexpected name.
- `--dry-run` reports planning problems the real run would hit: an unmapped Access type makes the
  table `fail` and the dry run exits 3, and a missing sync key is an `error`.
- An unreachable SQL Server or PostgreSQL host fails after about 15 seconds instead of hanging for
  more than a minute.

### Verified
- `default_cat.mdb` to SQLite, `--mode full`: 168/168 tables pass in about 2 minutes.
- `default_cat.mdb` sync after altering the SQLite target (3 values changed, 2 rows deleted, 1 extra
  row added): `--mode sync --checksum` reported exactly `updated=3`, `inserted=2`, `extra=1`, with
  all 168 tables passing their checksums (about 5 minutes). `--mode sync --delete-missing` then
  reported `deleted=1`. A repeated sync changed nothing (all counters 0) in 2 min 17 s.
- `footprint_lookup.accdb`: full, then `--mode sync --checksum`: 117 tables pass, no changes.
- `--dry-run` of `default_cat.mdb` for SQL Server and PostgreSQL URLs: about 0.7 s, 168
  `CREATE TABLE` and 165 `CREATE INDEX` statements, no SQL Server index skipped, no password shown.

### Known limitations
- **SQL Server and PostgreSQL have not been tested against a live server.** They are verified by
  compiling the SQL for those dialects and by dry runs. Live tests exist (pytest marker `live_db`,
  environment variables `ACADE2SQL_TEST_PG_URL` / `ACADE2SQL_TEST_MSSQL_URL`) but have not been run.
- Sync cannot handle keys that differ only by case or trailing spaces on a target whose collation
  ignores those differences (the SQL Server default). Use `--mode full` for such tables.
- Append assumes it is the only writer to the table during the run; a concurrent writer can make the
  row-count check fail (it cannot corrupt data).
- Append does not compute checksums (`--checksum` gives a warning).
- `--schema` needs an existing schema; acade2sql does not create it.
- The 0.1.0 limitations still apply (lock file while reading, `NOT NULL` mostly not reported, NULL
  Yes/No reads as `False`, no real project database tested).

## [0.1.0] - 2026-10-19

First version. Covers milestones M1 (schema inventory), M2 (reader and `inspect`) and M3 (SQLite
writer and `convert`).

### Added
- `acade2sql inspect <path>` lists tables, columns, primary keys and row counts. Options: `--table`
  (detail, repeatable), `--json`, `--counts` / `--no-counts`.
- `acade2sql convert <source>` converts to SQLite in `full` mode. Options: `--target`, `--mode full`,
  `--tables`, `--exclude`, `--batch-size` (default 5000), `--checksum`, `--copy-to-temp`, `--report`.
- Global options `-v` / `--verbose` (structured `key=value` logs on stderr, tracebacks) and
  `--version`.
- Target resolution: `--target`, then `ACADE2SQL_TARGET`, then `./<source stem>.sqlite`. Plain
  `.sqlite` / `.sqlite3` / `.db` paths become absolute SQLite URLs, and missing folders are created.
- Read-only pyodbc reader for `.mdb` and `.accdb` (`ReadOnly=1`, read-only connection, ODBC pooling
  off).
- Driver diagnostics: an actionable message when the Microsoft Access ODBC driver is missing or is
  installed only for the other bitness. The message names the Access Database Engine 2016
  Redistributable installer to use.
- `--copy-to-temp` / `copy_to_temp=True`: read from a temporary copy, so no lock file appears next
  to the source.
- Table and column names are preserved verbatim and quoted by the SQL dialect (for example
  `_PINLIST_BRUNDY/HUBBEL`, `ALLEN BRADLEY`, `KRAUS & NAIMER`).
- Table-driven type map (`typemap.py`) for SQLite, with SQL Server and PostgreSQL entries defined
  and compile-tested. Exact decimals are stored on SQLite as text (`DECIMAL_TEXT`).
- Primary keys: a `PrimaryKey` index is used (`discovered`); otherwise a single AutoNumber column
  such as `RECNUM` (`inferred`). Source AutoNumber values are kept. Other indexes are recreated with
  unique names (`ix_<table>_<index>`).
- Validation for every table: row counts, an optional order-independent value checksum, and
  `pass` / `fail` / `error` status. A failing table does not stop the run.
- Console summary, JSON report (`--report`) and exit codes 0 (pass), 1 (error before writing),
  2 (usage), 3 (validation failure).
- Passwords and other secrets in target URLs are masked in reports, logs and error messages.
- Python API: `from acade2sql import convert, resolve_target, open_reader` and the exception classes
  (`Acade2SqlError`, `SourceNotFoundError`, `DriverNotFoundError`, `BitnessMismatchError`,
  `TableNotFoundError`, `TargetError`, `WriteError`).
- pytest suite with synthetic `.mdb` / `.accdb` fixtures and the `requires_driver`, `real_data` and
  `slow` markers; ruff configuration.
- Design documents: schema inventory and type mapping of the sample ACADE catalogs
  (`docs/schema_inventory.md`, `docs/type_mapping.md`) and `docs/DECISIONS.md`.

### Verified
- `default_cat.mdb`: 168 tables / 1,594,493 rows converted, all passing, in about 2 minutes (about
  4 min 43 s with `--checksum`).
- `footprint_lookup.accdb`: 117 tables / 261,858 rows converted, all passing, in about 10 seconds.

### Known limitations
- Only the SQLite target and `full` mode are supported.
- The Access driver creates a `.ldb` / `.laccdb` file next to the source while reading; use
  `--copy-to-temp` to avoid it.
- The driver reports `NOT NULL` text and number columns as nullable, so only primary-key,
  AutoNumber and Yes/No columns are created `NOT NULL`.
- A NULL Yes/No value reads as `False`.
- No real ACADE project database has been tested; project tables are covered by synthetic fixtures
  only.
