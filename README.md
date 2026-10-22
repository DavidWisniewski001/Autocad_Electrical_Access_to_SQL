# acade2sql

`acade2sql` copies AutoCAD Electrical (ACADE) Microsoft Access databases (`.mdb` / `.accdb`) into a
SQL database. It handles the catalog (`default_cat.mdb`), the panel footprint lookup
(`footprint_lookup.accdb`) and other ACADE Access files. The result is a database you can query, join
and script against with ordinary SQL tools, without Access.

Version 0.3.0 writes to **SQLite**, **SQL Server** and **PostgreSQL**. It can reload a whole table
(`full`), add rows (`append`) or bring an earlier copy up to date (`sync`). It reads Access files
with the Microsoft Access ODBC driver or, where that driver is not installed, with a built-in
pure-Python reader. SQL Server and PostgreSQL have **not yet been tested against a live server**
(see [Limitations](#limitations-and-known-behaviour)).

```text
acade2sql convert default_cat.mdb --target default_cat.sqlite --checksum --report report.json
```

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Choosing the Access reader](#choosing-the-access-reader)
- [Keep a copy up to date with sync](#keep-a-copy-up-to-date-with-sync)
- [Choosing the target](#choosing-the-target)
- [Exit codes](#exit-codes)
- [Validation and data fidelity](#validation-and-data-fidelity)
- [Troubleshooting](#troubleshooting)
- [Limitations and known behaviour](#limitations-and-known-behaviour)
- [Roadmap](#roadmap)
- [Development](#development)
- [License](#license)

More detail: [Usage guide](docs/USAGE.md) · [CLI reference](docs/CLI.md) · [Changelog](docs/CHANGELOG.md)

## Features

### Released in 0.1.0 (milestones M1 to M3)

**Reading Access**
- The source is opened **read-only** (`ReadOnly=1` plus a read-only pyodbc connection) and is never
  modified. ODBC connection pooling is turned off, so closing the reader releases the file.
- The reader uses pyodbc and the Microsoft Access ODBC driver
  (`Microsoft Access Driver (*.mdb, *.accdb)`).
- Driver diagnostics: if the driver is missing, or is installed only for the other bitness
  (32-bit vs 64-bit), the tool says which installer you need. See
  [Installation](#1-install-the-microsoft-access-database-engine-recommended).
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
- Python API: `from acade2sql import convert` runs the same conversion and returns a report object.

### Released in 0.2.0 (milestone M4)

**Server targets** (not yet tested against a live server; see [Limitations](#limitations-and-known-behaviour))
- **SQL Server** through `mssql+pyodbc` and the Microsoft "ODBC Driver 18 (or 17) for SQL Server".
- **PostgreSQL** through `postgresql+psycopg` (install extra `postgresql`).
- `--schema NAME` writes into an existing schema.
- A 15-second connect timeout, so an unreachable server fails quickly (exit 1).
- SQL Server: an index too large for SQL Server is skipped with a warning; a primary key that is too
  large makes that table `error`. PostgreSQL: names over 63 bytes stop the run before anything is
  written; text containing a NUL character makes that table `error`.

**Modes**
- `--mode append` inserts the source rows into existing tables (missing tables are created). Nothing
  is dropped.
- `--mode sync` updates changed rows and inserts new ones, matched by a key (default: the primary
  key, `RECNUM` in the catalogs). Unchanged rows are not written, so a repeated sync writes nothing.
  `--key` chooses another key, `--no-key replace` handles tables without a key, and
  `--delete-missing` removes rows that are gone from the source.
- Every sync table is one transaction. The counters (`inserted`, `updated`, `deleted`, `extra`) are
  re-checked before commit.

**Planning**
- `--dry-run` prints the plan for every table and the exact DDL for the target dialect, without
  connecting to the target. `--no-counts` makes it faster on large files.

Real-catalog figures (64-bit Python 3.13, SQLite target):

| Source | Tables | Rows | Run | Time |
|---|---|---|---|---|
| `default_cat.mdb` | 168 | 1,594,493 | `--mode full` | ~2 min |
| `default_cat.mdb` | 168 | 1,594,493 | `--mode sync --checksum` after 6 changes to the target | ~5 min |
| `default_cat.mdb` | 168 | 1,594,493 | repeated `--mode sync` (no changes) | 2 min 17 s |
| `default_cat.mdb` | 168 | 1,594,493 | `--dry-run` for a SQL Server or PostgreSQL URL | ~0.7 s |
| `footprint_lookup.accdb` | 117 | 261,858 | `--mode full` | ~10 s |

### Released in 0.3.0 (milestone M5)

**Reading without the Access driver**
- A **pure-Python Access reader** (`--reader pure`) reads `.mdb` and `.accdb` files without the
  Microsoft Access ODBC driver and without pyodbc. It returns the same tables, types, keys and
  values as the ODBC reader, and never creates a lock file next to the source.
- `--reader {auto,odbc,pure}` on `inspect` and `convert`. The default `auto` uses the ODBC driver
  when it is usable and otherwise falls back to the pure reader with a warning. The report and
  `inspect --json` record which reader was used (`"reader"`).
- The pure reader decodes Access's compressed Unicode text itself (the access-parser library it
  builds on corrupts characters such as `–`). It never returns a partial table: if a table's pages
  do not add up to its stored row count, the table fails (`SourceIntegrityError`). It refuses
  password-protected or encrypted databases and attachment, multi-value and calculated fields with
  a clear error instead of guessing.
- If a `.ldb` / `.laccdb` lock file shows that the database is open in AutoCAD Electrical or
  Access, the pure reader warns and recommends closing the program or using `--copy-to-temp`.

**Tools and options**
- `acade2sql doctor` checks Python, the Access readers and the target drivers on this machine.
  `python -m acade2sql` also runs the CLI.
- `--progress` / `--no-progress`: one line per finished table on stderr (on by default in a
  terminal).
- `--tables-file` / `--exclude-file`: table names from a text file, one per line. This also selects
  names that contain a comma.
- `scripts/compare_readers.py` reads each table with both readers and reports any difference.
- A table with dates before 1753 gets a warning (see [Limitations](#limitations-and-known-behaviour)).

Verified on the sample databases with `scripts/compare_readers.py` (schema and a checksum of every
value, both readers):

| Source | Tables identical | ODBC reader | Pure reader |
|---|---|---|---|
| `footprint_lookup.accdb` | 117 / 117 | 3.4 s | 3.7 s |
| `default_cat.mdb` | 168 / 168 | 128.7 s | 81.6 s |

The 260 rows of `default_cat.mdb` table `MS` that contain `–` (U+2013) are read correctly, and
`acade2sql convert default_cat.mdb --reader pure --checksum` passes all 168 tables.

### Unreleased (milestone M7): desktop window

- `acade2sql-gui` (or `acade2sql gui`, or `python -m acade2sql.gui`) opens a tkinter window: pick
  the Access file, pick a SQLite file or fill in SQL Server / PostgreSQL connection fields (or
  paste a URL), set mode, reader, checksum, table filters, schema and report path, then **Dry run**
  (plan and DDL) or **Convert**. **Check setup** runs `doctor`; **List tables...** lets you pick
  tables to include or exclude.
- Work runs in the background: per-table progress and a results table (status, rows, changes,
  checksum, warnings, error; double-click a row for details), a log pane, **Cancel** (stops after
  the current batch) and **Save report...**. Passwords are masked in everything the window shows.
- Uses the same Python API as the CLI; no extra dependency (tkinter ships with Python on Windows).
  See [Use the desktop window](docs/USAGE.md#use-the-desktop-window).

## Requirements

- **Windows**. Tested on Windows 11 only. (The Access ODBC driver is Windows-only. The pure reader
  has no Windows-specific code, but other platforms have not been tested.)
- **Python 3.10 or newer**. Tested on Python 3.13, 64-bit.
- **Recommended:** the **Microsoft Access Database Engine 2016 Redistributable**, with the same
  bitness as your Python (see below). Without it, acade2sql uses its pure-Python reader
  automatically.
- Python packages, installed automatically: `access-parser>=0.0.6,<0.1` (Apache-2.0),
  `construct>=2.10`, `pyodbc>=4.0.35`, `sqlalchemy>=2.0`.
- For a **SQL Server** target: the Microsoft **ODBC Driver 18 for SQL Server** (or 17), same bitness
  as Python. The legacy Windows `SQL Server` driver and SQL Server Native Client are not supported.
- For a **PostgreSQL** target: the `postgresql` extra (installs `psycopg[binary]>=3.1`).

## Installation

### 1. Install the Microsoft Access Database Engine (recommended)

acade2sql works without this driver: `--reader auto` (the default) then uses the pure-Python reader
and logs a warning. The driver is still recommended. It is the reference reader that the pure
reader was verified against, `scripts/compare_readers.py` needs it, and the pure reader refuses
tables with attachment, multi-value or calculated fields and refers you to `--reader odbc` for
them. If you cannot install it (no admin
rights, a conflicting Office installation), skip this step; see
[Choosing the Access reader](#choosing-the-access-reader).

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

If the driver is missing, `--reader auto` falls back to the pure reader with a warning. With
`--reader odbc`, the command stops with exit code 1 and a message like this one:

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

Install extras (quote them in PowerShell and bash):

| Extra | Command | Installs | Needed for |
|---|---|---|---|
| (none) | `pip install -e .` | access-parser, construct, pyodbc, SQLAlchemy | both Access readers; SQLite targets; SQL Server targets (with the ODBC driver) |
| `postgresql` | `pip install -e ".[postgresql]"` | `psycopg[binary]>=3.1` | PostgreSQL targets |
| `mssql` | `pip install -e ".[mssql]"` | nothing extra (pyodbc is already installed) | SQL Server; also install "ODBC Driver 18 for SQL Server" from Microsoft |
| `dev` | `pip install -e ".[dev]"` | pytest, ruff | development |
| `fallback` | `pip install -e ".[fallback]"` | nothing (kept for compatibility; access-parser is a core dependency since 0.3.0) | |

Check the installation with `acade2sql doctor`. It lists the Access readers this Python can use
and the target drivers (output from a machine with the 64-bit Access driver):

```text
> acade2sql doctor
ok acade2sql                0.3.0
ok Python                   3.13.15 (64-bit) C:\...\python.exe
ok Access ODBC drivers      Microsoft Access Driver (*.mdb, *.accdb)
ok Access driver 32-bit     not installed
ok odbc reader              OK: 'Microsoft Access Driver (*.mdb, *.accdb)' is available to this 64-bit Python.
ok pure reader              access-parser 0.0.6, no driver needed
ok psycopg (PostgreSQL)     3.3.6
ok pyodbc (SQL Server)      5.3.0
-- SQL Server ODBC drivers  SQL Server: not supported by the writer (install ODBC Driver 17 or 18 for SQL Server)
usable readers: odbc, pure (--reader auto uses odbc)
```

`--` marks something that is missing or unusable. Here only the legacy SQL Server driver is
installed, which matters only for SQL Server targets. `doctor` exits 0 when at least one Access
reader is usable, else 1. `acade2sql --version` prints `acade2sql 0.3.0`.

## Quick start

**0. Check the machine** (once): `acade2sql doctor` (see [Installation](#2-install-acade2sql)).

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
...
19 tables, 12489 rows
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
19 tables: 19 passed, 0 failed, 0 errors
```

A statistics line goes to stderr:
`target=sqlite:///C:\...\default_cat.sqlite rows=12489 elapsed=0.6s rows_per_s=21887`.
In a terminal, stderr also shows a progress line as each table finishes (`[5/19] BIG 12345 rows
0.2s`); `--no-progress` turns it off.

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

## Choosing the Access reader

`inspect` and `convert` take `--reader`:

| `--reader` | Uses | When |
|---|---|---|
| `auto` (default) | `odbc` if the Access driver is usable, else `pure` (with a warning) | almost always |
| `odbc` | pyodbc and the Microsoft Access ODBC driver | to insist on the driver (fails with exit 1 if it is missing) |
| `pure` | built-in pure-Python reader; no driver, no pyodbc | no driver on the machine; files with Access "Large Number" columns |

Both readers open the source read-only and gave identical results on every table of the sample
databases. Differences that matter:

- The ODBC reader creates a `.ldb` / `.laccdb` lock file next to the source while it reads (use
  `--copy-to-temp` to avoid it). The pure reader never does.
- The ODBC driver misreads Access "Large Number" (`BIGINT`) columns, so the ODBC reader refuses a
  table that has one (status `error`, exit 3, "use --reader pure"). The pure reader reads them
  correctly.
- The pure reader refuses password-protected and encrypted databases, and tables with attachment,
  multi-value or calculated fields (exit 1, or that table `ERROR`), instead of returning wrong data.
- If AutoCAD Electrical has the database open, the pure reader warns; close ACADE or use
  `--copy-to-temp`.

To check a file with both readers: `python scripts/compare_readers.py <file>` (needs the driver).
Details: [usage guide](docs/USAGE.md#choose-a-reader) and
[CLI reference](docs/CLI.md#access-readers---reader).

## Keep a copy up to date with sync

Typical use: you keep a SQL copy of a project database and want to refresh it after the drawings
change, without dropping the tables (for example because reports or views point at them).

1. Create the copy once with a full conversion:

   ```text
   acade2sql convert MyProject.mdb --target C:\out\myproject.sqlite
   ```

2. After the drawings change, update it. Only changed rows are updated and new rows inserted.
   (Output below is from the synthetic test database, trimmed to the tables that changed.)

   ```text
   acade2sql convert MyProject.mdb --target C:\out\myproject.sqlite --mode sync --checksum
   ```

   ```text
   PASS COMP source=5 target=5 inserted=0 updated=2 deleted=0 extra=0 checksum=match
   PASS NETS source=2 target=3 inserted=0 updated=0 deleted=0 extra=1 checksum=match
   PASS WIRENO source=3 target=3 inserted=1 updated=0 deleted=0 extra=0 checksum=match
   ```

   `extra=1` means the target has a row whose key is no longer in the source. It is kept, and the
   table still passes.

3. To remove such rows as well, add `--delete-missing`:

   ```text
   acade2sql convert MyProject.mdb --target C:\out\myproject.sqlite --mode sync --delete-missing
   ```

   ```text
   PASS NETS source=2 target=2 inserted=0 updated=0 deleted=1 extra=0
   ```

Sync matches rows by the table's primary key. A table without one (for example `PNLCOMP` in the
test database) stops with `error=write failed: no key for sync; use --key or --no-key replace`.
Choose a key with `--key`, or add `--no-key replace` to reload such tables completely. See the
[usage guide](docs/USAGE.md#sync-a-project-database) for details.

## Choosing the target

The target is chosen in this order:

1. `--target URL|PATH`
2. the `ACADE2SQL_TARGET` environment variable
3. `<source name>.sqlite` in the **current directory** (for example `./default_cat.sqlite`)

| Target | Example `--target` / `ACADE2SQL_TARGET` value |
|---|---|
| SQLite (plain path) | `C:\out\cat.sqlite` (also `.sqlite3`, `.db`) |
| SQLite (URL) | `sqlite:///C:/out/cat.sqlite` |
| SQL Server | `mssql+pyodbc://<user>:<password>@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server` |
| SQL Server (Windows login) | `mssql+pyodbc://@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server&trusted_connection=yes` |
| PostgreSQL | `postgresql+psycopg://<user>:<password>@<host>:5432/<database>` |

- A plain path ending in `.sqlite`, `.sqlite3` or `.db` becomes a SQLite URL with an absolute path.
  Missing folders are created.
- For server targets the database (and the `--schema`, if given) must already exist. acade2sql
  creates tables, not databases or schemas.
- **Keep passwords off the command line.** Put the URL in `ACADE2SQL_TARGET` instead, so it does not
  end up in your shell history:

  ```powershell
  $env:ACADE2SQL_TARGET = "postgresql+psycopg://<user>:<password>@<host>/<database>"
  acade2sql convert default_cat.mdb --mode sync
  ```

  Passwords are shown as `***` in all output either way.
- More connection examples, including SQL Server `odbc_connect` strings:
  [usage guide](docs/USAGE.md#connect-to-sql-server).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success: every selected table passed (or, with `--dry-run`, was planned). An empty selection also exits 0, with `warning: no tables selected`. |
| `1` | Error before the target was changed: source missing or not `.mdb`/`.accdb`, Access driver missing or wrong bitness (with `--reader odbc`), password-protected or encrypted database (pure reader), unknown table name, invalid target URL, unsupported dialect, missing database driver (psycopg, SQL Server ODBC driver), target unreachable, PostgreSQL name over 63 bytes. Prints `error: ...` (no traceback unless `--verbose`). |
| `2` | Usage error: unknown option, `--batch-size 0`, missing source; `--key`, `--no-key` or `--delete-missing` without `--mode sync`; `--no-counts` without `--dry-run`; `--schema` with a SQLite target or an invalid schema name; a `--tables-file` / `--exclude-file` that cannot be read, or a `--tables-file` without names. |
| `3` | Validation failure: at least one table has status `fail` or `error`. The other tables are still processed. Also returned by `--dry-run` when a table could not be planned. |
| `130` | Interrupted (Ctrl+C). |

`acade2sql doctor` exits 0 when at least one Access reader is usable, else 1.

## Validation and data fidelity

Every table gets a status: `pass`, `fail` or `error` (`planned` in a dry run). What `pass` requires
depends on the mode:

| Mode | Always checked | With `--checksum` |
|---|---|---|
| `full` | source rows = target rows | checksum of all source rows = checksum of all target rows |
| `append` | target rows after = target rows before + source rows | not computed (warning `checksum not computed in append mode`) |
| `sync` | the number of target rows whose key is in the source = source rows; with `--delete-missing` also target rows = source rows | checksum of the source = checksum of the target rows whose key is in the source |

In sync, extra target rows (key not in the source) are counted as `extra` but do not fail the table.

**The checksum** reads every table a second time from both sides and compares an order-independent
SHA-256 checksum of all values. It distinguishes:
- `NULL` from `''` (empty string)
- leading and trailing spaces (text is never trimmed)
- every Unicode character (CJK, `µ`, `Ω`, emoji), and CR/LF inside text
- exact decimals, dates and times, binary data (including `\x00` bytes)

It ignores only these differences: GUID case and braces in GUID (Replication ID) columns, and
differences below single precision in Single (`REAL`) columns. Sync uses the same rules to decide
whether a row changed.

**Not checked without `--checksum`:** only row counts are compared. A truncated or altered value
would not be detected. `--checksum` roughly doubles the run time on large catalogs.

**Type map:**

| Access type | SQLite | SQL Server | PostgreSQL |
|---|---|---|---|
| Short Text `VARCHAR(n)` | `VARCHAR(n)` | `NVARCHAR(n)` | `VARCHAR(n)` |
| Long Text / MEMO `LONGCHAR` | `TEXT` | `NVARCHAR(max)` | `TEXT` |
| AutoNumber `COUNTER`, Long `INTEGER` | `INTEGER` | `INTEGER` | `INTEGER` |
| Integer `SMALLINT`, Byte `BYTE` | `SMALLINT` | `SMALLINT` | `SMALLINT` |
| `BIGINT` | `BIGINT` | `BIGINT` | `BIGINT` |
| Single `REAL` | `FLOAT` | `REAL` | `REAL` |
| Double `DOUBLE` | `DOUBLE` | `DOUBLE PRECISION` | `DOUBLE PRECISION` |
| Currency `CURRENCY` | `DECIMAL_TEXT` (exact text) | `NUMERIC(19, 4)` | `NUMERIC(19, 4)` |
| Decimal `DECIMAL(p,s)` | `DECIMAL_TEXT` (exact text) | `NUMERIC(p, s)` | `NUMERIC(p, s)` |
| Date/Time `DATETIME` | `DATETIME` (text) | `DATETIME2` | `TIMESTAMP WITHOUT TIME ZONE` |
| Yes/No `BIT` | `BOOLEAN` (`1`/`0`) | `BIT` | `BOOLEAN` |
| Replication ID `GUID` | `VARCHAR(36)` | `UNIQUEIDENTIFIER` | `UUID` |
| OLE Object / binary `LONGBINARY` | `BLOB` | `VARBINARY(max)` | `BYTEA` |

AutoNumber columns are plain integer columns in the target (no identity), so the source values are
kept. On SQL Server a `VARCHAR(n)` longer than 4,000 characters becomes `NVARCHAR(max)`. The SQL
Server and PostgreSQL columns above are taken from `--dry-run` output and the type map in
`typemap.py`.

An Access type that is not in the map is stored as text. The table is then marked `fail`, with
the warning `unmapped type <T> in column <C> stored as text`, even if its counts match.

The ACADE sample catalogs use only `VARCHAR`, `LONGCHAR` and `COUNTER`. The other rows of the table
are covered by the synthetic test database. Details: [docs/type_mapping.md](docs/type_mapping.md),
[docs/schema_inventory.md](docs/schema_inventory.md).

## Troubleshooting

Start with `acade2sql doctor`: it shows which Access readers and target drivers this Python can
use. Then:

| Symptom | What to do |
|---|---|
| `WARNING ... Microsoft Access ODBC driver not usable; falling back to the pure-Python reader` | Nothing, if the results are fine. To use the driver, install the Access Database Engine of your Python's bitness ([Installation](#1-install-the-microsoft-access-database-engine-recommended)). |
| `error: The Microsoft Access ODBC driver ... was not found` (with `--reader odbc`) | Install the driver, or use `--reader pure` / `auto`. |
| `WARNING ... the source database appears to be open in another program (lock file ... exists)` | Close AutoCAD Electrical / Access, or add `--copy-to-temp`. |
| `error: Cannot read ...: the database is password-protected` | Remove the password in Access, or convert a copy without one. Neither reader supports passwords. |
| A table `ERROR` with `read failed:` naming an attachment, multi-value or calculated field | Re-run that table with `--reader odbc`. |
| `-- SQL Server ODBC drivers  SQL Server: not supported by the writer` | Install "ODBC Driver 18 for SQL Server" (or 17) before using a SQL Server target. |

More: [usage guide](docs/USAGE.md#fix-access-odbc-driver-was-not-found),
[troubleshoot a failed table](docs/USAGE.md#troubleshoot-a-failed-table).

## Limitations and known behaviour

- **The ODBC driver misreads Access "Large Number" (`BIGINT`) columns** (it reports them as
  `CHAR(4)` and returns meaningless text). Whenever the driver reports a `CHAR(4)` column, the ODBC
  reader checks the file's own table definition. If the column is really a Large Number, that table
  fails with status `error` and a message telling you to use `--reader pure`, and no target table is
  created for it. Use `--reader pure` for files with Large Number columns. The ACADE sample
  databases have none.
- **Dates before 1753.** Both readers return such dates as the ODBC driver computes them, which can
  differ from what Access displays (for example `#1700-06-15#` reads as `1700-06-02`). The table
  gets the warning `dates before 1753 are read using the ODBC driver's calendar and may differ from
  what Access displays`; its status is not affected. The sample databases contain no such dates.
- **Jet 3 (Access 97) files** are implemented in the pure reader but untested (no sample file).
- **The pure reader** memory-maps the source and streams rows page by page; it has been measured
  only on the two sample databases. It refuses password-protected and encrypted databases and
  attachment, multi-value, calculated and Date/Time Extended fields.
- **SQL Server and PostgreSQL have not been tested against a live server.** Their SQL has been
  compiled for each dialect and checked with `--dry-run` on the real catalogs. Live tests exist
  (pytest marker `live_db`, environment variables `ACADE2SQL_TEST_PG_URL` and
  `ACADE2SQL_TEST_MSSQL_URL`) but have been skipped so far. Check the results (`--checksum`) before
  you rely on a server target. The writer does not support the legacy Windows `SQL Server` ODBC
  driver or SQL Server Native Client; use ODBC Driver 17 or 18 for SQL Server.
- **Sync and case-insensitive keys.** If the target's collation treats two keys as equal that the
  source keeps apart (`m1` and `M1`, or `m1` and `m1 `), sync stops that table with
  `... keys that differ only by case/trailing spaces can't be synced; re-run with --mode full` and
  leaves the target table unchanged. Use `--mode full` for that table.
- **SQL Server compares text case-insensitively** with its default collation (`..._CI_AS`). Text
  values are stored exactly, but SQL queries, unique indexes and sync key lookups ignore case. A
  unique index can therefore reject values that differ only by case.
- **Append assumes it is the only writer.** The row count before the insert is read separately; if
  another program writes to the table at the same time, the count check can fail (data is not
  corrupted). Append does not compute checksums. Re-appending rows that already exist fails with a
  duplicate-key error for tables with a key, and is rolled back.
- **Schema drift.** Append and sync never drop a table. If the existing target table lacks a source
  column, or has a `NOT NULL` column the source does not have, the table is `error`
  (`schema drift: ... (re-run with --mode full)`). Extra nullable target columns are fine.
- **Lock file while reading (ODBC reader).** The Access driver creates `<name>.ldb` (`.mdb`) or
  `<name>.laccdb` (`.accdb`) next to the source while it is connected, even in read-only mode. The
  file is removed when the run ends. Use `--copy-to-temp` (or `--reader pure`) if no file may
  appear next to the source, for example on a network share. When ACADE has the database open, use
  `--copy-to-temp` with either reader.
- **NOT NULL is not reported for most columns.** The driver (and, to match it, the pure reader) reports required (`NOT NULL`) text and
  number columns as nullable. Only primary-key columns, AutoNumber columns and Yes/No columns are
  created `NOT NULL` in the target.
- **Yes/No never reads as NULL.** Access stores a NULL Yes/No value as `False`, so it arrives as `0`.
- **No real ACADE project database has been tested yet.** Project tables (COMP, PNLCOMP, TERMS,
  WIRENO, WFRM2ALL, NETS) are covered only by synthetic test tables. The real catalog and footprint
  databases have been converted and synced with every table passing.
- `MSys*` system tables are skipped. Access queries (views) and linked tables are not converted.
  None occur in the sample catalogs.
- SQLite compares names case-insensitively for ASCII and needs double quotes around names with
  spaces or symbols: `SELECT * FROM "_PINLIST_BRUNDY/HUBBEL"`. See the
  [usage guide](docs/USAGE.md#query-the-result-in-sqlite).

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 to M3 | Schema inventory, reader and `inspect`, SQLite `convert` | Done (0.1.0) |
| M4 | SQL Server and PostgreSQL targets, `append` / `sync`, `--dry-run` | Done (0.2.0) |
| M5 | Pure-Python reader, `--reader`, `doctor`, progress, table list files, `compare_readers.py` | Done (0.3.0) |
| M7 | Desktop window (`acade2sql-gui`) | Done (unreleased) |

Still open (**not available in 0.3.0**):

| Item | Status |
|---|---|
| Run the `live_db` tests against real SQL Server and PostgreSQL servers | Not done yet |
| Convert and sync a real ACADE project database (not only catalogs and synthetic project tables) | Not done yet |

## Development

```text
pip install -e ".[dev]"
pytest                      # full suite (live_db tests skip without server URLs)
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
| `live_db` | Needs a live server. Set `ACADE2SQL_TEST_PG_URL` and/or `ACADE2SQL_TEST_MSSQL_URL` to a SQLAlchemy URL of a test database; skipped otherwise. |

Project layout:

```text
src/acade2sql/
  cli.py                      acade2sql inspect | convert | doctor
  __main__.py                 python -m acade2sql
  converter.py                convert(), resolve_target() - the public API
  modes.py                    full / append / sync per table
  keys.py                     sync key selection (--key)
  plan.py                     --dry-run (plan and DDL, no target connection)
  readers/__init__.py         open_reader() and --reader selection
  readers/pyodbc_reader.py    read-only Access reader (pyodbc + Access ODBC driver)
  readers/access_parser_reader.py  pure-Python Access reader (no driver)
  diagnostics.py              driver / bitness detection and messages
  doctor.py                   acade2sql doctor
  schema.py                   TableInfo / ColumnInfo / IndexInfo
  typemap.py                  Access -> SQLAlchemy type map (sqlite, mssql, postgresql)
  writers/sqlalchemy_writer.py  DDL, batched inserts, target connection
  writers/sync.py             sync (upsert) and --delete-missing
  writers/dialects.py         SQL Server key sizes, PostgreSQL name length and NUL checks
  validate.py                 per-table results, JSON report, checksum
  redact.py                   password masking
scripts/compare_readers.py    compare the pure and ODBC readers on Access files
tests/                        pytest suite; tests/fixtures/build_access.py builds the synthetic DBs
docs/                         design and user documentation
```

Design documents: [PLAN.md](PLAN.md) (requirements and milestones),
[docs/DECISIONS.md](docs/DECISIONS.md) (design decisions),
[docs/M3_SPEC.md](docs/M3_SPEC.md), [docs/M4_SPEC.md](docs/M4_SPEC.md),
[docs/M5_SPEC.md](docs/M5_SPEC.md),
[docs/schema_inventory.md](docs/schema_inventory.md), [docs/type_mapping.md](docs/type_mapping.md).

## License

Proprietary (as declared in `pyproject.toml`). No open-source license has been granted.
