"""Reader backends and the :func:`open_reader` factory."""

from __future__ import annotations

from pathlib import Path

from acade2sql.readers.base import Reader
from acade2sql.readers.pyodbc_reader import PyodbcReader

__all__ = ["PyodbcReader", "Reader", "open_reader"]


def open_reader(path: str | Path, *, copy_to_temp: bool = False) -> Reader:
    """Open an Access database read-only with the best available backend (pyodbc for now)."""
    return PyodbcReader(path, copy_to_temp=copy_to_temp)
