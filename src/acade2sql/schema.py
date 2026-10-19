"""Dialect-neutral schema description of an Access table (columns, primary key, indexes)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

PrimaryKeySource = Literal["discovered", "inferred", "none"]


@dataclass(frozen=True)
class ColumnInfo:
    """One column as reported by the ODBC catalog (``SQLColumns``)."""

    name: str
    ordinal: int
    access_type: str
    data_type: int
    size: int | None
    decimal_digits: int | None
    nullable: bool
    autoincrement: bool


@dataclass(frozen=True)
class IndexInfo:
    """One index as reported by ``SQLStatistics``; columns in key order."""

    name: str
    columns: tuple[str, ...]
    unique: bool


@dataclass(frozen=True)
class TableInfo:
    """A user table: ordered columns, primary key (with its provenance) and indexes."""

    name: str
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...]
    primary_key_source: PrimaryKeySource
    indexes: tuple[IndexInfo, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> ColumnInfo:
        """Return the column called ``name`` (exact match first, then case-insensitive)."""
        for col in self.columns:
            if col.name == name:
                return col
        folded = name.casefold()
        for col in self.columns:
            if col.name.casefold() == folded:
                return col
        raise KeyError(f"column {name!r} not found in table {self.name!r}")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation (tuples become lists)."""
        return {
            "name": self.name,
            "columns": [asdict(c) for c in self.columns],
            "primary_key": list(self.primary_key),
            "primary_key_source": self.primary_key_source,
            "indexes": [
                {"name": i.name, "columns": list(i.columns), "unique": i.unique}
                for i in self.indexes
            ],
        }
