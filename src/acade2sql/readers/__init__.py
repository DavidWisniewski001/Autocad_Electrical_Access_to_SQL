"""Reader backends and the :func:`open_reader` factory.

``odbc`` (:class:`PyodbcReader`, Microsoft Access ODBC driver) is the reference reader; ``pure``
(:class:`AccessParserReader`) needs no driver. ``auto`` picks ``odbc`` when the driver is usable,
else ``pure`` with one warning. Both backends are imported lazily, so ``pure`` works even where
pyodbc cannot be imported.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from acade2sql.readers.base import Reader

if TYPE_CHECKING:
    from acade2sql.diagnostics import DriverStatus
    from acade2sql.readers.access_parser_reader import AccessParserReader
    from acade2sql.readers.pyodbc_reader import PyodbcReader

__all__ = [
    "READER_CHOICES",
    "AccessParserReader",
    "PyodbcReader",
    "Reader",
    "ReaderChoice",
    "diagnose_driver",
    "open_reader",
]

log = logging.getLogger(__name__)

ReaderChoice = Literal["auto", "odbc", "pure"]
READER_CHOICES: tuple[str, ...] = ("auto", "odbc", "pure")

FALLBACK_WARNING = (
    "Microsoft Access ODBC driver not usable; falling back to the pure-Python reader "
    "(--reader pure). It returns the same data; its speed is comparable on the ACADE sample "
    "databases but can differ on other files, and it refuses password-protected/encrypted "
    "databases and attachment/calculated fields. To use the reference ODBC reader, install the "
    "driver ({hint}). {detail}"
)


def diagnose_driver() -> DriverStatus:
    """The ODBC driver check used by ``auto`` (looked up at call time, so tests can patch it)."""
    from acade2sql import diagnostics

    return diagnostics.diagnose_driver()


def _install_hint() -> str:
    from acade2sql.diagnostics import REDIST_NAME, REDIST_URL

    return f"'{REDIST_NAME}', {REDIST_URL}; check with: acade2sql doctor"


def open_reader(
    path: str | Path, *, copy_to_temp: bool = False, reader: ReaderChoice = "auto"
) -> Reader:
    """Open an Access database read-only.

    ``reader``: ``"odbc"`` (driver errors propagate), ``"pure"`` (never touches pyodbc) or
    ``"auto"`` (``odbc`` when :func:`diagnose_driver` says the driver is usable, else ``pure``
    with one WARNING). Any other value raises ``ValueError``.
    """
    if reader not in READER_CHOICES:
        raise ValueError(f"reader must be one of {', '.join(READER_CHOICES)}, got {reader!r}")
    if reader == "auto":
        try:
            status = diagnose_driver()
            ok, detail = status.ok, status.message
        except (ImportError, OSError) as exc:  # pyodbc / libodbc not loadable: no driver
            from acade2sql.diagnostics import pyodbc_unavailable_message

            ok, detail = False, pyodbc_unavailable_message(exc)
        if ok:
            reader = "odbc"
        else:
            reader = "pure"
            log.warning(FALLBACK_WARNING.format(hint=_install_hint(), detail=detail))
    if reader == "pure":
        from acade2sql.readers.access_parser_reader import AccessParserReader

        return AccessParserReader(path, copy_to_temp=copy_to_temp)
    try:
        from acade2sql.readers.pyodbc_reader import PyodbcReader
    except (ImportError, OSError) as exc:  # pyodbc / libodbc not loadable
        from acade2sql.diagnostics import pyodbc_unavailable_message
        from acade2sql.errors import DriverNotFoundError

        raise DriverNotFoundError(pyodbc_unavailable_message(exc)) from None
    return PyodbcReader(path, copy_to_temp=copy_to_temp)


def __getattr__(name: str) -> Any:  # lazy exports: PyodbcReader imports pyodbc
    if name == "PyodbcReader":
        from acade2sql.readers.pyodbc_reader import PyodbcReader

        return PyodbcReader
    if name == "AccessParserReader":
        from acade2sql.readers.access_parser_reader import AccessParserReader

        return AccessParserReader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
