# acade2sql usage guide

Task-oriented examples for acade2sql 0.1.0. For every option and default, see the
[CLI reference](CLI.md). For installation, see the [README](../README.md#installation).

All examples were run against the synthetic test database built by
`tests/fixtures/build_access.py`. Output is pasted from those runs and trimmed where marked `...`.

- [Convert a catalog to SQLite](#convert-a-catalog-to-sqlite)
- [Look before you convert](#look-before-you-convert)
- [Convert selected tables](#convert-selected-tables)
- [Verify every value with checksums](#verify-every-value-with-checksums)
- [Script a conversion with exit codes and the JSON report](#script-a-conversion-with-exit-codes-and-the-json-report)
- [Use the Python API](#use-the-python-api)
- [Fix "Access ODBC driver was not found"](#fix-access-odbc-driver-was-not-found)
- [Avoid the lock file with --copy-to-temp](#avoid-the-lock-file-with---copy-to-temp)
- [Query the result in SQLite](#query-the-result-in-sqlite)
- [Troubleshoot a failed table](#troubleshoot-a-failed-table)

## Your source files are safe

acade2sql opens every Access file **read-only** and never writes to it. While it reads, the Access
driver creates a temporary lock file next to the source (see
[Avoid the lock file](#avoid-the-lock-file-with---copy-to-temp)). That lock file is the only thing
created next to the source, and it is removed when the run ends.

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

Rules:
- Write the **source path first**. `--tables` and `--exclude` take any number of values, so in
  `acade2sql convert --tables NETS fixture.accdb`, `fixture.accdb` is read as a table name. The
  command then fails with `error: the following arguments are required: source` (exit 2).
- An unknown name in `--tables` stops the run before anything is written:
  `error: Table(s) not found in source: NOPE` (exit 1). Unknown names in `--exclude` are ignored.
- If the filters leave nothing to convert, the tool prints `warning: no tables selected` and exits 0.
- Quoting is the same in PowerShell and bash: wrap names with spaces, `/` or `&` in double quotes
  (`"KRAUS & NAIMER"`).

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
changed number, date, GUID or binary value. A mismatch shows as `checksum=MISMATCH`, marks the
table `FAIL` and gives exit code 3.

## Script a conversion with exit codes and the JSON report

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | every table passed | done |
| 1 | stopped before writing (source, driver, table name or target problem) | read the `error:` line |
| 2 | bad command line | check the options |
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
- The report's `ok` field is `true` only when every table has `status: "pass"`.
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

- `convert()` takes the same options as the CLI: `mode="full"`, `tables`, `exclude`,
  `batch_size=5000`, `checksum=False`, `copy_to_temp=False`, `report_path=None`.
- Problems found before anything is written raise an exception derived from
  `acade2sql.Acade2SqlError`: `SourceNotFoundError`, `DriverNotFoundError` (and its subclass
  `BitnessMismatchError`), `TableNotFoundError` or `TargetError`. Per-table problems do not raise.
  They appear as `status == "fail"` or `"error"` in `report.tables`.
- `report.to_dict()` returns the same structure as the JSON report.
- The package does not configure logging. Its loggers are under `acade2sql`.

Read an Access file directly:

```python
from acade2sql import open_reader

with open_reader("fixture.accdb") as r:          # read-only; copy_to_temp=True is also accepted
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

1. Check Python's bitness: `python -c "import struct; print(struct.calcsize('P') * 8)"`.
2. Install the Access Database Engine 2016 Redistributable **of that bitness**:
   `accessdatabaseengine_X64.exe` for 64-bit, `accessdatabaseengine.exe` for 32-bit.
3. If a different-bitness Office blocks the installer, run it from an administrator prompt with
   `/quiet` (for example `accessdatabaseengine_X64.exe /quiet`), or install a Python of the other
   bitness.
4. Check that the driver is visible to Python:

   ```text
   python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'Access' in d])"
   ['Microsoft Access Driver (*.mdb, *.accdb)']
   ```

If the message says *"installed only for 32-bit applications, but this Python is 64-bit (32/64-bit
mismatch)"*, a driver exists but has the wrong bitness. Follow steps 2 and 3.

The older `Microsoft Access Driver (*.mdb)` (Jet only) is not enough: it cannot open `.accdb` files.

## Avoid the lock file with --copy-to-temp

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
- A failed table's row load is rolled back. Depending on the step that failed, the table is then
  empty, missing, or (if the drop failed, as above) unchanged from before the run.
- `FAIL` means the copy completed but the validation did not match: different counts,
  `checksum=MISMATCH`, or `warnings=N` for an unmapped Access type.
- Re-run with `-v` / `--verbose` to get timestamped debug logs and, for exit-1 errors, the traceback.
