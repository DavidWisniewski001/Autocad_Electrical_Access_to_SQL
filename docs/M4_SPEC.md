# M4 spec: SQL Server + PostgreSQL targets, `append` / `sync` modes, `--dry-run`, `--schema`

Contract between `python-converter-dev` (owns `src/`) and `test-engineer` (owns `tests/`). Builds on
M3 (docs/M3_SPEC.md, DECISIONS #1–#24). Names and behaviour below are binding; private helpers are free.

**Environment fact:** no SQL Server or PostgreSQL server is reachable here. Everything must be fully
tested on SQLite; mssql/postgresql are tested by compiling DDL/DML with their dialects, plus *live*
tests that run only when `ACADE2SQL_TEST_PG_URL` / `ACADE2SQL_TEST_MSSQL_URL` are set (marker
`live_db`, skipped otherwise).

## 1. Packaging
`pyproject.toml` extras: `postgresql = ["psycopg[binary]>=3.1"]`, `mssql = []` (uses pyodbc + Microsoft
"ODBC Driver 17/18 for SQL Server"). A missing DBAPI module → `TargetError` whose message names the extra
(`pip install acade2sql[postgresql]`) or the ODBC driver to install.

## 2. Dialect hardening (writer + typemap)
* **mssql+pyodbc**: `create_engine(..., fast_executemany=True)`. The raw fast path stays SQLite-only.
* **mssql indexes**: an index containing an `NVARCHAR(max)`/`VARBINARY(max)` column, or whose key size
  exceeds 1700 bytes (NVARCHAR(n) = 2n bytes; non-clustered limit), is **skipped with a warning** in
  `TableResult.warnings` (status unaffected). A PK on such a column → table `error` with a clear message.
  The PK limit is 900 bytes (clustered).
* **PostgreSQL identifiers > 63 bytes** (UTF-8) for tables/columns → pre-flight `Acade2SqlError` (exit 1)
  naming them, before any target modification (PG would silently truncate and could collide). Index names
  are already ≤ 60.
* **PostgreSQL NUL**: text containing `\x00` cannot be stored in PG → that table `error` with message
  `"write failed: NUL character in text column <C> (not supported by PostgreSQL)"`.
* **Case-insensitive targets** (mssql default collation): no special handling; documented risk.
* **`--schema NAME`** (API `schema: str | None = None`): all tables/indexes are created in that schema
  (`MetaData(schema=...)`). SQLite: `ValueError`/exit 2 if given (not supported). The schema must already
  exist (no CREATE SCHEMA) — missing schema → table errors with the DB message.

## 3. Modes (`mode: Literal["full", "append", "sync"]`, CLI `--mode`)
Common: for `append`/`sync`, a missing target table is created (as in full). An existing target table is
**never dropped**. Its columns are reflected; if any source column name is missing in the target, or the
target has a NOT NULL column without default that the source lacks → table `error`
`"schema drift: ... (re-run with --mode full)"`. Extra nullable target columns are allowed (left NULL).

### append
Insert all source rows. Validation: `target_rows_after == target_rows_before + source_rows`; the result
gains `target_rows_before: int | None`. Duplicate-key violations → table `error` (transaction rolled back,
target unchanged). Checksum is not computed in append (`checksum_match=None`, warning if requested).

### sync (upsert by key)
* **Key per table**: `--key COL[,COL...]` (applies to every table that has all those columns) and
  `--key TABLE=COL[,COL...]` (repeatable; table match case-insensitive; overrides the global one).
  Default = `TableInfo.primary_key` (RECNUM for the catalogs). No key → behaviour from
  `--no-key {error,replace}` (default `error`: table `error` "no key for sync; use --key or --no-key replace";
  `replace`: delete all target rows and insert, in one transaction).
* Key columns must exist in source and target; source keys must be unique and non-NULL (checked while
  loading; violation → table `error`, rolled back).
* **Algorithm** (one transaction per table): stream source batches; per batch, fetch existing target
  keys for the batch's keys (bound `IN`/tuple-IN or a temp key table), `UPDATE` rows whose key exists and
  whose values differ (or all existing — implementer's choice, document it), `INSERT` the new ones.
  Native upsert (`INSERT … ON CONFLICT DO UPDATE`) may be used on sqlite/postgresql, but the **generic
  path must exist, be selectable on SQLite (`_force_generic_sync=True` writer flag) and be tested there**;
  mssql always uses the generic path.
* `--delete-missing` (API `delete_missing: bool = False`): afterwards delete target rows whose key was not
  in the source (via a temp table of source keys or batched NOT-IN; must work for 270k keys).
* Result counts: `TableResult` gains `inserted`, `updated`, `deleted`, `unchanged` (`int | None`).
* **Validation**: every source row must be present with identical values: with `--checksum` compare the
  source checksum to the checksum of target rows **restricted to the source keys**; always check
  `count(target rows with key in source keys) == source_rows`. With `--delete-missing` also
  `target_rows == source_rows`; without it, extra target rows are reported (`extra_rows`) but don't fail.
* Idempotency: a second identical sync → `inserted=0, updated=0, deleted=0`, all pass.

## 4. `--dry-run` (API `dry_run: bool = False`)
Reads the source schema (and row counts unless `--no-counts`), **never connects to the target**
(dialect from the URL via `sqlalchemy.create_mock_engine`, no DBAPI import needed), and prints:
the plan (mode, target masked, schema, per table: name, rows, key used for sync, actions: drop/create or
create-if-missing, warnings such as skipped mssql indexes) and the full DDL (`CREATE TABLE` + `CREATE
INDEX`, and `DROP TABLE` for full) for the target dialect. Exit 0. The report (if `--report`) has
`dry_run: true`, statuses `"planned"`, and `ok` true. Works for sqlite, mssql and postgresql URLs even if
psycopg/ODBC Driver 18 are not installed.

## 5. CLI additions
`--mode {full,append,sync}`, `--key SPEC` (repeatable), `--no-key {error,replace}`, `--delete-missing`,
`--schema NAME`, `--dry-run`, `--no-counts` (dry-run only). Invalid combos (`--delete-missing` without
sync, `--key` without sync, `--schema` with SQLite) → exit 2 with a message. `render_text` shows the new
counters for append/sync, e.g. `PASS COMP source=120 target=125 inserted=3 updated=2 deleted=0 extra=5`.

## 6. Acceptance (re-verified by the lead)
1. ruff clean; full pytest green (live tests skipped without env URLs).
2. Synthetic fixture: full → modify the *source copy* (insert, update incl. '' ↔ NULL and padding
   changes, delete) → sync → target equals the new source (checksum), counters exact; second sync is a
   no-op; generic and native paths give identical results.
3. Real data: `footprint_lookup.accdb` full to SQLite, then `--mode sync --checksum` → all pass,
   inserted=updated=deleted=0; `--mode append` on a fresh target equals full; append on a filled target
   → duplicate-key errors, target unchanged.
4. `--dry-run` against `mssql+pyodbc://u:p@h/db?driver=ODBC+Driver+18+for+SQL+Server` and
   `postgresql+psycopg://u:p@h/db` for default_cat prints NVARCHAR(max)/TEXT DDL, skipped-index warnings for
   mssql where applicable, no password, exit 0, and makes no network connection.
5. Sources unchanged; no lock file left.
