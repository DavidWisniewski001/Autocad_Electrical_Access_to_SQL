# acade2sql — Project Plan

Convert AutoCAD Electrical (ACADE) Microsoft Access databases (`.mdb` Jet 4 / `.accdb` ACE) into a SQL
database via SQLAlchemy. **First target: SQLite.** SQL Server and PostgreSQL follow in M4.

## Environment (verified 2026-10-18)

| Item | Status |
|---|---|
| Python | 3.13.15, 64-bit |
| pyodbc | 5.3.0 (installed for M1) |
| ODBC driver | `Microsoft Access Driver (*.mdb, *.accdb)` present; bitness matches the interpreter |
| Read-only open | Both sample DBs open with `ReadOnly=1`; no `.ldb`/`.laccdb` lock file left behind |
| VCS | git, remote `github.com/DavidWisniewski001/Autocad_Electrical_Access_to_SQL`; commit at each verified milestone |

## Source databases (`data_files/`)

| File | Size | User tables | Role |
|---|---|---|---|
| `default_cat.mdb` | ~645 MB | 168 | Catalog: one table per family plus `_PINLIST_*` and other `_`-prefixed support tables |
| `footprint_lookup.accdb` | ~42 MB | 117 | Panel footprint lookup: one table per manufacturer |

No **project** DB (`<project>.mdb`) or `wd_picklist.mdb` sample exists yet. The project DB is the main
input in the requirements, so the test-engineer will build a synthetic ACADE-like project fixture
(COMP, PNLCOMP, TERMS, WIRENO, WFRM2ALL, NETS) until a real one is supplied.

## Requirements (source of truth)

1. Input: project DB, plus optional catalog, footprint lookup, and picklist DBs; `.mdb` and `.accdb`.
2. Reading: pyodbc + Access driver as the primary reader, with an actionable error when the driver is
   missing or its bitness doesn't match. Pure-Python fallback (`access-parser`) behind the same interface.
3. Writing: SQLAlchemy targets SQLite (default), `mssql+pyodbc`, and PostgreSQL. The connection string
   comes from `--target` or `ACADE2SQL_TARGET`; no hard-coded credentials.
4. Schema fidelity: exact table/column names with dialect quoting, a table-driven type map, and PKs and
   indexes where they can be discovered.
5. Data fidelity: exact row counts, no truncation, dates and times kept, NULL distinct from `''`,
   unicode, batched streaming.
6. Modes: `full`, `append`, `sync` (upsert by key); `--tables` include/exclude; `--dry-run` (plan + DDL).
7. Validation: console + JSON report with per-table source vs. target counts; non-zero exit on mismatch.
8. Interface: `acade2sql convert`, `acade2sql inspect`, an importable API, and `--verbose` structured logging.
9. Packaging: Python 3.10+, `pyproject.toml`, `src/acade2sql/`, pytest, ruff, and a README (driver
   install, bitness).
10. Safety: sources opened read-only and never modified or locked; copy to temp if the driver needs it.

## Milestones

| # | Scope | Owner | Acceptance criteria | Status |
|---|---|---|---|---|
| **M1** | Schema inventory and type mapping for both sample DBs | acade-schema-analyst | `docs/schema_inventory.md`, `docs/type_mapping.md`, and a per-table sync-key recommendation exist; the table counts match the live DBs (168 / 117); quirks are quantified, not asserted; sources unmodified | **Done 2026-10-18**, verified independently |
| M2 | Project skeleton (`pyproject.toml`, `src/acade2sql/`), `Reader` interface, pyodbc reader, driver/bitness diagnostics (dev); pytest harness and synthetic fixtures (test) | python-converter-dev, test-engineer | `pip install -e .` works; `acade2sql inspect data_files/default_cat.mdb` lists all 168 tables with columns; tests and `ruff check` pass | **Done 2026-10-18**: 271 tests pass, verified independently |
| M3 | `typemap.py`, `schema.py`, SQLAlchemy writer, SQLite target, `full` mode, `validate.py` report | dev + test | Both sample DBs convert to SQLite with every table's row count matching; NULL/`''`/unicode/date round-trip tests pass; JSON report written; exit code non-zero on an injected mismatch | **Done 2026-10-19**: 663 tests; catalog 168/168 and footprint 117/117 tables pass with checksums; verified independently |
| — | **Review gate 1** | code-reviewer | Confirmed findings fixed and re-verified | **Done 2026-10-19**: 4 major + 7 minor + 5 nits fixed, each with regression tests |
| M4 | MSSQL and PostgreSQL dialects, `append`/`sync`, `--dry-run` | dev + test | DDL generated for all three dialects (tested via SQLAlchemy compile; live servers optional); sync is idempotent on re-run | Pending |
| M5 | `access-parser` fallback reader, CLI polish, README | dev | The fallback reader gives the same table/row counts as pyodbc on the samples; README covers the driver redistributable and bitness | Pending |
| — | **Review gate 2** | code-reviewer | As above | Pending |

