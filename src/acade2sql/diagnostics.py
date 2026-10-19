"""Access ODBC driver detection and translation of connect errors into actionable messages."""

from __future__ import annotations

import logging
import re
import struct
import sys
from dataclasses import dataclass, field

import pyodbc

from acade2sql.errors import (
    Acade2SqlError,
    BitnessMismatchError,
    DriverNotFoundError,
    SourceNotFoundError,
)

log = logging.getLogger(__name__)

ACCESS_DRIVER_NAMES = ("Microsoft Access Driver (*.mdb, *.accdb)",)

REDIST_NAME = "Microsoft Access Database Engine 2016 Redistributable"
REDIST_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=54920"

_SQLSTATE_RE = re.compile(r"\b(IM\d{3}|HY\d{3}|HYT\d{2}|\d{2}[0-9A-Z]\d{2})\b")


@dataclass
class DriverStatus:
    """Result of :func:`diagnose_driver`."""

    ok: bool
    python_bits: int
    access_drivers: list[str]
    message: str
    other_bitness_driver: bool = field(default=False)


def python_bits() -> int:
    """Bitness of the running interpreter (32 or 64)."""
    return struct.calcsize("P") * 8


def _redist_hint(bits: int) -> str:
    arch = "x64" if bits == 64 else "x86"
    exe = "accessdatabaseengine_X64.exe" if bits == 64 else "accessdatabaseengine.exe"
    return (
        f"Install the {bits}-bit ({arch}) '{REDIST_NAME}' ({exe}) from {REDIST_URL} "
        f"so that the ODBC driver '{ACCESS_DRIVER_NAMES[0]}' is available to this {bits}-bit "
        f"Python ({sys.executable}). The driver bitness must match the Python bitness. If a "
        f"{_other(bits)}-bit Microsoft Office is installed, either run the installer with /quiet "
        f"or use a {_other(bits)}-bit Python instead."
    )


def _other(bits: int) -> int:
    return 32 if bits == 64 else 64


def missing_driver_message(bits: int | None = None, other_bitness: bool = False) -> str:
    """The actionable text used whenever the Access driver cannot be used."""
    bits = bits or python_bits()
    if other_bitness:
        head = (
            f"The Microsoft Access ODBC driver is installed only for {_other(bits)}-bit "
            f"applications, but this Python is {bits}-bit (32/64-bit mismatch)."
        )
    else:
        head = (
            f"The Microsoft Access ODBC driver '{ACCESS_DRIVER_NAMES[0]}' was not found for this "
            f"{bits}-bit Python."
        )
    return f"{head} {_redist_hint(bits)}"


def _other_bitness_has_driver() -> bool:
    """Best effort: is an Access driver registered for the *other* bitness (Windows only)?"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return False
    # From 64-bit Python, 32-bit drivers live under WOW6432Node; from 32-bit Python, ask for
    # the 64-bit registry view explicitly.
    if python_bits() == 64:
        key_path, access = r"SOFTWARE\WOW6432Node\ODBC\ODBCINST.INI\ODBC Drivers", 0
    else:
        key_path, access = r"SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers", winreg.KEY_WOW64_64KEY
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | access) as k:
            i = 0
            while True:
                try:
                    name, _value, _type = winreg.EnumValue(k, i)
                except OSError:
                    return False
                if name in ACCESS_DRIVER_NAMES:
                    return True
                i += 1
    except OSError:
        return False


def _is_access_driver(name: str) -> bool:
    lowered = name.lower()
    return "access driver" in lowered or "*.mdb" in lowered


def diagnose_driver(drivers: list[str] | None = None) -> DriverStatus:
    """Check whether a usable Access ODBC driver is installed.

    ``drivers=None`` queries ``pyodbc.drivers()`` (and, on Windows, the registry for a driver of
    the other bitness). Passing a list makes the check deterministic for tests.
    """
    bits = python_bits()
    probe_registry = drivers is None
    installed = list(pyodbc.drivers()) if drivers is None else list(drivers)
    access = [d for d in installed if _is_access_driver(d)]
    usable = [d for d in ACCESS_DRIVER_NAMES if d in installed]
    if usable:
        msg = f"OK: '{usable[0]}' is available to this {bits}-bit Python."
        return DriverStatus(True, bits, access, msg)
    other = _other_bitness_has_driver() if probe_registry else False
    msg = missing_driver_message(bits, other_bitness=other)
    if access:
        msg += f" Other Access drivers found (cannot open .accdb): {', '.join(access)}."
    return DriverStatus(False, bits, access, msg, other_bitness_driver=other)


def require_driver() -> str:
    """Return the Access driver name, or raise :class:`DriverNotFoundError` (or its subclass)."""
    status = diagnose_driver()
    if status.ok:
        return next(d for d in ACCESS_DRIVER_NAMES if d in status.access_drivers)
    log.debug("access driver missing drivers=%s", status.access_drivers)
    if status.other_bitness_driver:
        raise BitnessMismatchError(status.message)
    raise DriverNotFoundError(status.message)


def _sqlstate(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], str) and args[0].strip():
        return args[0].strip().upper()
    match = _SQLSTATE_RE.search(str(exc))
    return match.group(1) if match else ""


def translate_connect_error(exc: pyodbc.Error) -> Acade2SqlError:
    """Map a ``pyodbc.connect`` failure to the matching acade2sql exception (returned)."""
    state = _sqlstate(exc)
    text = str(exc)
    lowered = text.lower()
    bits = python_bits()
    if state == "IM014" or "architecture mismatch" in lowered:
        return BitnessMismatchError(missing_driver_message(bits, other_bitness=True))
    if state == "IM002" or "data source name not found" in lowered:
        return DriverNotFoundError(f"{missing_driver_message(bits)} (driver error: {text})")
    if "could not find file" in lowered or "not a valid path" in lowered:
        return SourceNotFoundError(f"The Access driver could not open the file: {text}")
    if "already in use" in lowered or "exclusively locked" in lowered:
        return Acade2SqlError(
            f"The database is locked by another program (close Access/AutoCAD Electrical, or read "
            f"a copy: --copy-to-temp on the command line, copy_to_temp=True in the API): {text}"
        )
    return Acade2SqlError(f"Could not open the Access database (SQLSTATE {state or '?'}): {text}")
