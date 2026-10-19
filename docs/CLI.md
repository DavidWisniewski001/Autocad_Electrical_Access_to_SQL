# acade2sql command-line reference

Reference for acade2sql **0.1.0**. Everything here comes from `src/acade2sql/cli.py` and was checked
against `--help` and real runs on the synthetic test database. For worked examples, see the
[usage guide](USAGE.md).

- [acade2sql](#acade2sql)
- [acade2sql inspect](#acade2sql-inspect)
- [acade2sql convert](#acade2sql-convert)
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
| `--version` | | Print `acade2sql 0.1.0` and exit (code 0). |

A command (`inspect` or `convert`) is required.

**Log format** (stderr): `<local time> <LEVEL> <logger> <message> key=value ...`. For example:

```text
2026-10-19T05:30:05.379 INFO acade2sql.converter converted table table=NETS rows=2 seconds=0.007 rows_per_s=307 status=pass
```

Without `--verbose`, only WARNING and ERROR lines are logged.

## acade2sql inspect

List the user tables of an Access database with column counts, row counts and primary keys.
`MSys*` system tables are not listed.

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
| `none` (`pk=- (none)`) | No key. The target table is created without a primary key. |

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

Convert an Access database to a SQL database. In 0.1.0 the target is SQLite.

```text
usage: acade2sql convert [-h] [-v] [--target URL|PATH] [--mode {full}]
                         [--tables T [T ...]] [--exclude T [T ...]]
                         [--batch-size N] [--checksum] [--copy-to-temp]
                         [--report PATH.json]
                         source
```

| Argument / option | Default | Description |
|---|---|---|
| `source` | required | Access database, `.mdb` or `.accdb`. Opened read-only. Write it **before** `--tables` / `--exclude`. |
| `--target URL\|PATH` | `$ACADE2SQL_TARGET`, else `./<source stem>.sqlite` | Where to write. See [Target resolution](#target-resolution). |
| `--mode {full}` | `full` | `full`: for each selected table, drop it in the target if it exists, create it with its indexes, then load all rows. Tables not selected are left alone. `full` is the only mode in 0.1.0. |
| `--tables T [T ...]` | all user tables | Convert only these tables. Exact names, case-insensitive. An unknown name gives exit 1 before anything is written. |
| `--exclude T [T ...]` | none | Skip these tables. Exact names, case-insensitive. Unknown names are ignored. Wins over `--tables`. |
| `--batch-size N` | `5000` | Rows fetched and inserted per batch. Must be an integer of 1 or more; otherwise exit 2. Each table is still loaded in one transaction. |
| `--checksum` | off | Also compare an order-independent checksum of all values in each table. Every table is read a second time from both sides (on `default_cat.mdb`, about 4 min 43 s instead of about 2 min). |
| `--copy-to-temp` | off | Copy the source to a temporary folder (`acade2sql_*` in `%TEMP%`) and read the copy. No `.ldb`/`.laccdb` lock file appears next to the source. The copy is deleted at the end. |
| `--report PATH.json` | none | Write the [JSON report](#json-report) here. Parent folders are created. Written whenever conversion starts (exit codes 0 and 3); not written on exit 1. |
| `-v`, `--verbose` | off | Debug logging and tracebacks. |

The `--batch-size` default (`5000`) does not appear in `--help`.

### convert console output

stdout: one line per table, in source table order, then a summary.

```text
PASS NETS source=2 target=2 checksum=match
ERROR NETS source=2 target=- error=write failed: OperationalError: use DROP VIEW to delete view NETS
16 tables: 16 passed, 0 failed, 0 errors
```

Line format: `<PASS|FAIL|ERROR> <table> source=<n|-> target=<n|->`. These parts are added when they
apply: ` checksum=match|MISMATCH` (with `--checksum`), ` warnings=<n>`, ` error=<message>`.

| Status | Meaning |
|---|---|
| `PASS` | Source and target row counts match, the checksum matches (if requested), and there are no warnings. |
| `FAIL` | The table was copied, but the counts differ, the checksum differs, or a column has an Access type that is not in the type map (stored as text, with warning `unmapped type <T> in column <C> stored as text`). |
| `ERROR` | Reading, creating or writing this table raised an error. The message starts with `read failed:` (Access side) or `write failed:` (target side). The table's load was rolled back, and the other tables were still converted. |

stderr: one statistics line per run:

```text
target=sqlite:///C:\...\out\fixture.sqlite rows=12457 elapsed=0.5s rows_per_s=24156
```

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
- Missing folders for a SQLite target are created.
- Passwords in URLs are shown as `***` in all output, including the report. If you use a server URL
  later (M4), prefer `ACADE2SQL_TARGET` to a password on the command line, for example
  `postgresql+psycopg://<user>:<password>@<host>/<db>`.

## Exit codes

| Code | Meaning | Report written |
|---|---|---|
| `0` | Every selected table has status `pass`. Also returned when the filters select no table (`warning: no tables selected` on stderr). | yes (if `--report`) |
| `1` | An `error: ...` before the target was changed: source not found or not `.mdb`/`.accdb`; Access driver missing or wrong bitness; source locked or cannot be opened; table name not found (`--tables`, `inspect --table`); invalid target URL, missing database driver module, unreachable target, uncreatable target folder. | no |
| `2` | Usage error from the argument parser (unknown option, bad `--batch-size`, missing `source`, unsupported `--mode`). `--help` and `--version` exit 0. | no |
| `3` | Validation failure: at least one table is `fail` or `error`. | yes (if `--report`) |
| `130` | Interrupted with Ctrl+C. | no |

`inspect` uses codes 0, 1, 2 and 130.

## Output streams

| Stream | `inspect` | `convert` |
|---|---|---|
| stdout | the table list or JSON | per-table lines and the summary line |
| stderr | `error:` line, logs | statistics line, `warning: no tables selected`, `error:` line, logs |

## JSON report

Written by `convert --report PATH.json` (UTF-8, indented). Real example from the synthetic fixture,
trimmed to two tables:

```json
{
  "source": "C:\\Users\\<user>\\docs\\fixture.accdb",
  "target": "sqlite:///C:\\Users\\<user>\\docs\\out\\fixture.sqlite",
  "mode": "full",
  "started": "2026-10-19T09:29:33.594651+00:00",
  "finished": "2026-10-19T09:29:34.109278+00:00",
  "ok": true,
  "summary": {
    "tables": 16,
    "passed": 16,
    "failed": 0,
    "errors": 0,
    "source_rows": 12457,
    "target_rows": 12457
  },
  "tables": [
    {
      "name": "_PINLIST_BRUNDY/HUBBEL",
      "source_rows": 2,
      "target_rows": 2,
      "status": "pass",
      "error": null,
      "seconds": 0.00797849998343736,
      "checksum_match": true,
      "warnings": []
    },
    {
      "name": "XX",
      "source_rows": 40,
      "target_rows": 40,
      "status": "pass",
      "error": null,
      "seconds": 0.003792500006966293,
      "checksum_match": true,
      "warnings": []
    }
  ]
}
```

A table with an error (from a run where the target already had a view named `NETS`):

```json
{
  "name": "NETS",
  "source_rows": 2,
  "target_rows": null,
  "status": "error",
  "error": "write failed: OperationalError: use DROP VIEW to delete view NETS",
  "seconds": 0.0038528000004589558,
  "checksum_match": null,
  "warnings": []
}
```

| Field | Type | Meaning |
|---|---|---|
| `source` | string | Absolute path of the Access file. |
| `target` | string | Target URL with passwords masked (`***`). SQLite shows the plain path. |
| `mode` | string | `"full"`. |
| `started`, `finished` | string | ISO-8601 UTC timestamps. |
| `ok` | bool | `true` when every table has `status: "pass"` (also `true` for an empty selection). |
| `summary.tables` / `passed` / `failed` / `errors` | int | Number of tables per status. |
| `summary.source_rows` / `target_rows` | int | Sums over all tables (unknown counts count as 0). |
| `tables[].name` | string | Table name as spelled in the source. |
| `tables[].source_rows`, `target_rows` | int or null | `null` when the count could not be taken. |
| `tables[].status` | string | `"pass"`, `"fail"` or `"error"`. |
| `tables[].error` | string or null | Redacted message, prefixed `read failed:` or `write failed:`. |
| `tables[].seconds` | float | Time spent on this table. |
| `tables[].checksum_match` | bool or null | `null` unless `--checksum` was given (or the table errored). |
| `tables[].warnings` | list of string | For example `unmapped type <T> in column <C> stored as text`. |

## Not yet supported

These are **not** available in 0.1.0. See the [Roadmap](../README.md#roadmap).

- `--mode append` and `--mode sync` (with `--key`, `--no-key`, `--delete-missing`): milestone M4.
- `--dry-run`, `--no-counts` for convert, and `--schema NAME`: milestone M4.
- Tested SQL Server and PostgreSQL targets. The CLI passes such URLs to SQLAlchemy, but only SQLite
  is verified in 0.1.0 (M4).
- A reader that works without the Microsoft Access driver (`access-parser` fallback): milestone M5.
- Converting Access queries (views), linked tables or `MSys*` system tables.
