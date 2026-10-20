"""``acade2sql doctor``: report what this machine can read and write."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import platform
import sys
from dataclasses import dataclass
from typing import TextIO

from acade2sql import __version__

log = logging.getLogger(__name__)


@dataclass
class Check:
    label: str
    ok: bool
    detail: str


def _module_version(module: str, dist: str | None = None) -> tuple[bool, str]:
    """(importable, version-or-error) without letting an import failure escape."""
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # ImportError, or a broken native extension
        return False, f"not importable ({type(exc).__name__}: {exc})"
    try:
        version = importlib.metadata.version(dist or module)
    except importlib.metadata.PackageNotFoundError:
        version = str(getattr(mod, "__version__", getattr(mod, "version", "?")))
    return True, version


def _odbc_drivers() -> tuple[list[str] | None, str]:
    try:
        import pyodbc

        return list(pyodbc.drivers()), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _access_reader_checks() -> list[Check]:
    from acade2sql import readers

    try:
        status = readers.diagnose_driver()
    except Exception as exc:
        return [Check("odbc reader", False, f"driver check failed: {type(exc).__name__}: {exc}")]
    drivers = ", ".join(status.access_drivers) or "none found"
    if sys.platform != "win32":
        # No registry, no 32/64-bit redistributable: the driver simply does not exist here.
        if not status.access_drivers:
            drivers += " (the Access ODBC driver is Windows-only; the pure reader is used)"
        return [
            Check("Access ODBC drivers", bool(status.access_drivers), drivers),
            Check("odbc reader", status.ok, status.message),
        ]
    checks = [Check("Access ODBC drivers", bool(status.access_drivers), drivers)]
    other = getattr(status, "other_bitness_driver", False) or _other_bitness_driver()
    other_bits = 32 if getattr(status, "python_bits", 64) == 64 else 64
    checks.append(
        Check(
            f"Access driver {other_bits}-bit",
            not other or status.ok,
            "installed (cannot be used by this Python)" if other else "not installed",
        )
    )
    checks.append(Check("odbc reader", status.ok, status.message))
    return checks


def _other_bitness_driver() -> bool:
    try:
        from acade2sql.diagnostics import _other_bitness_has_driver

        return _other_bitness_has_driver()
    except Exception:
        return False


def collect() -> list[Check]:
    """All checks, in display order (never raises)."""
    bits = 64 if sys.maxsize > 2**32 else 32
    checks = [
        Check("acade2sql", True, __version__),
        Check("Python", True, f"{platform.python_version()} ({bits}-bit) {sys.executable}"),
    ]
    checks.extend(_access_reader_checks())
    ok, version = _module_version("access_parser", "access-parser")
    detail = f"access-parser {version}, no driver needed" if ok else f"access-parser {version}"
    checks.append(Check("pure reader", ok, detail))
    targets = (
        ("psycopg", "psycopg", "psycopg (PostgreSQL)"),
        ("pyodbc", "pyodbc", "pyodbc (SQL Server)"),
    )
    for module, dist, label in targets:
        ok, version = _module_version(module, dist)
        checks.append(Check(label, ok, version))
    drivers, error = _odbc_drivers()
    if drivers is None:
        checks.append(Check("SQL Server ODBC drivers", False, f"cannot list drivers ({error})"))
    else:
        checks.append(mssql_driver_check(drivers))
    return checks


SUPPORTED_MSSQL_DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
_LEGACY_MSSQL_NOTE = "not supported by the writer (install ODBC Driver 17 or 18 for SQL Server)"


def mssql_driver_check(drivers: list[str]) -> Check:
    """Only ODBC Driver 17/18 count as ok; legacy drivers (e.g. the Windows built-in
    ``SQL Server``, Native Client) are listed as present but unsupported."""
    mssql = [d for d in drivers if "sql server" in d.lower()]
    supported = [d for d in mssql if d in SUPPORTED_MSSQL_DRIVERS]
    parts = list(supported)
    parts.extend(f"{d}: {_LEGACY_MSSQL_NOTE}" for d in mssql if d not in SUPPORTED_MSSQL_DRIVERS)
    if not parts:
        parts = ["none (install ODBC Driver 17 or 18 for SQL Server)"]
    return Check("SQL Server ODBC drivers", bool(supported), "; ".join(parts))


def readers_usable(checks: list[Check]) -> list[str]:
    usable = {"odbc reader": "odbc", "pure reader": "pure"}
    return [usable[c.label] for c in checks if c.label in usable and c.ok]


def run(out: TextIO) -> int:
    """Print the report to ``out``; 0 when at least one Access reader is usable, else 1."""
    checks = collect()
    width = max(len(c.label) for c in checks)
    for c in checks:
        mark = "ok " if c.ok else "-- "
        out.write(f"{mark}{c.label:<{width}}  {c.detail}\n")
    usable = readers_usable(checks)
    if usable:
        auto = "odbc" if "odbc" in usable else "pure"
        out.write(f"usable readers: {', '.join(usable)} (--reader auto uses {auto})\n")
        return 0
    out.write("usable readers: none (install the Access ODBC driver or access-parser)\n")
    return 1