## Module interfaces (draft; finalised in M2)

```
readers/base.py      class Reader(Protocol):
                         list_tables() -> list[TableInfo]
                         get_table(name) -> TableInfo       # columns, PK, indexes
                         count_rows(name) -> int
                         iter_rows(name, batch_size) -> Iterator[list[tuple]]
readers/pyodbc_reader.py, readers/access_parser_reader.py
schema.py            TableInfo / ColumnInfo / IndexInfo dataclasses (dialect-neutral)
typemap.py           ACCESS_TYPES -> {sqlite, mssql, postgresql} (one table-driven dict)
writers/sqlalchemy_writer.py   build MetaData from TableInfo; create/drop; batched insert/upsert
validate.py          compare counts -> Report (console + JSON)
cli.py               convert | inspect
```

## M1 findings that shape the design (see `docs/schema_inventory.md`)

- Only three types occur: VARCHAR(n) (1,990 cols), LONGCHAR/MEMO (398), and COUNTER `RECNUM` (one per
  table). There are no dates, numerics, booleans, or BLOBs in these samples; the project DB will add more.
- 1,594,493 rows in the catalog and 261,858 in footprint_lookup. There are no `''` values (missing is
  always NULL) and no value overflows its declared size.
- `primaryKeys()` is unsupported by the driver; `statistics()` works for the catalog (non-unique
  indexes only). RECNUM is unique and non-null in all 285 tables.
- Read throughput is ~15k rows/s steady (60–80k on the first scan), so a full catalog read takes ~2 min.
- Catalog: 43 column-set shapes; footprint: 2. Everything is driven by metadata; no per-table code.

## Defaults adopted for M1 open questions (override any of them by telling the lead)

| Question | Default | Why |
|---|---|---|
| Table names | Preserve exactly; quote per dialect. No sanitising | Sanitising collides `ALLEN BRADLEY`/`ALLEN-BRADLEY` and `FERRAZ SHAWMUT`/`FERRAZ-SHAWMUT` |
| Text values | Verbatim (no trim, no case-folding, no numeric coercion) | 2,674 padded values; trimming merges keys |
| Empty tables (70 + 8) | Create them (schema-only) | Fidelity; `--tables` exclude can drop them |
| Footprint alias tables (`AB` vs `ALLEN BRADLEY`) | Keep separate | They are separate tables in the source |
| Primary key | `RECNUM` as the target PK, values copied as-is | Only column that is reliably unique |
| Sync key | RECNUM by default; `--key` override; natural keys created as non-unique indexes | No reliable natural key in large family tables |
| MEMO | `TEXT` / `NVARCHAR(MAX)` / `TEXT`, never `NVARCHAR(1073741823)` | Declared size is 1 GB |

## Risks and open questions

- The driver creates `<name>.ldb` next to the source **while connected** (removed on close), even with
  `ReadOnly=1`. Consider defaulting to `copy_to_temp=True` when ACADE may have the file open (DECISIONS #14).
- `ColumnInfo.nullable` is unreliable for NOT NULL text columns (DECISIONS #11).

- Is RECNUM stable across ACADE catalog versions? If not, cross-version sync needs a row-hash key.

- No real project DB sample → synthetic fixture for now. **Ask the user for one** (anonymised).
- Catalog table names contain `/`, spaces, and `-` → quoting needed everywhere, and possibly name
  sanitisation for some targets.
- The catalog is ~645 MB → streaming and batching are mandatory; SQLite write speed needs measuring.
- Catalog tables may have no PKs → sync mode needs a key-selection strategy (M1 recommends one).

## Decision log

See `docs/DECISIONS.md` (created in M2).
