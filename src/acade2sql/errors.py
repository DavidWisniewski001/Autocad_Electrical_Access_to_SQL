"""Exception hierarchy for acade2sql.

Every error raised on purpose by the package derives from :class:`Acade2SqlError`, so the CLI can
print a clean, actionable message instead of a traceback.
"""

from __future__ import annotations


class Acade2SqlError(Exception):
    """Base class for all expected acade2sql failures."""


class SourceNotFoundError(Acade2SqlError):
    """The source database path does not exist or is not an .mdb/.accdb file."""


class DriverNotFoundError(Acade2SqlError):
    """The Microsoft Access ODBC driver is not installed (for this Python's bitness)."""


class BitnessMismatchError(DriverNotFoundError):
    """An Access driver exists, but only for the other bitness (32 vs 64-bit)."""


class TableNotFoundError(Acade2SqlError):
    """The requested table does not exist in the source database."""
