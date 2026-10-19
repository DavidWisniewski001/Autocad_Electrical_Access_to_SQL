"""Abstract read-only interface over an Access database, shared by all reader backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

from acade2sql.schema import TableInfo


class Reader(ABC):
    """Read-only access to one Access database. Use as a context manager.

    Row values: ``str`` verbatim (no strip; ``''`` stays ``''``), NULL as ``None``, dates as
    ``datetime.datetime``, BIT as ``bool``, CURRENCY/DECIMAL as ``decimal.Decimal``, LONGBINARY as
    ``bytes`` and GUID as ``str``.
    """

    path: Path

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
