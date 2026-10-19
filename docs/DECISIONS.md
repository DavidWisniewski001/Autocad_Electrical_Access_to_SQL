# Design decisions

Each entry: decision — reason. Newest last.

1. **SQLAlchemy 2.x for writing** — one dialect-aware layer for SQLite/MSSQL/PostgreSQL; identifier
   quoting and DDL compilation come from the dialect instead of string concatenation.
2. **pyodbc + "Microsoft Access Driver (*.mdb, *.accdb)" as primary reader; `access-parser` fallback
   (M5)** — the driver is the most faithful reader; the fallback covers machines without it.
3. **Sources opened with `ReadOnly=1` and `pyodbc.connect(readonly=True)`** — M1 verified no lock file
   is left and size/mtime are unchanged. A `copy_to_temp` option is available for paranoid use.
4. **Table/column names preserved verbatim, quoted per dialect; no sanitising** — sanitising collides
   `ALLEN BRADLEY`/`ALLEN-BRADLEY` and `FERRAZ SHAWMUT`/`FERRAZ-SHAWMUT` (M1).
5. **Text copied verbatim** (no trim/case-fold/numeric coercion) — 2,674 padded values exist and
   numeric-looking identifiers (e.g. `PNEU-ACT.CATALOG`) must stay text.
6. **PK discovery order: unique index named `PrimaryKey` from `statistics()` → single COUNTER column
   (inferred) → none** — the driver does not implement `SQLPrimaryKeys` (M1). `TableInfo` records the
   source of the PK (`discovered` / `inferred` / `none`).
7. **RECNUM (COUNTER) values copied as-is and used as target PK and default sync key** — the only column
   unique in all 285 sample tables; natural keys become non-unique indexes.
8. **MEMO/LONGCHAR → unbounded text** (`TEXT` / `NVARCHAR(MAX)` / `TEXT`) — declared size 1 GB.
9. **Empty tables are created (schema only)** — fidelity; filters can exclude them.
10. **Metadata-driven conversion, no per-table code** — catalog has 43 column shapes, footprint 2.
11. **Driver-reported nullability is unreliable; PK members forced NOT NULL** (M2) — the Access driver
    reports `nullable=1` for `NOT NULL` text columns (verified on synthetic .mdb/.accdb). Columns of a
    discovered `PrimaryKey` index are set `nullable=False` (Access forbids NULL in PKs); COUNTER is
    reported correctly. Other `Required` columns cannot be detected via ODBC.
12. **Values are passed through as pyodbc returns them** (M2) — GUID is the unbraced upper-case string
    (`12345678-...`), YESNO is `bool` (Jet stores a NULL insert as False, so it never reads as NULL),
    LONGBINARY is normalised to `bytes`.
13. **MSys* tables are listed with `include_system=True` but are not resolvable by name** (M2) — the
    driver denies reading them ("no read permission"); `get_table`/`count_rows`/`iter_rows` raise
    `TableNotFoundError`.
14. **`ReadOnly=1;Exclusive=0` still creates `<name>.ldb` next to the source while connected** (M2) —
    removed on close (also for .accdb). Only `Exclusive=1` avoids it, but that locks out other users.
    Kept `Exclusive=0`; use `copy_to_temp=True` when even a transient lock file is unacceptable.
