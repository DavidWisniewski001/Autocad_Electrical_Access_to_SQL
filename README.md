# acade2sql

`acade2sql` copies AutoCAD Electrical (ACADE) Microsoft Access databases (`.mdb` / `.accdb`) into a
SQL database. It handles the catalog (`default_cat.mdb`), the panel footprint lookup
(`footprint_lookup.accdb`) and other ACADE Access files. The result is a database you can query, join
and script against with ordinary SQL tools, without Access.

Version 0.1.0 writes to **SQLite**. SQL Server and PostgreSQL targets are in progress (see
[Roadmap](#roadmap)).

```text
acade2sql convert default_cat.mdb --target default_cat.sqlite --checksum --report report.json
```

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Choosing the target](#choosing-the-target)
- [Exit codes](#exit-codes)
- [Validation and data fidelity](#validation-and-data-fidelity)
- [Limitations and known behaviour](#limitations-and-known-behaviour)
- [Roadmap](#roadmap)
- [Development](#development)
- [License](#license)

More detail: [Usage guide](docs/USAGE.md) · [CLI reference](docs/CLI.md) · [Changelog](docs/CHANGELOG.md)

## Features

These features are in release 0.1.0 (milestones M1 to M3).

**Reading Access**
- The source is opened **read-only** (`ReadOnly=1` plus a read-only pyodbc connection) and is never
  modified. ODBC connection pooling is turned off, so closing the reader releases the file.
- The reader uses pyodbc and the Microsoft Access ODBC driver
  (`Microsoft Access Driver (*.mdb, *.accdb)`).
- Driver diagnostics: if the driver is missing, or is installed only for the other bitness
  (32-bit vs 64-bit), the tool says which installer you need. See
  [Installation](#1-install-the-microsoft-access-database-engine).
- `--copy-to-temp` reads from a temporary copy, so no lock file appears next to your source.

**Inspecting**
- `acade2sql inspect` lists every user table with its column count, row count and primary key.
  `--table NAME` adds full column and index detail. `--json` writes a JSON document.

**Converting** (`acade2sql convert`, SQLite target, `full` mode)
- Table and column names are copied **exactly**: spaces, `/`, `-`, `&` and case are kept, and the
  names are quoted in SQL. For example, `_PINLIST_BRUNDY/HUBBEL` and `ALLEN BRADLEY` /
  `ALLEN-BRADLEY` stay separate tables.
- A table-driven type map converts Access types to SQL types (see
  [Validation and data fidelity](#validation-and-data-fidelity)).
- Primary keys: an Access `PrimaryKey` index is used when the driver reports one (`discovered`).
  Otherwise a table with exactly one AutoNumber column (in ACADE catalogs, `RECNUM`) gets that column
  as its key (`inferred`). AutoNumber values are copied as they are, not renumbered.
- Indexes are recreated, including unique ones. Target index names are `ix_<table>_<index>`,
  sanitised and kept unique.
- Rows are streamed in batches (`--batch-size`, default 5000), with one transaction per table.
- `--tables` / `--exclude` select tables. Names are matched case-insensitively.
- Re-running drops and reloads only the converted tables. Other tables in the target are not touched.
- **Validation** for every table: source and target row counts are compared. With `--checksum`, an
  order-independent checksum of every value is compared as well.
- A console summary, an optional **JSON report** (`--report`) and **exit codes** for scripting.
- Passwords in target URLs are masked (`***`) in the report, log lines and error messages.
- `--verbose` turns on structured `key=value` debug logging on stderr.

**Python API**
- `from acade2sql import convert` runs the same conversion from Python and returns a report object.

Real-catalog figures (64-bit Python 3.13, SQLite target):

| Source | Tables | Rows | Convert time |
|---|---|---|---|
| `default_cat.mdb` | 168 | 1,594,493 | ~2 min (~4 min 43 s with `--checksum`) |
| `footprint_lookup.accdb` | 117 | 261,858 | ~10 s |

## Requirements

- **Windows**. The Microsoft Access ODBC driver is Windows-only.
- **Python 3.10 or newer**. Tested on Python 3.13, 64-bit.
- The **Microsoft Access Database Engine 2016 Redistributable**, with the same bitness as your
  Python (see below).
- Python packages, installed automatically: `pyodbc>=4.0.35`, `sqlalchemy>=2.0`.

## Installation

### 1. Install the Microsoft Access Database Engine

The ODBC driver must have the **same bitness as Python**. A 64-bit Python cannot use a 32-bit driver,
and a 32-bit Python cannot use a 64-bit one.

Check your Python's bitness:

```text
python -c "import struct; print(struct.calcsize('P') * 8)"
```

This prints `64` or `32`. Then download the Microsoft Access Database Engine 2016 Redistributable
from <https://www.microsoft.com/en-us/download/details.aspx?id=54920> and run:

| Python | Installer |
|---|---|
| 64-bit | `accessdatabaseengine_X64.exe` |
| 32-bit | `accessdatabaseengine.exe` |

If a different-bitness Microsoft Office is already installed, the installer may refuse to run. You
can run it with `/quiet`, or use a Python of the other bitness.

If the driver is missing, every command stops with exit code 1 and a message like this one:

```text
error: The Microsoft Access ODBC driver 'Microsoft Access Driver (*.mdb, *.accdb)' was not found for
this 64-bit Python. Install the 64-bit (x64) 'Microsoft Access Database Engine 2016 Redistributable'
(accessdatabaseengine_X64.exe) from https://www.microsoft.com/en-us/download/details.aspx?id=54920 so
that the ODBC driver 'Microsoft Access Driver (*.mdb, *.accdb)' is available to this 64-bit Python
(C:\...\python.exe). The driver bitness must match the Python bitness. If a 32-bit Microsoft Office is
installed, either run the installer with /quiet or use a 32-bit Python instead.
```

If the driver exists only for the other bitness, the message starts with *"The Microsoft Access ODBC
driver is installed only for 32-bit applications, but this Python is 64-bit (32/64-bit mismatch)."*

### 2. Install acade2sql

PowerShell:

```powershell
git clone https://github.com/DavidWisniewski001/Autocad_Electrical_Access_to_SQL.git
cd Autocad_Electrical_Access_to_SQL
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

bash (Git Bash):

```bash
git clone https://github.com/DavidWisniewski001/Autocad_Electrical_Access_to_SQL.git
cd Autocad_Electrical_Access_to_SQL
python -m venv .venv
source .venv/Scripts/activate
pip install -e .
```

For development (pytest, ruff), run `pip install -e ".[dev]"` instead.

Check the installation:

```text
acade2sql --version
acade2sql 0.1.0
```

## Quick start

**1. Inspect the database.**

```text
acade2sql inspect default_cat.mdb
```

This prints one line per table, then a total line. Output from the synthetic test database:

```text
_PINLIST_BRUNDY/HUBBEL     2 cols          2 rows  pk=RECNUM (inferred)
_PINLIST_XX                6 cols         40 rows  pk=RECNUM (inferred)
ALLEN BRADLEY              5 cols          2 rows  pk=RECNUM (inferred)
...
TERMS                      4 cols          3 rows  pk=HDL,TERM (discovered)
16 tables, 12457 rows
```

**2. Convert to SQLite.** Write a JSON report and verify every value:

```text
acade2sql convert default_cat.mdb --target default_cat.sqlite --checksum --report report.json
```

The console shows one line per table and a summary:

```text
PASS _PINLIST_BRUNDY/HUBBEL source=2 target=2 checksum=match
PASS _PINLIST_XX source=40 target=40 checksum=match
...
16 tables: 16 passed, 0 failed, 0 errors
```

A statistics line goes to stderr:
`target=sqlite:///C:\...\default_cat.sqlite rows=12457 elapsed=0.5s rows_per_s=24156`.

**3. Check the result.** The exit code is `0` when every table passed. `report.json` contains each
table's counts and status (see [the report format](docs/CLI.md#json-report)).

PowerShell:

```powershell
acade2sql convert default_cat.mdb --target default_cat.sqlite --report report.json
if ($LASTEXITCODE -ne 0) { Write-Error "conversion failed: exit $LASTEXITCODE" }
```

bash:

```bash
acade2sql convert default_cat.mdb --target default_cat.sqlite --report report.json \
  || echo "conversion failed: exit $?"
```

Put the source path **before** `--tables` / `--exclude`. These options take several values, so a
path written after them is read as a table name.

## Choosing the target

The target is chosen in this order:

1. `--target URL|PATH`
2. the `ACADE2SQL_TARGET` environment variable
3. `<source name>.sqlite` in the **current directory** (for example `./default_cat.sqlite`)

A plain path ending in `.sqlite`, `.sqlite3` or `.db` is turned into a SQLite URL with an absolute
path. Missing folders are created. You can also pass a SQLAlchemy URL such as
`sqlite:///C:/data/cat.sqlite`.

In 0.1.0 only SQLite is supported. The CLI accepts other SQLAlchemy URLs, but SQL Server and
PostgreSQL conversion has not been tested yet (milestone M4).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success: every selected table passed (an empty selection also exits 0, with `warning: no tables selected`) |
| `1` | Error before the target was changed: source missing or not `.mdb`/`.accdb`, Access driver missing or wrong bitness, unknown table name, invalid or unreachable target, target folder cannot be created. Prints `error: ...` (no traceback unless `--verbose`) |
| `2` | Command-line usage error (unknown option, `--batch-size 0`, missing source) |
| `3` | Validation failure: at least one table has status `fail` (count or checksum mismatch, unmapped type) or `error` (that table could not be read or written; the other tables are still converted) |
| `130` | Interrupted (Ctrl+C) |

## Validation and data fidelity

**Always checked:** for every table, `COUNT(*)` in the source equals `COUNT(*)` in the target.

**Checked with `--checksum`:** every table is read a second time from both sides. An
order-independent SHA-256 checksum of all values is compared. The checksum distinguishes:
- `NULL` from `''` (empty string)
- leading and trailing spaces (text is never trimmed)
- every Unicode character (CJK, `µ`, `Ω`, emoji), and CR/LF inside text
- exact decimals, dates and times, binary data (including `\x00` bytes), and GUIDs

The comparison ignores GUID case and braces only.

**Not checked without `--checksum`:** without it, only row counts are compared. A truncated or altered
value would not be detected. `--checksum` roughly doubles the run time on large catalogs.

**Type map (Access → SQLite):**

| Access type | SQLite column | Stored as |
|---|---|---|
| Short Text `VARCHAR(n)` | `VARCHAR(n)` | text, verbatim |
| Long Text / MEMO `LONGCHAR` | `TEXT` | text, verbatim (no 1 GB declared size) |
| AutoNumber `COUNTER`, Long `INTEGER` | `INTEGER` | integer, original values |
| Integer `SMALLINT`, Byte `BYTE` | `SMALLINT` | integer |
| `BIGINT` | `BIGINT` | integer |
| Single `REAL` | `FLOAT` | float |
| Double `DOUBLE` | `DOUBLE` | float |
| Currency `CURRENCY`, Decimal `DECIMAL(p,s)` | `DECIMAL_TEXT` | exact decimal as text, e.g. `'1234.5678'` |
| Date/Time `DATETIME` | `DATETIME` | text `YYYY-MM-DD HH:MM:SS.ffffff` |
| Yes/No `BIT` | `BOOLEAN` | `1` / `0` |
| Replication ID `GUID` | `VARCHAR(36)` | upper-case GUID without braces |
| OLE Object / binary `LONGBINARY` | `BLOB` | bytes |

An Access type that is not in the map is stored as text. The table is then marked `fail`, with
the warning `unmapped type <T> in column <C> stored as text`, even if its counts match.

The ACADE sample catalogs use only `VARCHAR`, `LONGCHAR` and `COUNTER`. The other rows of the table
are covered by the synthetic test database. Details: [docs/type_mapping.md](docs/type_mapping.md),
[docs/schema_inventory.md](docs/schema_inventory.md).

## Limitations and known behaviour

- **Lock file while reading.** The Access driver creates `<name>.ldb` (`.mdb`) or `<name>.laccdb`
  (`.accdb`) next to the source while it is connected, even in read-only mode. The file is removed
  when the run ends. Use `--copy-to-temp` if no file may appear next to the source, for example on
  a network share or when ACADE has the database open.
- **NOT NULL is not reported for most columns.** The driver reports required (`NOT NULL`) text and
  number columns as nullable. Only primary-key columns, AutoNumber columns and Yes/No columns are
  created `NOT NULL` in the target.
- **Yes/No never reads as NULL.** Access stores a NULL Yes/No value as `False`, so it arrives as `0`.
- **No real ACADE project database has been tested yet.** Project tables (COMP, PNLCOMP, TERMS,
  WIRENO, WFRM2ALL, NETS) are covered only by synthetic test tables. The real catalog and footprint
  databases have been converted with every table passing.
- `MSys*` system tables are skipped. Access queries (views) and linked tables are not converted.
  None occur in the sample catalogs.
- Only `--mode full` exists: converted tables are dropped and reloaded each run.
- SQLite compares names case-insensitively for ASCII and needs double quotes around names with
  spaces or symbols: `SELECT * FROM "_PINLIST_BRUNDY/HUBBEL"`. See the
  [usage guide](docs/USAGE.md#query-the-result-in-sqlite).

## Roadmap

These features are **not available in 0.1.0**.

| Milestone | Planned scope | Status |
|---|---|---|
| **M4** | SQL Server (`mssql+pyodbc`) and PostgreSQL targets; `--mode append` and `--mode sync` (upsert by key, `--key`, `--delete-missing`); `--dry-run` (plan and DDL without touching the target); `--schema NAME` | In progress ([spec](docs/M4_SPEC.md)) |
| **M5** | Pure-Python fallback reader (`access-parser`) for machines without the Access driver; CLI polish | Planned |

The `fallback` extra in `pyproject.toml` already installs `access-parser`, but 0.1.0 does not use it.

## Development

```text
pip install -e ".[dev]"
pytest                      # full suite
pytest -m "not slow"        # skip long-running tests
ruff check src tests
ruff format --check src
```

Test markers:

| Marker | Meaning |
|---|---|
| `requires_driver` | Needs the Access ODBC driver. Skipped automatically when it is missing. The tests build their own synthetic `.mdb` / `.accdb` files. |
| `real_data` | Runs against `data_files/default_cat.mdb` and `data_files/footprint_lookup.accdb`. Skipped when those files are absent (`data_files/` is not in the repository). |
| `slow` | Long-running tests. |

Project layout:

```text
src/acade2sql/
  cli.py                      acade2sql inspect | convert
  converter.py                convert(), resolve_target() - the public API
  readers/pyodbc_reader.py    read-only Access reader (pyodbc + Access ODBC driver)
  diagnostics.py              driver / bitness detection and messages
  schema.py                   TableInfo / ColumnInfo / IndexInfo
  typemap.py                  Access -> SQLAlchemy type map (sqlite, mssql, postgresql)
  writers/sqlalchemy_writer.py  DDL and batched inserts
  validate.py                 per-table results, JSON report, checksum
  redact.py                   password masking
tests/                        pytest suite; tests/fixtures/build_access.py builds the synthetic DBs
docs/                         design and user documentation
```

Design documents: [PLAN.md](PLAN.md) (requirements and milestones),
[docs/DECISIONS.md](docs/DECISIONS.md) (design decisions),
[docs/M3_SPEC.md](docs/M3_SPEC.md), [docs/M4_SPEC.md](docs/M4_SPEC.md),
[docs/schema_inventory.md](docs/schema_inventory.md), [docs/type_mapping.md](docs/type_mapping.md).

## License

Proprietary (as declared in `pyproject.toml`). No open-source license has been granted.
