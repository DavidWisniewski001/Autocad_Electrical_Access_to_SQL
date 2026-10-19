# Changelog

All notable changes to acade2sql are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Milestone M4 is in progress. Nothing below is released yet; see [M4_SPEC.md](M4_SPEC.md).

### Planned
- SQL Server (`mssql+pyodbc`) and PostgreSQL (`psycopg`) targets, with `postgresql` / `mssql`
  install extras.
- `--mode append` and `--mode sync` (upsert by key; `--key`, `--no-key`, `--delete-missing`).
- `--dry-run`: print the plan and the DDL without connecting to the target.
- `--schema NAME` for server targets.

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
