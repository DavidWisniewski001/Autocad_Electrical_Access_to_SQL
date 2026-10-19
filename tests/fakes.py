"""Driver-free helpers for the M4 tests: an in-memory Reader and small schema builders."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from acade2sql.readers.base import Reader
from acade2sql.schema import ColumnInfo, TableInfo


def col(name, ordinal, access_type, *, nullable=True, size=None, autoinc=False):
    return ColumnInfo(name, ordinal, access_type, 0, size, None, nullable, autoinc)


def make_info(name, columns, pk=("id",), indexes=(), pk_source=None):
    """``columns``: list of (name, access_type[, size[, nullable]])."""
    cols = []
    for i, spec in enumerate(columns, 1):
        cname, atype, *rest = spec
        size = rest[0] if rest else None
        nullable = rest[1] if len(rest) > 1 else cname not in pk
        cols.append(col(cname, i, atype, nullable=nullable, size=size))
    src = pk_source or ("discovered" if pk else "none")
    return TableInfo(name, tuple(cols), tuple(pk), src, tuple(indexes))


STD_COLS = [
    ("id", "COUNTER", None, False),
    ("label", "VARCHAR", 50),
    ("code", "VARCHAR", 20),
    ("note", "LONGCHAR"),
]


def std_info(name="T", pk=("id",), indexes=()):
    return make_info(name, STD_COLS, pk=pk, indexes=indexes)


def std_rows(ids, tag=""):
    return [(i, f"label{i}{tag}", f"c{i}", None) for i in ids]


class FakeReader(Reader):
    """In-memory Reader. ``tables``: name -> (TableInfo, rows)."""

    def __init__(self, tables):
        self.path = Path("fake.mdb")
        self.tables = dict(tables)
        self.count_calls: list[str] = []

    def close(self):
        pass

    def list_tables(self, include_system=False):
        return sorted(self.tables, key=str.casefold)

    def get_table(self, name):
        return self.tables[name][0]

    def count_rows(self, name):
        self.count_calls.append(name)
        return len(self.tables[name][1])

    def iter_rows(self, name, batch_size=5000):
        rows = self.tables[name][1]
        for i in range(0, len(rows), batch_size):
            yield [tuple(r) for r in rows[i : i + batch_size]]


def sqlite_url(p: Path) -> str:
    return "sqlite:///" + Path(p).as_posix()


def dump(db: Path, table: str) -> list[tuple]:
    """Raw rows of one SQLite table, ordered by the first column."""
    con = sqlite3.connect(db)
    try:
        q = table.replace('"', '""')
        return con.execute(f'SELECT * FROM "{q}" ORDER BY 1').fetchall()
    finally:
        con.close()


def sql(db: Path, stmt: str, params=()):
    con = sqlite3.connect(db)
    try:
        cur = con.execute(stmt, params)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def by_name(report):
    return {t.name: t for t in report.tables}
