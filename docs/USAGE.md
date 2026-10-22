# acade2sql usage guide

Task-oriented examples for acade2sql 0.3.0. For every option and default, see the
[CLI reference](CLI.md). For installation, see the [README](../README.md#installation).

All examples were run against the synthetic test database built by
`tests/fixtures/build_access.py`. Output is pasted from those runs and trimmed where marked `...`.
Examples recorded with 0.2.0 show the fixture's 16 tables of that time; the 0.3.0 fixture has 19.
Where a target had to be "out of date", a copy of the SQLite target was edited with Python's
`sqlite3`; the Access source was never changed. SQL Server and PostgreSQL examples use `--dry-run`
with placeholder credentials and did not connect to any server.

- [Use the desktop window](#use-the-desktop-window)
- [Check your machine with doctor](#check-your-machine-with-doctor)
- [Convert on a machine without the Access driver](#convert-on-a-machine-without-the-access-driver)
- [Choose a reader](#choose-a-reader)
- [Compare the two readers](#compare-the-two-readers)
- [Convert a catalog to SQLite](#convert-a-catalog-to-sqlite)
- [Watch progress](#watch-progress)
- [Look before you convert](#look-before-you-convert)
- [Convert selected tables](#convert-selected-tables)
- [Convert the tables listed in a file](#convert-the-tables-listed-in-a-file)
- [Verify every value with checksums](#verify-every-value-with-checksums)
- [Sync a project database](#sync-a-project-database)
- [Choose a sync key](#choose-a-sync-key)
- [Delete rows removed from the source](#delete-rows-removed-from-the-source)
- [Append rows](#append-rows)
- [Preview with --dry-run](#preview-with---dry-run)
- [Connect to SQL Server](#connect-to-sql-server)
- [Connect to PostgreSQL](#connect-to-postgresql)
- [Write into a schema](#write-into-a-schema)
- [Script a conversion with exit codes and the JSON report](#script-a-conversion-with-exit-codes-and-the-json-report)
- [Use the Python API](#use-the-python-api)
- [Fix "Access ODBC driver was not found"](#fix-access-odbc-driver-was-not-found)
- [Avoid the lock file with --copy-to-temp](#avoid-the-lock-file-with---copy-to-temp)
- [Fix "the source database appears to be open in another program"](#fix-the-source-database-appears-to-be-open-in-another-program)
- [Query the result in SQLite](#query-the-result-in-sqlite)
- [Troubleshoot a failed table](#troubleshoot-a-failed-table)
- [Fix "keys that differ only by case/trailing spaces"](#fix-keys-that-differ-only-by-casetrailing-spaces)
- [Fix "schema drift"](#fix-schema-drift)

## Your source files are safe

acade2sql opens every Access file **read-only** and never writes to it, with either reader. While
the ODBC reader reads, the Access driver creates a temporary lock file next to the source (see
[Avoid the lock file](#avoid-the-lock-file-with---copy-to-temp)). That lock file is the only thing
created next to the source, and it is removed when the run ends. The pure reader creates nothing
next to the source.

## Use the desktop window

`acade2sql-gui` opens a window that does what `convert` and `doctor` do (also `acade2sql gui` or
`python -m acade2sql.gui`). It is installed with the package; on Windows `acade2sql-gui.exe` starts
without a console window.

1. **Source**: *Browse...* to the `.mdb` / `.accdb` file. The SQLite target path follows it
   (`<folder>\<name>.sqlite`) until you change it.
2. **Target**: *SQLite file*, *SQL Server* (host, optional instance and port, database, user and
   password or *Windows login*, ODBC driver, *Trust server certificate*), *PostgreSQL* (host, port
   5432 by default, database, user, password, SSL mode) or *Custom URL* (any SQLAlchemy URL; hidden
   while typing unless *Show* is ticked). The line under the fields shows the resulting URL with
   the password replaced by `***`.
3. **Options**: mode, reader, batch size, *Copy source to temp* (`--copy-to-temp`), *Checksum*,
   *Count rows in dry run* (off = `--no-counts`), table names to convert or exclude
   (comma-separated), schema, report JSON path. The sync fields (keys separated by `;`, *Table
   without key*, *Delete missing rows*) are only enabled in `sync` mode.
4. **Check setup** runs `doctor` (tab *Setup check*). **List tables...** reads the table names and
   lets you copy a selection into *Tables* or *Exclude*.
5. **Dry run** fills the results table with the planned actions and shows the plan and DDL in the
   *Plan / DDL* tab; nothing is written. **Convert** runs the conversion; `full` mode asks for
   confirmation when the target already exists (its tables are dropped and re-created).
6. While it runs, each table appears as `RUNNING`, then `PASS` / `FAIL` / `ERROR` with its row
   counts, changes, checksum and warning count. Double-click a row for the warnings and the full
   error. The *Log* tab shows the log (*Verbose log* adds debug lines).
7. **Cancel** stops after the current batch: the table in progress and the remaining tables get
   `ERROR` with `cancelled by user` (a `sync` table is rolled back, a `full` table is left
   partial). **Save report...** writes the JSON report of the last run, the same document as
   `--report`.

Errors such as a missing driver or a password-protected database are shown in a message box and
the log, with any password masked.

## Check your machine with doctor

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

How to read it:
- `usable readers` says what `--reader auto` will use. With `odbc` in the list, the Access driver
  works.
- `Access driver 32-bit ... installed (cannot be used by this Python)` means you installed the
  driver of the wrong bitness (see [Fix "Access ODBC driver was not found"](#fix-access-odbc-driver-was-not-found)).
- `SQL Server ODBC drivers` matters only for SQL Server targets. The Windows built-in `SQL Server`
  driver shown above is not supported by the writer; install "ODBC Driver 18 for SQL Server".
- `psycopg` matters only for PostgreSQL targets.
- The exit code is 0 when at least one Access reader is usable, else 1.

`python -m acade2sql doctor` does the same, which helps when the `acade2sql` command is not on
`PATH`.

## Convert on a machine without the Access driver

You do not need the Microsoft Access Database Engine. Without it, the default `--reader auto` uses
the pure-Python reader and logs one warning (on stderr) per run:

```text
> acade2sql convert fixture.accdb --tables NETS --target out/nodrv.sqlite
2026-10-19T09:53:03.159 WARNING acade2sql.readers Microsoft Access ODBC driver not usable; falling back to the pure-Python reader (--reader pure). It returns the same data; its speed is comparable on the ACADE sample databases but can differ on other files, and it refuses password-protected/encrypted databases and attachment/calculated fields. To use the reference ODBC reader, install the driver ('Microsoft Access Database Engine 2016 Redistributable', https://www.microsoft.com/en-us/download/details.aspx?id=54920; check with: acade2sql doctor). The Microsoft Access ODBC driver 'Microsoft Access Driver (*.mdb, *.accdb)' was not found for this 64-bit Python. ...
PASS NETS source=2 target=2
1 tables: 1 passed, 0 failed, 0 errors
```

(Recorded with the driver hidden from Python, to simulate such a machine.)

To skip the check and the warning, ask for the pure reader directly:

```text
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb" --reader pure --target C:\out\cat.sqlite --checksum
```

On `default_cat.mdb` this passed all 168 tables, including the checksum of every value. The
`--checksum` re-read uses the same reader, so it confirms the target matches what the pure reader
read; [Compare the two readers](#compare-the-two-readers) shows how it was checked against the
ODBC driver.

Things to know:
- The report records `"reader": "pure"`, so you can tell later which reader produced a target.
- Password-protected and encrypted databases stop with exit 1:
  `error: Cannot read <path>: the database is password-protected, which acade2sql does not support. Remove the password in Access (File > Info > Decrypt Database / Unset Database Password) or convert a copy without one.`
- A table with an attachment, multi-value, calculated or Date/Time Extended field is `ERROR`
  (`read failed: ...`) and the other tables are converted (exit 3). Such tables need the ODBC
  reader. The ACADE sample databases have none.
- The pure reader never returns part of a table. If the pages it finds do not match the row count
  Access stored for the table, that table is `ERROR` (`SourceIntegrityError`) and nothing of it is
  written (a sync of it is rolled back).
- Even without the driver, pyodbc is installed as a dependency; the pure reader never imports it.

## Choose a reader

| You have | Use |
|---|---|
| The Access driver, ordinary ACADE files | `--reader auto` (default), which uses the driver |
| No Access driver (no admin rights, Office bitness conflict) | `--reader auto` or `--reader pure` |
| A file with Access "Large Number" (`BIGINT`) columns | `--reader pure` (the ODBC driver misreads them) |
| A file with attachment, multi-value or calculated fields | `--reader odbc` |
| A database that ACADE may have open | either reader with `--copy-to-temp` |

Check which reader a run used: `"reader"` in the `--report` file or in `inspect --json`:

```text
> acade2sql inspect fixture.mdb --reader pure --json --no-counts --table NETS
{
  "source": "fixture.mdb",
  "reader": "pure",
  "tables": [
...
```

`--reader odbc` on a machine without the driver stops with exit 1 and the installer to use (see
[Fix "Access ODBC driver was not found"](#fix-access-odbc-driver-was-not-found)). If pyodbc itself
cannot be imported, the message is
`error: pyodbc not importable (...); the ODBC reader cannot be used. Install pyodbc ('python -m pip install pyodbc'; ...) or use --reader pure.`

**Large Number columns.** The ODBC driver reports an Access "Large Number" column as `CHAR(4)` and
returns meaningless text. To keep that text out of the target, the ODBC reader checks the file's own
table definition whenever the driver reports a `CHAR(4)` column. It refuses a table whose column is
really a Large Number: the table ends with status `error` (exit 3), no target table is created for
it, and the message says to use `--reader pure`. Genuine 4-character text columns are not affected.
From a scratch database with one Large Number value `1234567890123`:

```text
> acade2sql inspect scratch.accdb --reader odbc --table BIGT
...
    2  N                        CHAR                4  yes
> acade2sql inspect scratch.accdb --reader pure --table BIGT
...
    2  N                        BIGINT             19  yes
```

Converting with `--reader pure` writes `1234567890123`. With `--reader odbc` the `BIGT` table fails
with `column 'N' of table 'BIGT' is an Access Large Number (BIGINT), which the Access ODBC driver
misreads; use --reader pure`.

## Compare the two readers

`scripts/compare_readers.py` (in the repository, not installed as a command) reads every table with
both readers and compares the column names, order and types, the row count and a checksum of all
values. It needs the Access driver, and it writes nothing next to the source when you add
`--copy-to-temp`.

```text
> python scripts/compare_readers.py fixture.mdb fixture.accdb --copy-to-temp --json out/cmp.json
== fixture.mdb  (19 tables)
[1/19] same _PINLIST_BRUNDY/HUBBEL rows=2 odbc=0.0s pure=0.0s
[2/19] same _PINLIST_XX rows=40 odbc=0.0s pure=0.0s
...
== fixture.mdb: 19/19 identical; tables with schema notes: 0; time odbc=0.1s pure=0.1s
== fixture.accdb  (19 tables)
...
== fixture.accdb: 19/19 identical; tables with schema notes: 0; time odbc=0.1s pure=0.1s
```

Results on the sample databases (with checksums):

| Source | Identical | ODBC | Pure |
|---|---|---|---|
| `footprint_lookup.accdb` | 117 / 117 | 3.4 s | 3.7 s |
| `default_cat.mdb` | 168 / 168 | 128.7 s | 81.6 s |

A difference looks like this (the Large Number table from [Choose a reader](#choose-a-reader); a
missing table name is reported the same way):

```text
> python scripts/compare_readers.py scratch.accdb --tables BIGT OLDD NOPE --copy-to-temp
== scratch.accdb  (3 tables)
[1/3] DIFF BIGT rows=1 odbc=0.0s pure=0.0s
    difference: schema: odbc=[('ID', 1, 'COUNTER'), ('N', 2, 'CHAR')] pure=[('ID', 1, 'COUNTER'), ('N', 2, 'BIGINT')]
    difference: checksum differs
[2/3] same OLDD rows=1 odbc=0.0s pure=0.0s
[3/3] DIFF NOPE rows=None
    difference: odbc failed: TableNotFoundError: Table 'NOPE' not found in scratch.accdb
    difference: pure failed: TableNotFoundError: Table 'NOPE' not found in scratch.accdb
== scratch.accdb: 1/3 identical; tables with schema notes: 1; time odbc=0.0s pure=0.0s
```

- Exit code 0: every table identical; 1: at least one difference; 2: bad arguments or a source
  that cannot be opened (the other sources are still compared, and `--json` is still written).
- `-v` also prints schema notes (differences in size, nullable, indexes and similar fields that do
  not affect the data).
- `--tables` takes exact, space-separated names (no comma splitting).

## Convert a catalog to SQLite

```text
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb" --target C:\out\default_cat.sqlite
```

- Every user table is converted, including empty ones (schema only).
- If you leave out `--target`, the output is `default_cat.sqlite` in the **current directory**, not
  next to the source.
- Missing folders in the target path are created.
- Running the command again drops and reloads the converted tables. Other tables already in the
  target file are left alone. You can therefore convert the catalog and the footprint lookup into
  one SQLite file, as long as their table names do not clash.

Expected time on the real catalogs: `default_cat.mdb` (168 tables, 1,594,493 rows) takes about
2 minutes; `footprint_lookup.accdb` (117 tables, 261,858 rows) takes about 10 seconds.

Convert both catalogs into one file.

PowerShell:

```powershell
$env:ACADE2SQL_TARGET = "C:\out\acade.sqlite"
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb"
acade2sql convert "C:\ACADE\Catalogs\footprint_lookup.accdb"
Remove-Item Env:ACADE2SQL_TARGET
```

bash:

```bash
export ACADE2SQL_TARGET="C:/out/acade.sqlite"
acade2sql convert "C:/ACADE/Catalogs/default_cat.mdb"
acade2sql convert "C:/ACADE/Catalogs/footprint_lookup.accdb"
unset ACADE2SQL_TARGET
```

Check that the two databases have no table names in common first. A footprint table with the same
name as a catalog table (compared case-insensitively) would replace the catalog table.

## Watch progress

When you run `convert` in a terminal, a line appears on stderr as each table finishes. The
per-table result lines and the summary are printed to stdout at the end:

```text
> acade2sql convert fixture.accdb --reader pure --target out/pure.sqlite --checksum --report out/pure.json --progress
[1/19] _PINLIST_BRUNDY/HUBBEL 2 rows 0.0s
[2/19] _PINLIST_XX 40 rows 0.0s
...
[5/19] BIG 12345 rows 0.2s
...
[19/19] XX 40 rows 0.0s
target=sqlite:///C:\...\out\pure.sqlite rows=12489 elapsed=0.3s rows_per_s=39807
PASS _PINLIST_BRUNDY/HUBBEL source=2 target=2 checksum=match
...
19 tables: 19 passed, 0 failed, 0 errors
```

- Progress is on by default only when stderr is a terminal. In a script or with stderr redirected
  it is off; `--progress` forces it on, `--no-progress` off.
- It never goes to stdout, so `> result.txt` still captures only the results.
- A dry run prints no progress lines.

## Look before you convert

List the tables, row counts and primary keys:

```text
> acade2sql inspect fixture.accdb
_PINLIST_BRUNDY/HUBBEL     2 cols          2 rows  pk=RECNUM (inferred)
_PINLIST_XX                6 cols         40 rows  pk=RECNUM (inferred)
ALLEN BRADLEY              5 cols          2 rows  pk=RECNUM (inferred)
...
COMP                      11 cols          5 rows  pk=HDL (discovered)
...
PNLCOMP                    6 cols          3 rows  pk=- (none)
TERMS                      4 cols          3 rows  pk=HDL,TERM (discovered)
...
16 tables, 12457 rows
```

The `pk=` column shows where each table's key came from:
- `discovered`: Access has a `PrimaryKey` index on these columns.
- `inferred`: there is no `PrimaryKey` index, but the table has exactly one AutoNumber column (ACADE's
  `RECNUM`), which becomes the key.
- `none`: the table is created without a primary key.

Show columns and indexes for specific tables. Quote names that contain spaces:

```text
> acade2sql inspect fixture.accdb --table _PINLIST_XX --table "ALLEN BRADLEY"
...
Table _PINLIST_XX  rows=40  pk=RECNUM (inferred)
    #  column                   type             size  nullable
    1  RECNUM                   COUNTER            10  no  autoincrement
    2  CATALOG                  VARCHAR            60  yes
    3  ASSEMBLYCODE             VARCHAR            30  yes
    4  MFG                      VARCHAR           255  yes
    5  DESC1                    VARCHAR           255  yes
    6  NOTES                    LONGCHAR   1073741823  yes
  index IDX_CAT (CATALOG, ASSEMBLYCODE) non-unique
...
```

On a large catalog, `--no-counts` skips the row counts and returns faster. `--json` writes the same
information as JSON (see [CLI reference](CLI.md#inspect---json-output)).

## Convert selected tables

`--tables` keeps only the named tables. `--exclude` removes tables. Names are matched exactly but
case-insensitively. If a name appears in both options, `--exclude` wins.

```text
> acade2sql convert fixture.accdb --tables "_pinlist_brundy/hubbel" "allen bradley" --target out/sel.sqlite
PASS _PINLIST_BRUNDY/HUBBEL source=2 target=2
PASS ALLEN BRADLEY source=2 target=2
2 tables: 2 passed, 0 failed, 0 errors
```

In the target, the tables keep their original spelling (`_PINLIST_BRUNDY/HUBBEL`), not the spelling
you typed.

```text
acade2sql convert default_cat.mdb --exclude MS CB --target cat_small.sqlite
```

Names can also be comma-separated or given in repeated options. These three are equivalent:

```text
acade2sql convert fixture.accdb --tables NETS WIRENO COMP --target out/t.sqlite
acade2sql convert fixture.accdb --tables NETS,WIRENO,COMP --target out/t.sqlite
acade2sql convert fixture.accdb --tables NETS,WIRENO --tables COMP --target out/t.sqlite
```

Rules:
- Write the **source path first**. `--tables` and `--exclude` take any number of values, so in
  `acade2sql convert --tables NETS fixture.accdb`, `fixture.accdb` is read as a table name. The
  command then fails with `error: the following arguments are required: source` (exit 2).
- An unknown name in `--tables` stops the run before anything is written:
  `error: Table(s) not found in source: NOPE` (exit 1). Unknown names in `--exclude` are ignored.
- If the filters leave nothing to convert, the tool prints `warning: no tables selected` and exits 0.
- Quoting is the same in PowerShell and bash: wrap names with spaces, `/` or `&` in double quotes
  (`"KRAUS & NAIMER"`). A table name that contains a comma cannot be selected with `--tables`,
  because the comma splits it (`--tables COMMA,NAME` gives
  `error: Table(s) not found in source: COMMA, NAME`). Use a
  [tables file](#convert-the-tables-listed-in-a-file) for such names.
- An empty name (`--tables "NETS,,COMP"`) is a usage error (exit 2).

## Convert the tables listed in a file

For a long or recurring selection, put the names in a UTF-8 text file, one per line. Blank lines and
lines starting with `#` are ignored, and commas are part of the name:

```text
# project tables
COMP
NETS

COMMA,NAME
```

```text
> acade2sql convert fixture.accdb --reader pure --tables-file tables.txt --exclude-file skip.txt --target out/tf.sqlite --no-progress
PASS COMMA,NAME source=2 target=2
PASS COMP source=5 target=5
2 tables: 2 passed, 0 failed, 0 errors
```

Here `skip.txt` contains the single line `NETS`, so `--exclude-file` removed it.

- The file's names are added to any `--tables` (or `--exclude`) names on the command line:
  `--tables-file tables.txt --tables WIRENO` converts four tables.
- Names are matched case-insensitively, and an unknown name in a tables file stops the run (exit 1),
  as with `--tables`. Unknown names in an exclude file are ignored.
- Exit 2: `error: cannot read --tables-file nope.txt: No such file or directory`, a file that is not
  UTF-8, or `error: --tables-file empty.txt lists no table names`.
- Files saved by Notepad with a UTF-8 byte-order mark work. Windows PowerShell 5.1's `>` and
  `Out-File` write UTF-16, which is rejected
  (`error: --tables-file u16.txt is not UTF-8: 'utf-8' codec can't decode byte 0xff in position 0: ...`).
  Write the file with an explicit encoding instead:

  ```powershell
  "COMP", "NETS" | Set-Content -Encoding utf8 tables.txt
  ```

  bash:

  ```bash
  printf 'COMP\nNETS\n' > tables.txt
  ```

## Verify every value with checksums

By default only the row counts are compared. Add `--checksum` to compare every value as well:

```text
> acade2sql convert fixture.accdb --target out/fixture.sqlite --checksum --report out/report.json
PASS _PINLIST_BRUNDY/HUBBEL source=2 target=2 checksum=match
PASS _PINLIST_XX source=40 target=40 checksum=match
...
PASS TYPED source=4 target=4 checksum=match
PASS WFRM2ALL source=2 target=2 checksum=match
PASS WIRENO source=3 target=3 checksum=match
PASS XX source=40 target=40 checksum=match
16 tables: 16 passed, 0 failed, 0 errors
```

`--checksum` reads every table a second time, from both the source and the target. On
`default_cat.mdb` the run takes about 4 min 43 s instead of about 2 min. The checksum does not depend
on row order. It detects NULL vs `''`, trimmed or added spaces, changed Unicode characters, and any
changed number, date, GUID or binary value. It ignores only GUID case and braces in GUID columns,
and differences below single precision in Single (`REAL`) columns. A mismatch shows as
`checksum=MISMATCH`, marks the table `FAIL` and gives exit code 3.

## Sync a project database

Use `--mode sync` to bring an existing SQL copy up to date after the drawings change. Unlike
`--mode full`, sync never drops a table: it updates the rows whose values changed, inserts new rows
and (with `--delete-missing`) deletes rows that are gone. Rows are matched by a key, by default the
table's primary key.

1. Create the copy once:

   ```text
   acade2sql convert MyProject.mdb --target C:\out\myproject.sqlite
   ```

2. After each round of drawing changes, sync it:

   ```text
   acade2sql convert MyProject.mdb --target C:\out\myproject.sqlite --mode sync --no-key replace --checksum --report C:\out\sync.json
   ```

Output from the test database, where the target copy had two changed `COMP` rows, one missing
`WIRENO` row and one extra `NETS` row:

```text
> acade2sql convert fixture.accdb --target out/sync.sqlite --mode sync --no-key replace --checksum --report out/sync1.json
PASS _PINLIST_BRUNDY/HUBBEL source=2 target=2 inserted=0 updated=0 deleted=0 extra=0 checksum=match
...
PASS COMP source=5 target=5 inserted=0 updated=2 deleted=0 extra=0 checksum=match
...
PASS NETS source=2 target=3 inserted=0 updated=0 deleted=0 extra=1 checksum=match
PASS PNLCOMP source=3 target=3 inserted=3 updated=0 deleted=3 extra=0 checksum=match
...
PASS WIRENO source=3 target=3 inserted=1 updated=0 deleted=0 extra=0 checksum=match
PASS XX source=40 target=40 inserted=0 updated=0 deleted=0 extra=0 checksum=match
16 tables: 16 passed, 0 failed, 0 errors
```

How to read it:
- `updated=2`: two rows had different values and were rewritten. Rows with identical values are not
  written (the report counts them as `unchanged`), so a sync with no changes reports
  `inserted=0 updated=0 deleted=0` for every table.
- `inserted=1`: a source row whose key was not in the target.
- `extra=1`: the target has a row whose key is not in the source. It is kept and the table still
  passes. Use `--delete-missing` to remove it (next sections).
- `PNLCOMP` has no primary key. `--no-key replace` reloads such tables completely
  (`deleted=3 inserted=3`). Without it, a keyless table is an error:

  ```text
  ERROR PNLCOMP source=- target=- error=write failed: no key for sync; use --key or --no-key replace
  16 tables: 15 passed, 0 failed, 1 errors
  ```

  (exit 3; the other 15 tables were synced.)
- With `--checksum`, the checksum covers exactly the target rows whose key is in the source, so extra
  rows do not cause a mismatch.

On the real catalog (`default_cat.mdb`, 168 tables, 1.6 million rows), a sync with `--checksum` took
about 5 minutes; a sync without `--checksum` and without changes took 2 min 17 s, about the same as a
full conversion.

Each table is synced in one transaction. If anything goes wrong in a table (duplicate source keys,
schema drift, a database error), that table is rolled back and reported as `ERROR`; the others
continue.

## Choose a sync key

The key decides which source row matches which target row. Check what each table would use with a
dry run (`key=` column):

```text
> acade2sql convert fixture.accdb --target out/sync.sqlite --mode sync --dry-run --no-counts --tables COMP TERMS PNLCOMP
PLANNED COMP source=- target=- key=HDL actions=create-if-missing,upsert
ERROR PNLCOMP source=- target=- key=- error=no key for sync; use --key or --no-key replace
PLANNED TERMS source=- target=- key=HDL,TERM actions=create-if-missing,upsert
3 tables: 0 passed, 0 failed, 1 errors, 2 planned
...
```

(The dry run exits 3 here because `PNLCOMP` has no key. Since 0.3.0 such a table shows no actions
and gets no `CREATE TABLE` in the DDL, because the real run would not create it. With
`--no-key replace` it shows `actions=create-if-missing,replace-rows`.)

Order of precedence:

| Option | Applies to |
|---|---|
| `--key TABLE=COL[,COL...]` | that table only (table name case-insensitive) |
| `--key COL[,COL...]` | every table that has all those columns; other tables use their primary key |
| (none) | the table's primary key, as shown by `acade2sql inspect` (`RECNUM` in catalogs) |
| `--no-key replace` | tables still without a key: delete all their target rows and insert the source rows |

`--key` can be repeated, for example `--key HDL --key "PNLCOMP=HDL,PNLNAME"`.

The key must be unique and non-NULL in the source. Otherwise the table is rolled back:

```text
> acade2sql convert fixture.accdb --target out/sync.sqlite --mode sync --tables PNLCOMP --key "PNLCOMP=HDL,PNLNAME"
ERROR PNLCOMP source=3 target=- error=write failed: duplicate source key ('H0001', 'PNL1') for sync key (HDL, PNLNAME)
1 tables: 0 passed, 0 failed, 1 errors
```

In the test database, `PNLCOMP` contains two identical rows, so no column combination is unique.
For such tables use `--no-key replace`.

A column that does not exist gives
`error=write failed: key column(s) NOPE not found in source table PNLCOMP`. A `--key TABLE=...` for a
table that is not being converted is ignored with a warning (`--key table not converted table=NOPE`).

## Delete rows removed from the source

Without `--delete-missing`, sync never deletes rows; rows removed from the source stay in the target
and are counted as `extra`. To remove them:

```text
> acade2sql convert fixture.accdb --target out/sync.sqlite --mode sync --delete-missing --tables NETS COMP
PASS COMP source=5 target=5 inserted=0 updated=0 deleted=0 extra=0
PASS NETS source=2 target=2 inserted=0 updated=0 deleted=1 extra=0
2 tables: 2 passed, 0 failed, 0 errors
```

- With `--delete-missing` a table passes only when the target has exactly the source rows afterwards.
- Deletion is in the same transaction as the updates and inserts.
- Try it with `--dry-run` first: the actions then include `delete-missing`.
- Keyless tables with `--no-key replace` are always reloaded completely, with or without
  `--delete-missing`.

## Append rows

`--mode append` inserts every source row into the target table, creating the table if it does not
exist. It never drops or updates anything.

```text
> acade2sql convert fixture.accdb --target out/app.sqlite --mode append --tables NETS WIRENO --checksum
PASS NETS source=2 target=2 target_before=0 inserted=2 warnings=1
PASS WIRENO source=3 target=3 target_before=0 inserted=3 warnings=1
2 tables: 2 passed, 0 failed, 0 errors
```

The warning is `checksum not computed in append mode`: append checks only that the target grew by
exactly the number of source rows (`target = target_before + source`).

Appending the same rows again fails for tables with a key, and is rolled back:

```text
> acade2sql convert fixture.accdb --target out/app.sqlite --mode append --tables NETS WIRENO
ERROR NETS source=2 target=- target_before=2 error=write failed: IntegrityError: UNIQUE constraint failed: NETS.RECNUM
ERROR WIRENO source=3 target=- target_before=3 error=write failed: IntegrityError: UNIQUE constraint failed: WIRENO.RECNUM
2 tables: 0 passed, 0 failed, 2 errors
```

A table without a key accepts them and now holds duplicates (`PASS PNLCOMP source=3 target=6
target_before=3 inserted=3`). To refresh existing data, use `--mode sync` instead.

Append counts the target rows before inserting, outside the insert transaction. Do not let another
program write to the same tables during the run; the count check could then fail (the data itself
is not affected).

## Preview with --dry-run

`--dry-run` shows what a run would do, without connecting to the target: the actions per table, the
sync key, warnings, and the exact `CREATE TABLE` / `CREATE INDEX` (and, for `full`, `DROP TABLE IF
EXISTS`) statements for the target's dialect. It works even if psycopg or the SQL Server ODBC
driver is not installed, and it never creates a SQLite file.

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
	[ASSEMBLYCODE] NVARCHAR(30) NULL,
	[MFG] NVARCHAR(255) NULL,
	[DESC1] NVARCHAR(255) NULL,
	[NOTES] NVARCHAR(max) NULL,
	PRIMARY KEY ([RECNUM])
);
CREATE INDEX [ix__PINLIST_XX_IDX_CAT] ON [_PINLIST_XX] ([CATALOG], [ASSEMBLYCODE]);
...
```

- Save the DDL for a database administrator: redirect stdout (`> plan.txt`), or use `--report` (the
  report's `ddl` field holds one statement per entry).
- `--no-counts` skips counting the source rows. A dry run of `default_cat.mdb` for a SQL Server or
  PostgreSQL URL took about 0.7 s and printed 168 `CREATE TABLE` and 165 `CREATE INDEX` statements,
  with no SQL Server index skipped.
- The password is masked in all output, however short it is.
- `--checksum` has no effect in a dry run; it prints `warning: --checksum is ignored with --dry-run`.
- Exit code 0 means every table is `PLANNED`. A table the real run would fail on (no sync key,
  unknown key column, SQL Server primary key too large, unmapped Access type) is `ERROR` or `FAIL`,
  and the exit code is 3.
- In `append` and `sync` modes the DDL lists `CREATE TABLE` for every table, because the dry run
  does not look at the target. The real run creates only the missing tables.

## Connect to SQL Server

> SQL Server support has been verified by compiling its SQL and by dry runs only, not yet against a
> live server. Check the first runs with `--checksum`.

Requirements: the Microsoft **ODBC Driver 18 for SQL Server** (or 17) with the same bitness as Python,
and an existing database. Check the driver:

```text
python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'SQL Server' in d])"
```

URL forms (replace the `<...>` placeholders):

| Login | URL |
|---|---|
| SQL login | `mssql+pyodbc://<user>:<password>@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server` |
| Named instance | `mssql+pyodbc://<user>:<password>@<host>\<instance>/<database>?driver=ODBC+Driver+18+for+SQL+Server` |
| Windows login | `mssql+pyodbc://@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server&trusted_connection=yes` |
| Self-signed server certificate (driver 18 encrypts by default) | add `&TrustServerCertificate=yes` |
| Raw ODBC connection string | `mssql+pyodbc:///?odbc_connect=<URL-encoded string>` |

Spaces in the driver name are written as `+`. Special characters in the password (`@`, `:`, `/`,
`%`) must be URL-encoded (`@` is `%40`).

`odbc_connect` passes a complete ODBC connection string. URL-encode it, for example with Python:

```text
python -c "import urllib.parse; print(urllib.parse.quote_plus('DRIVER={ODBC Driver 18 for SQL Server};SERVER=<host>;DATABASE=<database>;UID=<user>;PWD=<password>;Encrypt=yes'))"
```

Then use `mssql+pyodbc:///?odbc_connect=<output>`. The `PWD=` value is masked in all output:

```text
dry run: mode=full target=mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+18+for+SQL+Server%7D%3BSERVER%3Dsqlhost%3BDATABASE%3Dacade%3BUID%3Dacade%3BPWD%3D***%3BEncrypt%3Dyes schema=- (nothing was written)
```

Keep the password out of your shell history by setting the URL in the environment.

PowerShell:

```powershell
$env:ACADE2SQL_TARGET = "mssql+pyodbc://<user>:<password>@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server"
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb" --dry-run --no-counts > plan.txt
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb" --checksum --report cat_report.json
Remove-Item Env:ACADE2SQL_TARGET
```

bash:

```bash
export ACADE2SQL_TARGET='mssql+pyodbc://<user>:<password>@<host>/<database>?driver=ODBC+Driver+18+for+SQL+Server'
acade2sql convert "C:/ACADE/Catalogs/default_cat.mdb" --dry-run --no-counts > plan.txt
acade2sql convert "C:/ACADE/Catalogs/default_cat.mdb" --checksum --report cat_report.json
unset ACADE2SQL_TARGET
```

Things to know:
- Text columns become `NVARCHAR(n)` (`NVARCHAR(max)` for MEMO and for text longer than 4,000
  characters), dates `DATETIME2`, GUIDs `UNIQUEIDENTIFIER`.
- An index containing an `NVARCHAR(max)` column, or whose key is larger than 1,700 bytes
  (`NVARCHAR(n)` counts 2n bytes), is skipped with the warning
  `index <name> skipped on SQL Server: ...`; the table still passes. A primary key over 900 bytes or
  on an `NVARCHAR(max)` column makes the table `ERROR`. On `default_cat.mdb` no index is affected.
- SQL Server's default collation is case-insensitive. Values are stored exactly, but unique indexes
  and sync key lookups ignore case and trailing spaces (see
  [Fix "keys that differ only by case/trailing spaces"](#fix-keys-that-differ-only-by-casetrailing-spaces)).
- The connection times out after 15 seconds (`error: Cannot connect to target ...`, exit 1). To
  allow longer, put your own timeout in the URL, for example `Connection Timeout=60` inside
  `odbc_connect`.
- If the ODBC driver is missing, the error ends with
  `SQL Server needs pyodbc (pip install acade2sql[mssql]) and the Microsoft "ODBC Driver 18 for SQL Server" (or 17) installed for this Python's bitness; ...`.

## Connect to PostgreSQL

> PostgreSQL support has been verified by compiling its SQL and by dry runs only, not yet against a
> live server. Check the first runs with `--checksum`.

Install the driver extra once: `pip install -e ".[postgresql]"` (installs `psycopg[binary]`).
Without it, a real run stops with `... install the PostgreSQL driver: pip install acade2sql[postgresql]`
(exit 1); `--dry-run` works without it.

URL: `postgresql+psycopg://<user>:<password>@<host>:5432/<database>`. Add `?sslmode=require` if your
server requires TLS.

PowerShell:

```powershell
$env:ACADE2SQL_TARGET = "postgresql+psycopg://<user>:<password>@<host>:5432/<database>"
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb" --checksum
Remove-Item Env:ACADE2SQL_TARGET
```

bash:

```bash
export ACADE2SQL_TARGET='postgresql+psycopg://<user>:<password>@<host>:5432/<database>'
acade2sql convert "C:/ACADE/Catalogs/default_cat.mdb" --checksum
unset ACADE2SQL_TARGET
```

Things to know:
- Names are kept exactly and quoted, so in SQL you must write them in double quotes with the original
  case: `SELECT * FROM "COMP"` (an unquoted `comp` would not match).
- PostgreSQL truncates names longer than 63 bytes. acade2sql stops before writing anything if a
  table, column or schema name is longer (exit 1, `identifier(s) longer than 63 bytes are not
  supported by PostgreSQL ...`), naming them. Exclude those tables with `--exclude`.
- PostgreSQL text cannot contain the NUL character. A table with such a value is `ERROR`
  (`write failed: NUL character in text column <C> (not supported by PostgreSQL)`) and rolled back.
- The connection times out after 15 seconds; `?connect_timeout=60` in the URL replaces that.

## Write into a schema

On SQL Server and PostgreSQL, `--schema NAME` puts every table and index into an existing schema:

```text
> acade2sql convert fixture.accdb --target "postgresql+psycopg://acade:<password>@pghost/acade" --schema acade --mode sync --delete-missing --dry-run --no-counts --tables COMP TYPED
PLANNED COMP source=- target=- key=HDL actions=create-if-missing,upsert,delete-missing
PLANNED TYPED source=- target=- key=ID actions=create-if-missing,upsert,delete-missing
...
dry run: mode=sync target=postgresql+psycopg://acade:***@pghost/acade schema=acade (nothing was written)
-- DDL
CREATE TABLE acade."COMP" (
	"HDL" VARCHAR(20) NOT NULL,
	...
```

- The schema must exist; acade2sql does not create it. A missing schema makes each table `ERROR`
  with the database's message. Create it first (`CREATE SCHEMA acade;`).
- Give one plain name. Names with `.`, `[`, `]`, `"`, a backtick or control characters are rejected
  (exit 2), because acade2sql quotes the name itself.
- `--schema` with a SQLite target is a usage error (exit 2): SQLite has no schemas.

## Script a conversion with exit codes and the JSON report

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | every table passed | done |
| 1 | stopped before writing (source, driver, table name, target or connection problem) | read the `error:` line |
| 2 | bad command line or option combination (for example `--key` without `--mode sync`) | check the options |
| 3 | at least one table failed or had an error; the other tables were converted | open the report |

PowerShell:

```powershell
acade2sql convert "C:\ACADE\Catalogs\default_cat.mdb" --target C:\out\cat.sqlite `
    --checksum --report C:\out\cat_report.json
switch ($LASTEXITCODE) {
    0 { "OK" }
    3 {
        $r = Get-Content C:\out\cat_report.json -Raw | ConvertFrom-Json
        $r.tables | Where-Object { $_.status -ne "pass" } |
            Format-Table name, status, source_rows, target_rows, checksum_match, error
    }
    default { "acade2sql stopped with exit code $LASTEXITCODE" }
}
```

bash:

```bash
acade2sql convert "C:/ACADE/Catalogs/default_cat.mdb" --target C:/out/cat.sqlite \
    --checksum --report C:/out/cat_report.json
rc=$?
if [ $rc -eq 3 ]; then
  python -c "import json,sys; r=json.load(open(sys.argv[1], encoding='utf-8')); \
[print(t['status'], t['name'], t['error'] or '') for t in r['tables'] if t['status'] != 'pass']" \
  C:/out/cat_report.json
elif [ $rc -ne 0 ]; then
  echo "acade2sql stopped with exit code $rc"
fi
```

Notes:
- The report is written whenever the run gets as far as converting tables, so it exists for exit
  codes 0 and 3. It is not written for exit code 1 (the run stopped before writing).
- The report's `ok` field is `true` only when every table has `status: "pass"` (or `"planned"` in a
  dry run).
- For a sync, the report also has `inserted`, `updated`, `deleted`, `unchanged` and `extra_rows` per
  table. To list the tables a sync changed (PowerShell):
  `$r.tables | Where-Object { $_.inserted -or $_.updated -or $_.deleted } | Format-Table name, inserted, updated, deleted, extra_rows`.
- The per-table lines go to stdout. The statistics line (`target=... rows=... elapsed=...`), warnings
  and `--verbose` logs go to stderr. Redirect stdout to keep a clean summary.
- The report format is described in the [CLI reference](CLI.md#json-report).

## Use the Python API

```python
from acade2sql import convert

report = convert(
    "fixture.accdb",
    "out/api.sqlite",               # path or SQLAlchemy URL; None = $ACADE2SQL_TARGET or ./<stem>.sqlite
    tables=["NETS", "WIRENO"],      # optional; exclude=[...] also available
    checksum=True,
    report_path="out/api.json",     # optional
)
print(report.ok)
print(report.render_text())
for t in report.tables:
    print(t.name, t.status, t.source_rows, t.target_rows, t.checksum_match)
```

Output:

```text
True
PASS NETS source=2 target=2 checksum=match
PASS WIRENO source=3 target=3 checksum=match
2 tables: 2 passed, 0 failed, 0 errors
NETS pass 2 2 True
WIRENO pass 3 3 True
```

- `convert()` takes the same options as the CLI: `mode="full"` (or `"append"`, `"sync"`), `tables`,
  `exclude`, `batch_size=5000`, `checksum=False`, `copy_to_temp=False`, `report_path=None`,
  `key=None` (a string or a list of `--key` specs), `no_key="error"`, `delete_missing=False`,
  `schema=None`, `dry_run=False`, `counts=True`, `reader="auto"` (or `"odbc"`, `"pure"`),
  `progress=None`, `table_started=None` and `cancel=None`. `report.reader` is the reader that was
  used.
- `table_started(name, index, total)` is called before each table of a real run. `cancel()` is
  polled before each table and after each source batch; once it returns `True` the table in
  progress and all later tables get `status="error"`, `error="cancelled by user"`, and `convert()`
  returns the report as usual (the desktop window's Cancel button uses this). Neither is used by
  a dry run.
- `progress` is a function called after each table of a real run with `(result, index, total)`:

  ```python
  def show(res, i, n):
      print(f"{i}/{n} {res.name} {res.status}")

  report = convert("fixture.accdb", "out/api.sqlite", tables=["NETS", "WIRENO"],
                   reader="pure", progress=show)
  print(report.ok, report.reader)
  ```

  ```text
  1/2 NETS pass
  2/2 WIRENO pass
  True pure
  ```
- Invalid option combinations raise `acade2sql.UsageError` (a `ValueError`) before anything is
  opened.
- Problems found before anything is written raise an exception derived from
  `acade2sql.Acade2SqlError`: `SourceNotFoundError`, `DriverNotFoundError` (and its subclass
  `BitnessMismatchError`), `TableNotFoundError`, `TargetError`, or `SchemaError` (PostgreSQL name
  over 63 bytes). Per-table problems do not raise. They appear as `status == "fail"` or `"error"`
  in `report.tables`.
- Since 0.3.0, `UsageError`, `SchemaError`, `SyncKeyError` and `TargetDataError` can be imported
  directly from `acade2sql` (they remain in `acade2sql.errors`). `SourceIntegrityError` and
  `UnsupportedAccessFeature` are exported from `acade2sql` as well (both live in
  `acade2sql.errors`). All of them derive from `Acade2SqlError`.
- Each `TableResult` also has `inserted`, `updated`, `deleted`, `unchanged`, `extra_rows`,
  `target_rows_before`, `key` and `actions` (see the [report fields](CLI.md#json-report)).

A sync from Python:

```python
from acade2sql import convert

report = convert("fixture.accdb", "out/sync.sqlite", mode="sync", tables=["COMP", "NETS"],
                 delete_missing=True, checksum=True)
print(report.ok)
for t in report.tables:
    print(t.name, t.status, t.inserted, t.updated, t.deleted, t.unchanged, t.extra_rows, t.key)
```

```text
True
COMP pass 0 0 0 5 0 ['HDL']
NETS pass 0 0 0 2 0 ['RECNUM']
```
- `report.to_dict()` returns the same structure as the JSON report.
- The package does not configure logging. Its loggers are under `acade2sql`.

Read an Access file directly:

```python
from acade2sql import open_reader

with open_reader("fixture.accdb") as r:          # read-only; copy_to_temp=True and reader="pure" also accepted
    print(r.kind)                                # 'odbc' or 'pure'
    print(r.list_tables()[:3])                   # ['_PINLIST_BRUNDY/HUBBEL', '_PINLIST_XX', 'ALLEN BRADLEY']
    print(r.count_rows("big"))                   # 12345 (names are case-insensitive)
    info = r.get_table("NETS")                   # columns, primary_key, primary_key_source, indexes
    for batch in r.iter_rows("NETS", batch_size=1000):
        ...                                      # lists of tuples in column order
```

## Fix "Access ODBC driver was not found"

```text
error: The Microsoft Access ODBC driver 'Microsoft Access Driver (*.mdb, *.accdb)' was not found for
this 64-bit Python. Install the 64-bit (x64) 'Microsoft Access Database Engine 2016 Redistributable'
(accessdatabaseengine_X64.exe) from https://www.microsoft.com/en-us/download/details.aspx?id=54920 ...
```

Since 0.3.0 this is an error only with `--reader odbc`. With the default `--reader auto` the same
text appears at the end of a WARNING, and the run continues with the pure reader (see
[Convert on a machine without the Access driver](#convert-on-a-machine-without-the-access-driver)).
If you cannot install the driver, use `--reader pure` and stop here. To install it:

1. Check Python's bitness: `python -c "import struct; print(struct.calcsize('P') * 8)"`.
2. Install the Access Database Engine 2016 Redistributable **of that bitness**:
   `accessdatabaseengine_X64.exe` for 64-bit, `accessdatabaseengine.exe` for 32-bit.
3. If a different-bitness Office blocks the installer, run it from an administrator prompt with
   `/quiet` (for example `accessdatabaseengine_X64.exe /quiet`), or install a Python of the other
   bitness.
4. Check that the driver is visible to Python: `acade2sql doctor` should show
   `ok odbc reader` and `usable readers: odbc, pure`. Or directly:

   ```text
   python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'Access' in d])"
   ['Microsoft Access Driver (*.mdb, *.accdb)']
   ```

If the message says *"installed only for 32-bit applications, but this Python is 64-bit (32/64-bit
mismatch)"*, a driver exists but has the wrong bitness. Follow steps 2 and 3.

The older `Microsoft Access Driver (*.mdb)` (Jet only) is not enough: it cannot open `.accdb` files.

## Avoid the lock file with --copy-to-temp

This section is about the ODBC reader. The pure reader (`--reader pure`) never creates a lock file.

Even in read-only mode, the Access driver creates `<name>.ldb` (for `.mdb`) or `<name>.laccdb` (for
`.accdb`) next to the source while it is connected. The file is deleted when acade2sql closes the
database. If you watch the folder during a run, you see:

```text
fixture.accdb  fixture.laccdb     <- during the run
fixture.accdb                     <- after the run
```

Use `--copy-to-temp` when nothing at all may appear next to the source. Examples: a read-only
network share, a folder synchronised to other users, or a database that AutoCAD Electrical has open.

```text
acade2sql convert "\\server\acade\default_cat.mdb" --copy-to-temp --target C:\out\cat.sqlite
```

The source is first copied to a folder named `acade2sql_*` in your temp directory and read from
there. The copy is deleted at the end. You need free temp space equal to the size of the source
(about 645 MB for `default_cat.mdb`).

If another program has locked the database, the run stops with
`error: The database is locked by another program (close Access/AutoCAD Electrical, or use copy_to_temp): ...`.
Close the program, or try again with `--copy-to-temp`.

## Fix "the source database appears to be open in another program"

```text
WARNING acade2sql.readers.access_parser_reader the source database appears to be open in another program (lock file C:\...\fixture.laccdb exists): the pure reader reads the file as it is on disk, so a write in progress can make tables inconsistent. Close AutoCAD Electrical/Access first, or use --copy-to-temp (copy_to_temp=True).
```

The pure reader found `<name>.ldb` / `<name>.laccdb` next to the source, so another program
(usually AutoCAD Electrical or Access) has the database open and may be writing to it. The run
continues. What to do:

1. Close the drawing project in AutoCAD Electrical (or close Access) and run again, or
2. add `--copy-to-temp`. The source is copied first and the copy is read; the warning is not shown:

   ```text
   > acade2sql convert fixture.accdb --reader pure --copy-to-temp --tables NETS --target out/lk.sqlite
   PASS NETS source=2 target=2
   1 tables: 1 passed, 0 failed, 0 errors
   ```

If a write was in progress, the pure reader may find that a table's pages do not match its stored
row count. That table then fails as `ERROR` (`SourceIntegrityError`) instead of being written
partially; re-run it after closing the program. A lock file left behind by a crashed program
causes the same warning; delete it only when you are sure no program has the database open.

## Query the result in SQLite

ACADE table names contain spaces, `/`, `-` and `&`, and acade2sql keeps them exactly. In SQL, wrap
such names in **double quotes**:

```sql
SELECT COUNT(*) FROM "_PINLIST_BRUNDY/HUBBEL";
SELECT "DESC", "DUCT SIZE W x H" FROM "ALLEN BRADLEY";
SELECT "CATALOG", "MANUFACTURER" FROM "PNEU-CYL" LIMIT 5;
```

Without quotes, the name is a syntax error:

```text
SELECT * FROM _pinlist_brundy/hubbel   ->  near "/": syntax error
```

- SQLite compares table and column names case-insensitively for ASCII letters, so
  `"_pinlist_brundy/hubbel"` also works. Text **values** are compared case-sensitively by default,
  unlike Access. Use `COLLATE NOCASE` or `UPPER()` to match the Access behaviour.
- Column names that are SQL keywords, such as `DESC` and `RANGE`, also need quotes.
- Values are stored verbatim: padded text keeps its spaces, and missing values are `NULL`, not
  `''`.
- Storage of the other types: dates are text `YYYY-MM-DD HH:MM:SS.ffffff`, Currency/Decimal values
  are exact text such as `'1234.5678'`, Yes/No is `1`/`0`, and GUIDs are upper-case text without
  braces.

From Python, with no extra tools:

PowerShell:

```powershell
@'
import sqlite3
c = sqlite3.connect(r"C:\out\cat.sqlite")
print(c.execute('SELECT COUNT(*) FROM "_PINLIST_BRUNDY/HUBBEL"').fetchone())
'@ | python -
```

(Windows PowerShell 5.1 drops embedded `"` from arguments to `python -c`, so the script is piped in
instead.)

bash:

```bash
python -c 'import sqlite3; c = sqlite3.connect("C:/out/cat.sqlite"); print(c.execute("SELECT COUNT(*) FROM \"_PINLIST_BRUNDY/HUBBEL\"").fetchone())'
```

With the `sqlite3` command-line shell (if installed), or a GUI such as DB Browser for SQLite, use
the same double-quoted names.

## Troubleshoot a failed table

A table with status `ERROR` or `FAIL` does not stop the run. The other tables are still converted,
and the exit code is 3. Example: the target already contains a **view** called `NETS`:

```text
> acade2sql convert fixture.accdb --tables NETS WIRENO --target out/v.sqlite --report out/v.json
2026-10-19T05:30:03.140 ERROR acade2sql.converter table failed table=NETS error="write failed: OperationalError: use DROP VIEW to delete view NETS"
ERROR NETS source=2 target=- error=write failed: OperationalError: use DROP VIEW to delete view NETS
PASS WIRENO source=3 target=3
2 tables: 1 passed, 0 failed, 1 errors
```

- `read failed: ...` means the Access side raised an error. `write failed: ...` means the target side
  raised an error (including `CREATE` / `DROP`).
- A failed table's row load is rolled back. In `full` mode, depending on the step that failed, the
  table is then empty, missing, or (if the drop failed, as above) unchanged from before the run. In
  `append` and `sync` the existing table is unchanged; a table the run had just created stays,
  empty.
- `FAIL` means the copy completed but the validation did not match: different counts,
  `checksum=MISMATCH`, or an unmapped Access type (`warnings=N`).
- `read failed:` with the pure reader can also mean a field type it refuses (attachment,
  multi-value, calculated, Date/Time Extended; re-run that table with `--reader odbc`) or
  `SourceIntegrityError` (the file changed while being read, or is damaged; see
  [Fix "the source database appears to be open in another program"](#fix-the-source-database-appears-to-be-open-in-another-program)).
- `warnings=1` on a `PASS` line with the log line
  `dates before 1753 are read using the ODBC driver's calendar and may differ from what Access displays table=OLDD`
  means the table has dates before 1753-01-01. Both readers return them as the ODBC driver does,
  which can be some days off from what Access shows (`#1700-06-15#` became `1700-06-02` in a test).
  Check such values by hand if they matter.
- Re-run with `-v` / `--verbose` to get timestamped debug logs and, for exit-1 errors, the traceback.

## Fix "keys that differ only by case/trailing spaces"

```text
ERROR COMP source=5 target=- error=write failed: target key 'h0001' matches source key 'H0001' under the target's collation; keys that differ only by case/trailing spaces can't be synced; re-run with --mode full
```

Access and acade2sql treat `h0001` and `H0001` (or `H0001` and `H0001 `) as different keys, but the
target's collation treats them as equal: SQL Server with its default case-insensitive collation, or
a SQLite column declared `COLLATE NOCASE` / `RTRIM`. Updating by such a key could overwrite the
wrong row, so sync stops that table, rolls it back and leaves the target table unchanged. The other
tables are still synced (exit 3).

What to do:
1. Re-create the table with a full conversion: `acade2sql convert MyProject.mdb --target ... --mode
   full --tables COMP`. Full mode does not look up keys, so case-only differences are copied as
   they are (on SQL Server a unique index can still reject them; see the
   [README](../README.md#limitations-and-known-behaviour)).
2. If the conflict came from an old target row, the full run has replaced it and later syncs work
   again. If the source itself contains keys that differ only by case or trailing spaces, sync that
   table cannot work on this target: exclude it from syncs (`--exclude COMP`) and reload it with
   `--mode full`.
3. On SQL Server, a database or column with a case-sensitive collation (`..._CS_AS`) avoids the
   problem.

## Fix "schema drift"

```text
ERROR NETS source=2 target=- error=write failed: schema drift: target table NETS lacks source column(s) SHEET (re-run with --mode full)
```

`append` and `sync` write into the existing target table and never change its structure. The table
is `error` (and unchanged) when:
- a source column is missing from the target table (for example a column added in a newer ACADE
  version, or removed from the target by hand), or
- the target table has a `NOT NULL` column without a default that the source does not have.

Extra nullable columns in the target are fine; they stay `NULL` on inserted rows. A column that
exists on both sides with a different type is not detected as drift.

Fix: re-create the table once with `--mode full --tables <table>`, then sync again:

```text
> acade2sql convert fixture.accdb --target out/drift.sqlite --tables NETS
PASS NETS source=2 target=2
1 tables: 1 passed, 0 failed, 0 errors
```

`--mode full` drops and recreates the table. Views or permissions on it in a server database may
need to be re-created afterwards.
