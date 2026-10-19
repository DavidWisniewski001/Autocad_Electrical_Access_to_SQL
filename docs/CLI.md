# acade2sql command-line reference

Reference for acade2sql **0.2.0**. Everything here comes from `src/acade2sql/cli.py` and the modules
it calls, and was checked against `--help` and real runs on the synthetic test database. For worked
examples, see the [usage guide](USAGE.md).

- [acade2sql](#acade2sql)
- [acade2sql inspect](#acade2sql-inspect)
- [acade2sql convert](#acade2sql-convert)
  - [Modes](#modes)
  - [Sync options](#sync-options)
  - [Dry run](#dry-run)
  - [Option combinations](#option-combinations)
  - [convert console output](#convert-console-output)
- [Target resolution](#target-resolution)
- [Exit codes](#exit-codes)
- [Output streams](#output-streams)
- [JSON report](#json-report)
- [Not yet supported](#not-yet-supported)

## acade2sql

```text
usage: acade2sql [-h] [-v] [--version] COMMAND ...

Convert AutoCAD Electrical Access databases (.mdb/.accdb) to SQL.

positional arguments:
  COMMAND
    inspect      list tables, columns, keys, counts
    convert      convert to a SQL database

options:
  -h, --help     show this help message and exit
  -v, --verbose  debug logging (structured, stderr) and tracebacks on error
  --version      show program's version number and exit
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--help` | | Show help and exit (code 0). |
| `-v`, `--verbose` | off | Debug logging to stderr and a traceback for `error:` failures. Can be given before or after the command (`acade2sql -v convert ...` or `acade2sql convert ... -v`). |
| `--version` | | Print `acade2sql 0.2.0` and exit (code 0). |

A command (`inspect` or `convert`) is required.

**Log format** (stderr): `<local time> <LEVEL> <logger> <message> key=value ...`. For example:

```text
2026-10-19T07:20:07.024 ERROR acade2sql.converter table failed table=NETS error="write failed: schema drift: target table NETS lacks source column(s) SHEET (re-run with --mode full)"
```

Without `--verbose`, only WARNING and ERROR lines are logged.

## acade2sql inspect

List the user tables of an Access database with column counts, row counts and primary keys.
`MSys*` system tables are not listed. `inspect` is unchanged in 0.2.0.

```text
usage: acade2sql inspect [-h] [-v] [--table NAME] [--json]
                         [--counts | --no-counts]
                         path
```

| Argument / option | Default | Description |
|---|---|---|
| `path` | required | Access database, `.mdb` or `.accdb`. |
| `--table NAME` | all tables | Show only this table, plus its full column and index detail. Repeatable. Case-insensitive. An unknown name gives exit 1 (`error: Table 'nope' not found in ...`). |
| `--json` | off | Write a JSON document to stdout instead of text. |
| `--counts` / `--no-counts` | `--counts` | Count rows per table (`SELECT COUNT(*)`). `--no-counts` is faster on large catalogs and shows `-` (text) or `null` (JSON). |
| `-v`, `--verbose` | off | As above. |

### inspect text output

One summary line per table: name, column count, row count, and primary key with its source. Then a
total line (`N tables, M rows`, or `N tables` when counts are off).

```text
_PINLIST_XX       6 cols         40 rows  pk=RECNUM (inferred)
ALLEN BRADLEY     5 cols          2 rows  pk=RECNUM (inferred)
2 tables, 42 rows

Table _PINLIST_XX  rows=40  pk=RECNUM (inferred)
    #  column                   type             size  nullable
    1  RECNUM                   COUNTER            10  no  autoincrement
    2  CATALOG                  VARCHAR            60  yes
    ...
    6  NOTES                    LONGCHAR   1073741823  yes
  index IDX_CAT (CATALOG, ASSEMBLYCODE) non-unique

Table ALLEN BRADLEY  rows=2  pk=RECNUM (inferred)
    ...
  indexes: none reported
```

The detail blocks (`Table ...`) appear only with `--table`.

Primary key sources (`pk=... (<source>)`):

| Source | Meaning |
|---|---|
| `discovered` | Access reports a unique index named `PrimaryKey`. Its columns are the key. |
| `inferred` | There is no `PrimaryKey` index, and the table has exactly one AutoNumber (`COUNTER`) column, which becomes the key. ACADE catalogs: `RECNUM`. |
| `none` (`pk=- (none)`) | No key. The target table is created without a primary key, and `--mode sync` needs `--key` or `--no-key replace` for it. |

The `nullable` column shows what the driver reports. The driver reports most `NOT NULL` columns as
nullable; see the [README](../README.md#limitations-and-known-behaviour).

### inspect --json output

```text
acade2sql inspect fixture.accdb --json --no-counts --table NETS
```

```json
{
  "source": "fixture.accdb",
  "tables": [
    {
      "name": "NETS",
      "columns": [
        {
          "name": "RECNUM",
          "ordinal": 1,
          "access_type": "COUNTER",
          "data_type": 4,
          "size": 10,
          "decimal_digits": 0,
          "nullable": false,
          "autoincrement": true
        },
        {
          "name": "NETNAME",
          "ordinal": 2,
          "access_type": "VARCHAR",
          "data_type": -9,
          "size": 30,
          "decimal_digits": null,
          "nullable": true,
          "autoincrement": false
        }
      ],
      "primary_key": ["RECNUM"],
      "primary_key_source": "inferred",
      "indexes": [],
      "row_count": null
    }
  ]
}
```

(Trimmed: `NETS` has a third column. The real output puts one array item per line.)

| Field | Meaning |
|---|---|
| `source` | The path as you typed it. |
| `columns[].access_type` | Upper-case ODBC type name (`VARCHAR`, `LONGCHAR`, `COUNTER`, `INTEGER`, `DATETIME`, `BIT`, ...). |
| `columns[].data_type` | ODBC SQL type code. |
| `columns[].size` | Declared size (characters for text; `1073741823` for MEMO). |
| `indexes[]` | `{"name", "columns", "unique"}` for each index the driver reports. |
| `row_count` | Integer, or `null` with `--no-counts`. |

## acade2sql convert

Convert an Access database to a SQLite, SQL Server or PostgreSQL database.

```text
usage: acade2sql convert [-h] [-v] [--target URL|PATH]
                         [--mode {full,append,sync}] [--tables T [T ...]]
                         [--exclude T [T ...]] [--batch-size N] [--checksum]
                         [--copy-to-temp] [--report PATH.json] [--key SPEC]
                         [--no-key {error,replace}] [--delete-missing]
                         [--schema NAME] [--dry-run] [--no-counts]
                         source
```

| Argument / option | Default | Description |
|---|---|---|
| `source` | required | Access database, `.mdb` or `.accdb`. Opened read-only. Write it **before** `--tables` / `--exclude`. |
| `--target URL\|PATH` | `$ACADE2SQL_TARGET`, else `./<source stem>.sqlite` | Where to write. See [Target resolution](#target-resolution). |
| `--mode {full,append,sync}` | `full` | How each selected table is written. See [Modes](#modes). |
| `--tables T [T ...]` | all user tables | Convert only these tables. Names can be separated by spaces (`--tables A B`), commas (`--tables A,B`) or given in repeated options (`--tables A --tables B`). Exact names, case-insensitive. An unknown name gives exit 1 before anything is written. An empty name (`A,,B`) gives exit 2. |
| `--exclude T [T ...]` | none | Skip these tables. Same forms as `--tables`. Unknown names are ignored. Wins over `--tables`. |
| `--batch-size N` | `5000` | Rows fetched and written per batch. Must be an integer of 1 or more; otherwise exit 2. Each table is still written in one transaction. |
| `--checksum` | off | Also compare an order-independent checksum of all values in each table. Every table is read a second time. In `append` mode no checksum is computed and the table gets the warning `checksum not computed in append mode`. |
| `--copy-to-temp` | off | Copy the source to a temporary folder (`acade2sql_*` in `%TEMP%`) and read the copy. No `.ldb`/`.laccdb` lock file appears next to the source. The copy is deleted at the end. |
| `--report PATH.json` | none | Write the [JSON report](#json-report) here. Parent folders are created. Written whenever conversion or planning starts (exit codes 0 and 3); not written on exit 1 or 2. |
| `--key SPEC` | the table's primary key | Sync key. `COL[,COL...]` applies to every table that has all those columns; `TABLE=COL[,COL...]` applies to one table. Repeatable. Requires `--mode sync`. See [Sync options](#sync-options). |
| `--no-key {error,replace}` | `error` | Sync of a table that has no key: `error` makes the table `error`; `replace` deletes all its target rows and inserts the source rows, in one transaction. Requires `--mode sync`. |
| `--delete-missing` | off | Sync: after updating, delete target rows whose key is not in the source. Requires `--mode sync`. |
| `--schema NAME` | none (the connection's default schema) | Create and write the tables in this existing schema. SQL Server and PostgreSQL only. One plain name, without quotes or dots. |
| `--dry-run` | off | Print the plan and the DDL; never connect to the target. See [Dry run](#dry-run). |
| `--no-counts` | counts on | With `--dry-run`: do not count the source rows (faster on large files). |
| `-v`, `--verbose` | off | Debug logging and tracebacks. |

### Modes

| Mode | Existing target table | Missing target table | Rows | Table status `pass` when |
|---|---|---|---|---|
| `full` | dropped and recreated with its indexes | created | all source rows inserted | source rows = target rows, and checksums match (if `--checksum`) |
| `append` | kept; never dropped | created | all source rows inserted | target rows after = target rows before + source rows |
| `sync` | kept; never dropped | created | changed rows updated, new rows inserted (by key); with `--delete-missing` rows with keys not in the source deleted | target rows whose key is in the source = source rows; with `--delete-missing` also target rows = source rows; checksum of those rows matches (if `--checksum`) |

- Every table is one transaction. A failing table is rolled back (status `error`) and the other
  tables are still processed.
- In `append` and `sync`, the existing target table must match the source: every source column must
  exist in it (exact name), and it must not have a `NOT NULL` column without default that the source
  lacks. Otherwise the table is `error` with
  `write failed: schema drift: ... (re-run with --mode full)`. Extra nullable target columns are
  allowed; they stay `NULL` on inserted rows and untouched on updated rows.
- `append` on a table that already holds the same keys fails with a duplicate-key error
  (`write failed: IntegrityError: UNIQUE constraint failed: ...` on SQLite) and is rolled back. A
  table without a key accepts the rows again (duplicates).
- A table that `append` or `sync` creates stays in the target (empty) if its load is then rolled
  back.

### Sync options

**Which key is used** for a table, first match wins:

1. `--key TABLE=COL[,COL...]` for that table (table name case-insensitive). If a column is missing
   from the table, the table is `error`: `key column(s) NOPE not found in source table PNLCOMP`.
2. `--key COL[,COL...]` (global), if the table has all those columns (case-insensitive). Tables
   without them fall through to the next rule.
3. The table's primary key (see `inspect`: `discovered` or `inferred`; `RECNUM` in the catalogs).
4. None: `--no-key error` (default) makes the table `error`
   (`no key for sync; use --key or --no-key replace`); `--no-key replace` reloads the table.

A later `--key` for the same table (or a later global `--key`) replaces the earlier one. A `--key
TABLE=...` naming a table that is not converted logs a warning and is ignored.

**Key rules:** source keys must be unique and non-NULL, or the table is `error` and rolled back
(`duplicate source key ('H0001',) for sync key (HDL)`, `NULL value in sync key ...`). Keys must also
be unique in the target.

**Collation guard:** the target looks keys up with its own collation. If it returns a row whose key
differs from the source key only by case or trailing spaces (SQL Server default collation, SQLite
`COLLATE NOCASE` or `RTRIM` columns), the table is `error` and unchanged:

```text
ERROR COMP source=5 target=- error=write failed: target key 'h0001' matches source key 'H0001' under the target's collation; keys that differ only by case/trailing spaces can't be synced; re-run with --mode full
```

**Counters** (in the console line and the report):

| Counter | Meaning |
|---|---|
| `inserted` | source rows whose key was not in the target |
| `updated` | rows whose key was in the target and whose values differed |
| `unchanged` | rows whose key was in the target with identical values (report only) |
| `deleted` | rows removed by `--delete-missing`, or all previous rows with `--no-key replace` |
| `extra` / `extra_rows` | target rows whose key is not in the source, after the run (kept unless `--delete-missing`) |

Values are compared like the checksum: `''` differs from NULL, spaces and case matter, GUID columns
ignore case and braces, Single (`REAL`) columns are compared at single precision. A repeated sync
therefore reports `inserted=0 updated=0 deleted=0`. Before commit, acade2sql re-counts the target
rows on the same connection; if they disagree with the counters, the table is
`write failed: sync verification failed: ...` and rolled back.

### Dry run

`--dry-run` reads the source schema (and row counts, unless `--no-counts`), builds the tables for
the target's dialect and prints the plan and the DDL. It **never connects to the target** and does
not need psycopg or the SQL Server ODBC driver. No SQLite file is created.

```text
> acade2sql convert fixture.accdb --target "mssql+pyodbc://acade:<password>@sqlhost/acade?driver=ODBC+Driver+18+for+SQL+Server" --dry-run --tables _PINLIST_XX COMP
PLANNED _PINLIST_XX source=40 target=- actions=drop,create,insert
PLANNED COMP source=5 target=- actions=drop,create,insert
2 tables: 0 passed, 0 failed, 0 errors, 2 planned
dry run: mode=full target=mssql+pyodbc://acade:***@sqlhost/acade?driver=ODBC+Driver+18+for+SQL+Server schema=- (nothing was written)
-- DDL
DROP TABLE IF EXISTS [_PINLIST_XX];
CREATE TABLE [_PINLIST_XX] (
	[RECNUM] INTEGER NOT NULL,
	[CATALOG] NVARCHAR(60) NULL,
	...
	[NOTES] NVARCHAR(max) NULL,
	PRIMARY KEY ([RECNUM])
);
CREATE INDEX [ix__PINLIST_XX_IDX_CAT] ON [_PINLIST_XX] ([CATALOG], [ASSEMBLYCODE]);
DROP TABLE IF EXISTS [COMP];
CREATE TABLE [COMP] (
	...
);
```

stderr: `dry run: target=mssql+pyodbc://acade:***@sqlhost/acade?driver=... elapsed=0.3s`.

`actions` per table:

| Mode | Actions |
|---|---|
| `full` | `drop,create,insert` |
| `append` | `create-if-missing,append` |
| `sync` with a key | `create-if-missing,upsert` (plus `delete-missing` with `--delete-missing`) |
| `sync` without a key | `create-if-missing,replace-rows` |

- In `full` mode the DDL starts each table with `DROP TABLE IF EXISTS`. In `append` and `sync` the
  DDL shows `CREATE TABLE` for every table, because the dry run cannot see which tables already
  exist; the real run creates only the missing ones.
- In sync mode each line shows `key=COL[,COL]`, or `key=-` when the table has no key.
- Warnings (for example `index X skipped on SQL Server: ...`) are listed as
  `warning: <table>: <text>` before `-- DDL`.
- Status `PLANNED` counts as success (exit 0). Problems the real run would also hit are reported:
  a missing sync key, an unknown `--key` column or a SQL Server primary key that is too large is
  `ERROR`; an unmapped Access type is `FAIL`. The dry run then exits 3.
- PostgreSQL names over 63 bytes and an unsupported dialect stop the dry run with exit 1, as in a
  real run.

### Option combinations

These combinations are rejected before the source or target is opened. The tool prints
`error: <message>` and exits 2.

| Combination | Message |
|---|---|
| `--key` without `--mode sync` | `--key requires --mode sync` |
| `--no-key` without `--mode sync` | `--no-key requires --mode sync` |
| `--delete-missing` without `--mode sync` | `--delete-missing requires --mode sync` |
| `--no-counts` without `--dry-run` | `--no-counts is only valid with --dry-run` |
| `--schema` with a SQLite target | `--schema is not supported for SQLite targets` |
| `--schema` empty or blank | `--schema must not be empty` |
| `--schema` containing `.`, `[`, `]`, `"`, a backtick or a control character | `invalid --schema 'a.b': contains '.'; give one plain schema name (it is quoted by acade2sql)` |
| `--key` with an empty column or table name (`--key =HDL`, `--key A,,B`) | `invalid --key ...` |
| `--mode` other than `full`, `append`, `sync`; `--batch-size` below 1; empty name in `--tables`/`--exclude` | argparse usage message |

Valid combinations: `--checksum` works with every mode (no effect in `append`, which warns);
`--dry-run` works with every mode, `--schema`, `--key`, `--no-key`, `--delete-missing` and
`--no-counts`; `--schema` works with every mode on SQL Server and PostgreSQL.

### convert console output

stdout: one line per table, in source table order, then a summary.

```text
PASS NETS source=2 target=2 checksum=match
PASS COMP source=5 target=5 inserted=0 updated=2 deleted=0 extra=0 checksum=match
PASS NETS source=2 target=2 target_before=0 inserted=2 warnings=1
ERROR PNLCOMP source=- target=- error=write failed: no key for sync; use --key or --no-key replace
16 tables: 15 passed, 0 failed, 1 errors
```

Line format: `<PASS|FAIL|ERROR|PLANNED> <table> source=<n|-> target=<n|->`, followed by these parts
when they apply:

| Part | When |
|---|---|
| ` target_before=<n>` | `append`: target rows before the insert |
| ` inserted=<n> updated=<n> deleted=<n> extra=<n>` | `sync` (`append` shows `inserted` only) |
| ` key=<cols or ->` | `sync` with `--dry-run` |
| ` actions=<list>` | `--dry-run` |
| ` checksum=match\|MISMATCH` | `--checksum` (not in `append`) |
| ` warnings=<n>` | the table has warnings (listed in the report and the log) |
| ` error=<message>` | status `ERROR` |

The summary line adds `, N planned` in a dry run.

| Status | Meaning |
|---|---|
| `PASS` | The mode's checks passed (see [Modes](#modes)). A `PASS` line can carry `warnings=N`, for example a skipped SQL Server index or `checksum not computed in append mode`. |
| `FAIL` | The table was written, but a check did not match (counts, checksum), or a column has an Access type that is not in the type map (stored as text, warning `unmapped type <T> in column <C> stored as text`). |
| `ERROR` | Reading, creating or writing this table raised an error, or (sync) the key was unusable. The message starts with `read failed:` (Access side) or `write failed:` (target side), except in a dry run. The table's transaction was rolled back, and the other tables were still processed. |
| `PLANNED` | `--dry-run` only: the table was planned; nothing was written. |

stderr: one statistics line per run:

```text
target=sqlite:///C:\...\out\proj.sqlite rows=12457 elapsed=0.5s rows_per_s=24492
```

`rows` is the sum of the target row counts. With `--dry-run` the line is
`dry run: target=<masked URL> elapsed=<s>s`.

## Target resolution

| Order | Source | Example |
|---|---|---|
| 1 | `--target` | `--target C:\out\cat.sqlite` or `--target sqlite:///C:/out/cat.sqlite` |
| 2 | Environment variable `ACADE2SQL_TARGET` (blank values are ignored) | `$env:ACADE2SQL_TARGET = "C:\out\cat.sqlite"` |
| 3 | Default | `./<source stem>.sqlite` in the **current directory** |

- A plain path (no `://`) ending in `.sqlite`, `.sqlite3` or `.db` (any case) becomes a `sqlite:///`
  URL with the absolute path. Characters such as `%` are taken literally.
- Anything else is used as a SQLAlchemy URL. A path with another extension (for example `out.sql`)
  is therefore rejected: `error: Invalid target URL ...` (exit 1).
- Supported dialects: `sqlite`, `mssql` (driver `pyodbc`) and `postgresql` (driver `psycopg`).
  Others stop with `error: Unsupported target dialect 'oracle' (...); supported: sqlite, mssql,
  postgresql` (exit 1).
- Missing folders for a SQLite target are created.

| Target | URL |
|---|---|
| SQLite | `sqlite:///C:/out/cat.sqlite` or a plain `.sqlite` path |
| SQL Server, SQL login | `mssql+pyodbc://<user>:<password>@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server` |
| SQL Server, Windows login | `mssql+pyodbc://@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server&trusted_connection=yes` |
| SQL Server, raw ODBC string | `mssql+pyodbc:///?odbc_connect=<URL-encoded ODBC connection string>` |
| PostgreSQL | `postgresql+psycopg://<user>:<password>@<host>:5432/<database>` |

**Connect timeout:** SQL Server (`mssql+pyodbc`) and PostgreSQL (`+psycopg`, `+psycopg2` or plain
`postgresql://`) connections time out after 15 seconds:
`error: Cannot connect to target postgresql+psycopg://<user>:***@<host>/<db>: connection timeout expired`
(exit 1). If the URL has any query key containing `timeout` (for example `?connect_timeout=60` for
PostgreSQL, or `Connection Timeout` inside `odbc_connect`), that value is used instead.

**Missing driver:** a missing psycopg gives exit 1 with
`install the PostgreSQL driver: pip install acade2sql[postgresql]`. A missing SQL Server ODBC driver
gives exit 1 with `SQL Server needs pyodbc (pip install acade2sql[mssql]) and the Microsoft "ODBC
Driver 18 for SQL Server" (or 17) installed for this Python's bitness; ...`.

**Passwords** in URLs (including `PWD=` inside `odbc_connect`) are shown as `***` in all output,
including the report. Prefer `ACADE2SQL_TARGET` to a password on the command line.

## Exit codes

| Code | Meaning | Report written |
|---|---|---|
| `0` | Every selected table has status `pass` (or `planned` with `--dry-run`). Also returned when the filters select no table (`warning: no tables selected` on stderr). | yes (if `--report`) |
| `1` | An `error: ...` before the target was changed: source not found or not `.mdb`/`.accdb`; Access driver missing or wrong bitness; source locked or cannot be opened; table name not found (`--tables`, `inspect --table`); invalid target URL, unsupported dialect, missing database driver (psycopg, SQL Server ODBC driver), target unreachable or connect timeout, uncreatable target folder; PostgreSQL table, column or schema name over 63 bytes. | no |
| `2` | Usage error: from the argument parser (unknown option, bad `--batch-size`, missing `source`, unsupported `--mode`, empty name in `--tables`), or an invalid [option combination](#option-combinations). `--help` and `--version` exit 0. | no |
| `3` | Validation failure: at least one table is `fail` or `error` (also in a dry run). | yes (if `--report`) |
| `130` | Interrupted with Ctrl+C. | no |

`inspect` uses codes 0, 1, 2 and 130.

## Output streams

| Stream | `inspect` | `convert` |
|---|---|---|
| stdout | the table list or JSON | per-table lines and the summary line; with `--dry-run` also the plan footer, warnings and DDL |
| stderr | `error:` line, logs | statistics line, `warning: no tables selected`, `error:` line, logs |

## JSON report

Written by `convert --report PATH.json` (UTF-8, indented). Real example from a sync of the
synthetic fixture after three rows of the target had been changed, trimmed to two tables:

```json
{
  "source": "C:\\Users\\<user>\\docs2\\fixture.accdb",
  "target": "sqlite:///C:\\Users\\<user>\\docs2\\out\\sync.sqlite",
  "mode": "sync",
  "started": "2026-10-19T11:18:59.839917+00:00",
  "finished": "2026-10-19T11:19:00.430096+00:00",
  "ok": true,
  "dry_run": false,
  "schema": null,
  "summary": {
    "tables": 16,
    "passed": 16,
    "failed": 0,
    "errors": 0,
    "planned": 0,
    "source_rows": 12457,
    "target_rows": 12458
  },
  "tables": [
    {
      "name": "COMP",
      "source_rows": 5,
      "target_rows": 5,
      "status": "pass",
      "error": null,
      "seconds": 0.009405599965248257,
      "checksum_match": true,
      "warnings": [],
      "target_rows_before": null,
      "inserted": 0,
      "updated": 2,
      "deleted": 0,
      "unchanged": 3,
      "extra_rows": 0,
      "key": ["HDL"],
      "actions": []
    },
    {
      "name": "NETS",
      "source_rows": 2,
      "target_rows": 3,
      "status": "pass",
      "error": null,
      "seconds": 0.003974399995058775,
      "checksum_match": true,
      "warnings": [],
      "target_rows_before": null,
      "inserted": 0,
      "updated": 0,
      "deleted": 0,
      "unchanged": 2,
      "extra_rows": 1,
      "key": ["RECNUM"],
      "actions": []
    }
  ]
}
```

A dry-run report has `"dry_run": true`, `planned` statuses and a `ddl` list (one string per
statement, without the trailing `;`):

```json
{
  "target": "mssql+pyodbc://acade:***@sqlhost/acade?driver=ODBC+Driver+18+for+SQL+Server",
  "mode": "full",
  "ok": true,
  "dry_run": true,
  "schema": null,
  "summary": {"tables": 2, "passed": 0, "failed": 0, "errors": 0, "planned": 2,
              "source_rows": 45, "target_rows": 0},
  "tables": [
    {"name": "COMP", "source_rows": 5, "target_rows": null, "status": "planned",
     "key": null, "actions": ["drop", "create", "insert"], "...": "..."}
  ],
  "ddl": [
    "DROP TABLE IF EXISTS [_PINLIST_XX]",
    "CREATE TABLE [_PINLIST_XX] (\n\t[RECNUM] INTEGER NOT NULL, ..."
  ]
}
```

(Trimmed and reformatted; the real file has one field per line and every table field.)

| Field | Type | Meaning |
|---|---|---|
| `source` | string | Absolute path of the Access file. |
| `target` | string | Target URL with passwords masked (`***`). SQLite shows the plain path. |
| `mode` | string | `"full"`, `"append"` or `"sync"`. |
| `started`, `finished` | string | ISO-8601 UTC timestamps. |
| `ok` | bool | `true` when every table has status `pass` or `planned` (also `true` for an empty selection). |
| `dry_run` | bool | `true` for `--dry-run`. |
| `schema` | string or null | The `--schema` value. |
| `ddl` | list of string | Dry run only: the DDL statements in order. Absent otherwise. |
| `summary.tables` / `passed` / `failed` / `errors` / `planned` | int | Number of tables per status. |
| `summary.source_rows` / `target_rows` | int | Sums over all tables (unknown counts count as 0). |
| `tables[].name` | string | Table name as spelled in the source. |
| `tables[].source_rows`, `target_rows` | int or null | `null` when not counted (error, dry run, `--no-counts`). |
| `tables[].status` | string | `"pass"`, `"fail"`, `"error"` or `"planned"` (dry run). |
| `tables[].error` | string or null | Redacted message, prefixed `read failed:` or `write failed:` (plain message in a dry run). |
| `tables[].seconds` | float | Time spent on this table (`0.0` in a dry run). |
| `tables[].checksum_match` | bool or null | `null` unless `--checksum` was given (always `null` in append, or when the table errored). |
| `tables[].warnings` | list of string | For example `unmapped type <T> in column <C> stored as text`, `index X skipped on SQL Server: ...`, `checksum not computed in append mode`. |
| `tables[].target_rows_before` | int or null | `append`: target rows before the insert (`0` for a table it created). |
| `tables[].inserted` | int or null | `append` and `sync`: rows inserted. |
| `tables[].updated` | int or null | `sync`: rows updated (values differed). |
| `tables[].deleted` | int or null | `sync`: rows deleted (`--delete-missing`, or all previous rows with `--no-key replace`). |
| `tables[].unchanged` | int or null | `sync`: rows found with identical values. |
| `tables[].extra_rows` | int or null | `sync`: target rows whose key is not in the source, after the run. |
| `tables[].key` | list of string or null | `sync`: the key columns used; `null` for a table without a key. |
| `tables[].actions` | list of string | Dry run: the planned actions (see [Dry run](#dry-run)); empty otherwise. |

Counters that do not apply to the mode are `null` (for example all of them in `full` mode).

## Not yet supported

These are **not** available in 0.2.0. See the [Roadmap](../README.md#roadmap).

- Verified operation against a live SQL Server or PostgreSQL server. Both targets are checked only
  by compiling SQL for their dialects and by dry runs; the `live_db` tests have not been run.
- A reader that works without the Microsoft Access driver (`access-parser` fallback): milestone M5.
- Creating the target database or schema (`--schema` must name an existing schema).
- Sync of keys that differ only by case or trailing spaces on a case-insensitive target (use
  `--mode full`).
- Checksums in `append` mode.
- Other target dialects (MySQL, Oracle, ...).
- Converting Access queries (views), linked tables or `MSys*` system tables.
