# Changelog

All notable changes to acade2sql are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Milestone M5 is planned. Nothing below is released yet.

### Planned
- Pure-Python fallback reader (`access-parser`) for machines without the Microsoft Access ODBC
  driver.
- CLI polish.
- Verification of the SQL Server and PostgreSQL targets against live servers.

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
