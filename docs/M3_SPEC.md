# M3 spec: type map, SQLAlchemy writer, SQLite target, `full` mode, validation

This is the contract between `python-converter-dev` (owns `src/`) and `test-engineer` (owns `tests/`).
Implement exactly these names and signatures; private helpers are free. Context: PLAN.md,
docs/DECISIONS.md (#4–#14 apply), docs/type_mapping.md, docs/schema_inventory.md.

## 1. `src/acade2sql/typemap.py` (one table-driven module)

```python
DIALECTS = ("sqlite", "mssql", "postgresql")
TYPE_MAP: dict[str, TypeRule]          # key = ColumnInfo.access_type (upper-case ODBC type_name)
def sa_type(col: ColumnInfo, dialect: str) -> sqlalchemy.types.TypeEngine
def is_known(access_type: str) -> bool
```
`TypeRule` is a small frozen dataclass holding one factory per dialect: `Callable[[ColumnInfo], TypeEngine]`.
Unknown `access_type` → unbounded text + one WARNING log per type (never crash). Unknown dialect →
`ValueError`. Aliases: `TEXT`→VARCHAR, `CHAR`→VARCHAR, `MEMO`/`LONGTEXT`→LONGCHAR, `LONG`→INTEGER,
`YESNO`→BIT, `AUTOINCREMENT`→COUNTER, `NUMERIC`→DECIMAL, `MONEY`→CURRENCY, `FLOAT`→DOUBLE,
`UNIQUEIDENTIFIER`/`REPLICATIONID`→GUID, `VARBINARY`/`BINARY`/`IMAGE`/`OLE`→LONGBINARY.

| access_type | sqlite | mssql | postgresql |
|---|---|---|---|
| VARCHAR(n) | `String(n)` | `NVARCHAR(n)` (`sqlalchemy.dialects.mssql.NVARCHAR`) | `String(n)` |
| LONGCHAR | `Text` | `NVARCHAR(max)` (`mssql.NVARCHAR()` with no length) | `Text` |
| COUNTER | `Integer` | `Integer` | `Integer` |
| INTEGER (32-bit) | `Integer` | `Integer` | `Integer` |
| SMALLINT, BYTE | `SmallInteger` | `SmallInteger` | `SmallInteger` |
| REAL | `Float(24)` (as float) | `REAL` | `REAL` |
| DOUBLE | `Double` (as float) | `Double` (FLOAT(53)) | `DOUBLE_PRECISION` |
| CURRENCY | `DecimalText` (below) | `Numeric(19,4)` | `Numeric(19,4)` |
| DECIMAL(p,s) | `DecimalText` | `Numeric(p,s)` | `Numeric(p,s)` |
| DATETIME | `DateTime` | `DATETIME2` | `TIMESTAMP` (no tz) |
| BIT | `Boolean` | `BIT` | `Boolean` |
| GUID | `String(36)` | `UNIQUEIDENTIFIER` (`mssql.UNIQUEIDENTIFIER(as_uuid=False)`) | `Uuid(as_uuid=False)` |
| LONGBINARY | `LargeBinary` | `VARBINARY(max)` | `LargeBinary` (BYTEA) |

* **Never** emit a length ≥ 4001 for `NVARCHAR` or ≥ 8001 for `VARBINARY`; any VARCHAR wider than 4000
  (not seen in the samples) → `NVARCHAR(max)`.
* `DecimalText`: a `TypeDecorator` that stores `decimal.Decimal` as canonical TEXT on SQLite
  (SQLite's NUMERIC goes through float and loses digits) and returns `Decimal` on read, exactly
  equal to the input. `cache_ok = True`.
* Only SQLite is converted end-to-end in M3. The mssql/postgresql columns must compile
  (`CreateTable(t).compile(dialect=...)`) and are tested that way; live servers come in M4.

## 2. `src/acade2sql/writers/` (package) — `sqlalchemy_writer.py`

```python
def build_table(metadata: MetaData, info: TableInfo, dialect: str, *, used_index_names: set[str]) -> Table
class SqlAlchemyWriter:
    def __init__(self, url: str, *, echo: bool = False) -> None      # creates the Engine
    dialect: str                                                      # engine.dialect.name
    def prepare(self, tables: Sequence[TableInfo], mode: Literal["full"] = "full") -> None   # no DDL
    def create_table(self, info: TableInfo) -> None   # drop if exists + create table and indexes
    def write_batches(self, info: TableInfo, batches: Iterable[list[tuple]]) -> int   # returns rows written
    def count_rows(self, name: str) -> int
    def read_rows(self, name: str, batch_size: int = 5000) -> Iterator[list[tuple]]   # ordered by PK if any; values typed via the SQLAlchemy types; columns in ordinal order
    def close(self) -> None   # also a context manager
```
* Names are verbatim (DECISIONS #4); quoting is the dialect's job (`quote=True` on every Table/Column/Index
  so case and special characters survive). **No SQL built by string concatenation with identifiers.**
* Columns in ordinal order, `nullable` from ColumnInfo (PK members are NOT NULL). PK = `info.primary_key`
  (`PrimaryKeyConstraint`); COUNTER/PK columns get `autoincrement=False` (source values are inserted as-is).
* Indexes: every `info.indexes` entry except the one that is the PK. Target index names must be unique
  database-wide (SQLite/PG): name = `ix_<table>_<index>` sanitised to `[A-Za-z0-9_]`, ≤ 60 chars, with an
  8-hex-char hash suffix when truncated or already in `used_index_names`. Keep the source name in
  `Index.info["source_name"]`. Unique stays unique.
* `prepare(mode="full")`: builds the MetaData for all given tables and reserves their index names
  (connects once; `TargetError` if unreachable). **No DDL.** `create_table(info)`: drop if it exists
  (`Table.drop(checkfirst=True)`) then create the table and its indexes; `WriteError` (redacted) on
  failure. `convert` calls it per table right before `write_batches` (review gate 1, DECISIONS #20).
  **Tables in the target that are not being converted are left alone.**
* PK index = a unique index whose columns equal `info.primary_key` (the name is not consulted).
* `write_batches`: one transaction per table; insert via SQLAlchemy bound parameters
  (`connection.execute(table.insert(), list_of_dicts)` executemany; on SQLite only, the faster
  compiled-INSERT + DBAPI `executemany` path of DECISIONS #16). On error roll back the table's transaction and raise `WriteError(table, cause)`.
* SQLite load tuning: for SQLite targets set `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`
  on connect (event listener). Record it in DECISIONS.md.
* Secrets never appear in logs, reports or error messages: `acade2sql.redact.mask_url(url)` for
  display and `redact(text, url)` for exception text (DECISIONS #22). Bad target URLs
  (`ArgumentError`, `ValueError`, `TypeError`, `NoSuchModuleError`, missing DBAPI) → `TargetError`.

## 3. `src/acade2sql/validate.py`

```python
@dataclass
class TableResult:
    name: str; source_rows: int | None; target_rows: int | None   # None: could not be counted
    status: Literal["pass", "fail", "error"]; error: str | None = None
    seconds: float = 0.0; checksum_match: bool | None = None
    warnings: list[str] = []   # e.g. "unmapped type <T> in column <C> stored as text"
@dataclass
class Report:
    source: str; target: str  # password-masked
    mode: str; started: str; finished: str   # ISO-8601 UTC
    tables: list[TableResult]
    ok -> bool  (property: every table "pass"; an empty table list is ok)
    to_dict() -> dict; write_json(path) -> None; render_text() -> str
def table_checksum(rows: Iterable[list[tuple]]) -> str
```
* `status`: `pass` when counts are equal and (if computed) checksums match; `fail` on a mismatch or
  when the table has an unmapped Access type (DECISIONS #21); `error` when an exception occurred
  (redacted message in `error`, prefixed `read failed:` or `write failed:`).
* `table_checksum`: order-independent hash of the rows (e.g. sum of SHA-256 per row mod 2**256, hex).
  Normalise each value before hashing: `None`→a NULL marker distinct from `''`; `str` as-is (no strip);
  `bool` before `int`; `int`; `float` via `repr`; `Decimal` via `normalize()` string; `datetime` via
  `isoformat()`; `bytes` hex; GUID strings (a full match of `8-4-4-4-12` hex, bare or inside ONE
  balanced pair of braces) compared upper-case without the braces. The same function is
  applied to source rows (reader.iter_rows) and target rows (writer.read_rows).
* `render_text()`: one line per table (`PASS|FAIL|ERROR <name> source=N target=M`, `-` for an
  unknown count, ` warnings=N` appended when non-zero), then the summary line
  `N tables: P passed, F failed, E errors`. `to_dict()` includes each table's `warnings`.

## 4. `src/acade2sql/converter.py` (public API, exported from `acade2sql`)

`from acade2sql import convert, resolve_target` gives the plain functions; there is no module named
`acade2sql.convert` (DECISIONS #18).

```python
def convert(source: str | Path, target: str | None = None, *, mode: Literal["full"] = "full",
            tables: Sequence[str] | None = None, exclude: Sequence[str] | None = None,
            batch_size: int = 5000, checksum: bool = False, copy_to_temp: bool = False,
            report_path: str | Path | None = None) -> Report
def resolve_target(target: str | None, source: Path) -> str
```
* `resolve_target`: explicit `target` → as-is; else env `ACADE2SQL_TARGET`; else
  `sqlite:///<cwd>/<source stem>.sqlite`. A plain filesystem path ending `.sqlite`/`.db`/`.sqlite3` is
  turned into a `sqlite:///` URL (absolute path) via `URL.create("sqlite", database=...)`, so the path
  is escaped and never URL-decoded (`a%20b.sqlite` opens exactly that file).
* `tables`/`exclude`: case-insensitive exact names; an unknown name in `tables` → `TableNotFoundError`
  before anything is written. Exclude wins over include.
* Flow: open reader → collect TableInfo for the selected tables → `writer.prepare` (no DDL) → per
  table: source count, `writer.create_table(info)`, `write_batches(info, reader.iter_rows(...))`,
  target count, optional checksum → TableResult. An exception in one table (DDL included) is
  recorded as `error` and conversion continues with the next table. The report is always written
  when `report_path` is given. An empty selection logs a WARNING and returns an (ok) empty report.
* One INFO log line per table with rows and rows/s.

## 5. CLI (`cli.py`)

```
acade2sql [--verbose] convert <source> [--target URL|PATH] [--mode full] [--tables T [T ...]]
          [--exclude T [T ...]] [--batch-size N] [--checksum] [--copy-to-temp] [--report PATH.json]
```
Prints `report.render_text()` to stdout. Exit codes: `0` all pass (also an empty selection, with
`warning: no tables selected` on stderr), `1` Acade2SqlError before any target modification (bad
source, driver, unknown table, bad target URL, unreachable target, uncreatable folder; redacted, no
traceback), `2` usage, **`3` validation failure** (any fail/error, including per-table DDL errors).
Logging: `configure_logging` is idempotent, writes to the current `sys.stderr` at emit time and sets
`propagate=False` on the `acade2sql` logger.
`WriteError(Acade2SqlError)` is added to `errors.py` (attributes `table`, `cause`).

## 6. Acceptance (the orchestrator re-verifies)

1. `ruff check src tests` and `ruff format --check src` clean; full `pytest` green.
2. `acade2sql convert data_files/footprint_lookup.accdb --target <tmp>/fp.sqlite --checksum --report <tmp>/fp.json`
   → exit 0, 117 tables pass, checksums match, JSON valid.
3. `acade2sql convert data_files/default_cat.mdb --target <tmp>/cat.sqlite` → exit 0, 168 tables /
   1,594,493 rows pass; elapsed time reported (target < 5 min).
4. Synthetic fixture round-trip is value-exact for every type (NULL vs '', padding, unicode, CR/LF,
   DATETIME, BIT, CURRENCY/DECIMAL exact, DOUBLE, bytes incl. `\x00`, GUID, 6,400-char MEMO).
5. Re-running `full` does not double rows; unrelated tables in the target survive.
6. An injected write failure gives `status="error"` for that table, other tables still converted, exit 3.
7. Source files unchanged, no lock file left in data_files/.
