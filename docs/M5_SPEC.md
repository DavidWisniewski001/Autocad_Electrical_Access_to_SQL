# M5 spec: pure-Python fallback reader, reader selection, CLI polish, backlog

Contract between `python-converter-dev` (owns `src/`, `pyproject.toml`) and `test-engineer` (owns
`tests/`). Builds on M1–M4 (DECISIONS #1–#36). Target version **0.3.0**.

## 0. Feasibility findings (lead, 2026-10-19, access-parser 0.0.6)

* `AccessParser(path).catalog` lists the same tables (168 / 117). `parse_table(name)` returns
  `{column: [values]}` (whole table in memory).
* `footprint_lookup.accdb`: AB (126,623 rows), ALLEN BRADLEY, SIEMENS are value-identical to pyodbc.
* `default_cat.mdb`: `MS` has **260 rows that differ**: U+2013 `–` comes back as `'\x13'`.
  Cause: `access_parser/utils.py:get_decoded_text` decodes Jet 4 *compressed unicode* (`FF FE` prefix)
  with a UTF-8 / Latin-1 guess, ignoring the `0x00` mode toggles, and falls back to
  `errors='ignore'` (silent data loss).
* Speed: MS 84 s (ODBC about 18 s); PNEU-CYL 37 s. Acceptable for a fallback.

## 1. `src/acade2sql/readers/access_parser_reader.py`

`class AccessParserReader(Reader)`, `__init__(path, *, copy_to_temp=False)` with the **same interface and
value contract** as `PyodbcReader` (M2 spec): `list_tables`, `get_table`, `count_rows`, `iter_rows`,
`close`, context manager, `TableNotFoundError`, `SourceNotFoundError`.

* **Text decoding is ours, never access-parser's guess.** Implement `decode_jet_text(raw: bytes,
  version: int) -> str` and use it for TEXT and MEMO values:
  - Jet 4 / ACE: if `raw` starts with `b"\xff\xfe"`, read the rest as *compressed* runs: in compressed
    mode each byte is one character (`chr(b)`), and a `0x00` byte switches to uncompressed mode, where
    characters are UTF-16LE pairs until a `0x00 0x00` pair switches back. Without the prefix,
    decode the whole value as UTF-16LE.
  - Jet 3 (Access 97, version 3): decode with the database code page (cp1252 default).
  - **Never** use `errors="ignore"` or `"replace"`. Undecodable data raises `Acade2SqlError` naming the
    table/column (it becomes a table `error` in convert).
  Hook it in by post-processing the raw bytes (access-parser exposes the raw buffer to `parse_type`; if
  needed, wrap or monkeypatch `access_parser.utils.parse_type` *inside our module only*, restoring
  nothing global outside it — prefer a subclass/wrapper over patching). Document the approach.
* **Types and values** must match `PyodbcReader` exactly, as observed on the synthetic fixture:
  `access_type` uses the same ODBC names (VARCHAR, LONGCHAR, COUNTER, INTEGER, SMALLINT, BYTE, REAL, DOUBLE,
  CURRENCY, DECIMAL, DATETIME, BIT, GUID, LONGBINARY, BIGINT); values: str, int, float, `Decimal`
  (CURRENCY with 4 decimals, DECIMAL with its scale), `datetime` (Access days since 1899-12-30, incl.
  dates before 1899 and time-only values), `bool` (BIT never None, as pyodbc), GUID as the unbraced
  upper-case string, `bytes`. `size`, `decimal_digits` and `nullable` as close to pyodbc as the file
  allows; list any field that can't match.
* **Keys/indexes**: if access-parser exposes the table's index definitions, use them (PK `discovered`);
  otherwise `indexes=()` and the PK rule falls through to `inferred` (single COUNTER) / `none`. State which.
* **Streaming**: access-parser loads a whole table; `iter_rows` yields batches from it, and the parsed
  table is released after the last batch (only one table in memory at a time). `count_rows` must not
  keep the parsed data around.
* Opens the file in binary read-only mode (no lock file, nothing written). `copy_to_temp` behaves as
  in PyodbcReader.
* Unsupported features (attachment / multi-value / calculated fields, encrypted or password-protected
  databases) → `Acade2SqlError` with a clear message, never wrong data.

## 2. Reader selection

* `open_reader(path, *, copy_to_temp=False, reader: Literal["auto", "odbc", "pure"] = "auto")`.
  `auto` → `odbc` when `diagnose_driver().ok`, else `pure` with one WARNING log line that explains the
  speed difference and how to install the driver. `odbc` keeps today's errors. `pure` never touches
  pyodbc.
* CLI: `--reader {auto,odbc,pure}` on `inspect` and `convert` (default `auto`). API: `convert(...,
  reader="auto")`. The report and `inspect --json` record `"reader": "odbc" | "pure"`.
* `access-parser` becomes a **core dependency** (pin `>=0.0.6,<0.1`); remove the `fallback` extra (keep
  it as an empty alias for compatibility). Record its license in DECISIONS (look it up in the package
  metadata or source distribution; if unknown, say so).

## 3. CLI polish

* `acade2sql doctor`: prints Python version and bitness, the Access ODBC drivers found (and other-bitness
  detection), which readers are usable, installed target drivers (psycopg, pyodbc, the SQL Server ODBC
  drivers), and the package version. Exit 0 if at least one reader is usable, else 1.
* Progress: when stderr is a TTY (or `--progress` is given), print one line per finished table to stderr
  (`[12/168] MS 269772 rows 18.2s`). `--no-progress` turns it off. It never goes to stdout.

## 4. Backlog from 0.2.0 (PLAN.md "M5 backlog")

1. `redact()`: mask structured positions (URL userinfo, `PWD=`/`Password=` values, sensitive query
   values) always; mask free-text occurrences of a secret only when it is ≥ 4 characters. Short secrets
   still never appear in a URL or PWD position.
2. Dry-run actions for keyless tables under `--no-key error` must not list `replace-rows`.
3. `--tables-file PATH` / `--exclude-file PATH`: one exact table name per line (no comma splitting, `#`
   comments and blank lines ignored, UTF-8). They combine with `--tables`/`--exclude`.
4. Export `UsageError`, `SchemaError`, `SyncKeyError`, `TargetDataError` from `acade2sql`.
5. `--checksum` with `--dry-run` → a stderr warning "ignored with --dry-run" (exit unchanged).

## 5. Acceptance (re-verified by the lead)

1. ruff clean; full pytest green (live tests may skip).
2. **Reader equivalence on real data**: for every table of both sample databases, `--reader pure`
   and `--reader odbc` produce the same schema (names, order, access_type) and the same
   `table_checksum` (column-aware) — 168/168 and 117/117. This includes MS (the U+2013 rows).
3. `acade2sql convert data_files/default_cat.mdb --reader pure --checksum` → 168/168 pass (the checksum
   compares the pure reader against the SQLite target; equivalence to ODBC comes from point 2).
4. Synthetic fixture: `pure` equals `odbc` value-for-value for every MANIFEST table, including TYPED
   (all types) and the unicode/CR-LF/padding rows, for `.mdb` and `.accdb`.
5. `acade2sql doctor` output is correct on this machine; simulate a machine without the driver
   (monkeypatch) → `auto` picks `pure` with the warning.
6. Sources unchanged; no lock file from the pure reader, even while it is open.
