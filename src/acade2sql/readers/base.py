"""Abstract read-only interface over an Access database, shared by all reader backends."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Literal

from acade2sql.errors import SourceNotFoundError, TableNotFoundError
from acade2sql.schema import ColumnInfo, IndexInfo, PrimaryKeySource, TableInfo

log = logging.getLogger(__name__)

ACCESS_SUFFIXES = frozenset({".mdb", ".accdb"})
PRIMARY_KEY_INDEX = "primarykey"
ReaderKind = Literal["odbc", "pure"]
_CLEANUP_RETRIES = 10
_CLEANUP_DELAY_S = 0.2


def validate_source(path: Path) -> Path:
    """Resolved ``path`` if it is an existing .mdb/.accdb file, else ``SourceNotFoundError``."""
    if path.suffix.lower() not in ACCESS_SUFFIXES:
        raise SourceNotFoundError(f"Not an Access database (expected .mdb or .accdb): {path}")
    if not path.is_file():
        raise SourceNotFoundError(f"Access database not found: {path}")
    return path.resolve()


def primary_key_rule(
    columns: Sequence[ColumnInfo], indexes: Sequence[IndexInfo]
) -> tuple[tuple[str, ...], PrimaryKeySource]:
    """DECISIONS #6: unique index 'PrimaryKey' -> single COUNTER column -> none."""
    for idx in indexes:
        if idx.unique and idx.name.casefold() == PRIMARY_KEY_INDEX:
            return idx.columns, "discovered"
    counters = [c.name for c in columns if c.autoincrement]
    if len(counters) == 1:
        return (counters[0],), "inferred"
    return (), "none"


def build_table_info(
    name: str, columns: tuple[ColumnInfo, ...], indexes: tuple[IndexInfo, ...]
) -> TableInfo:
    """TableInfo with the PK rule applied; discovered PK columns are made NOT NULL (#11)."""
    pk, source = primary_key_rule(columns, indexes)
    if source == "discovered":
        pk_cols = set(pk)
        columns = tuple(replace(c, nullable=False) if c.name in pk_cols else c for c in columns)
    return TableInfo(
        name=name, columns=columns, primary_key=pk, primary_key_source=source, indexes=indexes
    )


def resolve_table(name: str, known: Sequence[str], path: Path) -> str:
    """Map a user-supplied name to the exact user-table name (exact, then unique case-insensitive
    match), or raise ``TableNotFoundError``."""
    if name in known:
        return name
    folded = name.casefold()
    matches = [n for n in known if n.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    raise TableNotFoundError(f"Table {name!r} not found in {path}")


def check_batch_size(batch_size: object) -> None:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError(f"batch_size must be an integer >= 1, got {batch_size!r}")


class TempCopy:
    """Private temporary copy of the source (``copy_to_temp=True``), removed by :meth:`cleanup`."""

    def __init__(self, source: Path) -> None:
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = tempfile.TemporaryDirectory(
            prefix="acade2sql_", ignore_cleanup_errors=True
        )
        self.path = Path(self._tmpdir.name) / source.name
        shutil.copy2(source, self.path)
        log.debug("copied source to temp", extra={"kv": {"temp": str(self.path)}})

    def cleanup(self) -> None:
        """Remove the copy (idempotent). Retries briefly: a driver can hold the file a moment."""
        if self._tmpdir is None:
            return
        tmp = Path(self._tmpdir.name)
        self._tmpdir.cleanup()  # ignore_cleanup_errors=True: never raises
        self._tmpdir = None
        for _ in range(_CLEANUP_RETRIES):
            if not tmp.exists():
                break
            time.sleep(_CLEANUP_DELAY_S)
            shutil.rmtree(tmp, ignore_errors=True)
        if self.path.exists():
            log.warning(
                "temporary copy of the source could not be removed",
                extra={"kv": {"path": str(self.path)}},
            )


class Reader(ABC):
    """Read-only access to one Access database. Use as a context manager.

    Row values: ``str`` verbatim (no strip; ``''`` stays ``''``), NULL as ``None``, dates as
    ``datetime.datetime``, BIT as ``bool``, CURRENCY/DECIMAL as ``decimal.Decimal``, LONGBINARY as
    ``bytes`` and GUID as ``str``.
    """

    path: Path
    #: ``"odbc"`` or ``"pure"``: recorded in the report and ``inspect --json`` as ``"reader"``.
    kind: str = "odbc"

    def __enter__(self) -> Reader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @abstractmethod
    def close(self) -> None:
        """Release the connection and any temporary copy. Idempotent."""

    @abstractmethod
    def list_tables(self, include_system: bool = False) -> list[str]:
        """User tables sorted case-insensitively; ``include_system`` adds visible MSys* tables."""

    @abstractmethod
    def get_table(self, name: str) -> TableInfo:
        """Schema of one table. Raises ``TableNotFoundError``."""

    @abstractmethod
    def count_rows(self, name: str) -> int:
        """``COUNT(*)`` of one table. Raises ``TableNotFoundError``."""

    @abstractmethod
    def iter_rows(self, name: str, batch_size: int = 5000) -> Iterator[list[tuple]]:
        """Yield batches of plain tuples in column-ordinal order. ``batch_size`` must be >= 1."""
