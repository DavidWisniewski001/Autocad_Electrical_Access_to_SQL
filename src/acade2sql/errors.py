"""Exception hierarchy for acade2sql.

Every error raised on purpose by the package derives from :class:`Acade2SqlError`, so the CLI can
print a clean, actionable message instead of a traceback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from acade2sql.redact import redact

if TYPE_CHECKING:
    from sqlalchemy.engine import URL


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


class TargetError(Acade2SqlError):
    """The target URL is invalid, or its database driver is not installed."""


class UsageError(ValueError):
    """Invalid option combination (e.g. ``--key`` without ``--mode sync``); CLI exit 2."""


class SchemaError(Acade2SqlError):
    """The schema cannot be written as requested: schema drift between source and target, an
    identifier too long for PostgreSQL, a primary key SQL Server cannot index."""


class SyncKeyError(Acade2SqlError):
    """No usable sync key, or the source keys are NULL / not unique."""


class TargetDataError(Acade2SqlError):
    """A source value cannot be stored in this target (e.g. NUL in PostgreSQL text)."""


class WriteError(Acade2SqlError):
    """Writing one table to the target failed; the table's transaction was rolled back.

    When ``cause`` is itself an :class:`Acade2SqlError` (a check made by acade2sql, not a database
    error) ``detail`` is its message without the exception type name.
    """

    MAX_CAUSE_CHARS = 500

    def __init__(self, table: str, cause: BaseException, *, url: str | URL | None = None) -> None:
        """``url``: the target URL whose secrets are redacted from the message."""
        self.table = table
        self.cause = cause
        text = redact(str(getattr(cause, "orig", None) or cause), url)
        if len(text) > self.MAX_CAUSE_CHARS:
            text = text[: self.MAX_CAUSE_CHARS] + "..."
        if isinstance(cause, Acade2SqlError):
            self.detail = text
        else:
            self.detail = f"{type(cause).__name__}: {text}"
        super().__init__(f"Writing table {table!r} failed: {self.detail}")
