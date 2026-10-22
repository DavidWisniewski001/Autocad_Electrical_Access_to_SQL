"""Tk-free GUI logic: form values -> target URL and :func:`acade2sql.convert` keyword arguments.

Everything here is plain Python so it can be unit-tested without a display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.engine import URL

from acade2sql.converter import sqlite_url
from acade2sql.keys import NO_KEY_CHOICES
from acade2sql.readers import READER_CHOICES
from acade2sql.redact import mask_url
from acade2sql.writers.sqlalchemy_writer import MODES

TargetKind = Literal["sqlite", "mssql", "postgresql", "url"]
TARGET_KINDS: tuple[TargetKind, ...] = ("sqlite", "mssql", "postgresql", "url")
TARGET_LABELS: dict[TargetKind, str] = {
    "sqlite": "SQLite file",
    "mssql": "SQL Server",
    "postgresql": "PostgreSQL",
    "url": "Custom URL",
}
MSSQL_DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
PG_SSLMODES = ("", "disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_PG_PORT = 5432
DEFAULT_BATCH_SIZE = 5000

_NAME_SPLIT = re.compile(r"[,\r\n]+")
_KEY_SPLIT = re.compile(r"[;\r\n]+")


class OptionsError(ValueError):
    """A form value is missing or invalid; the message is shown to the user as is."""


@dataclass
class ServerFields:
    """Connection fields for the SQL Server / PostgreSQL targets."""

    host: str = ""
    port: str = ""
    database: str = ""
    user: str = ""
    password: str = ""
    instance: str = ""  # SQL Server named instance (host\\instance)
    driver: str = MSSQL_DRIVERS[0]  # SQL Server ODBC driver
    windows_auth: bool = False  # SQL Server: trusted_connection=yes, no user/password
    trust_server_certificate: bool = False  # SQL Server: TrustServerCertificate=yes
    sslmode: str = ""  # PostgreSQL ?sslmode=


@dataclass
class GuiOptions:
    """Everything the main window collects. Strings are raw entry text."""

    source: str = ""
    target_kind: TargetKind = "sqlite"
    sqlite_path: str = ""
    server: ServerFields = field(default_factory=ServerFields)
    url: str = ""
    mode: str = "full"
    reader: str = "auto"
    copy_to_temp: bool = False
    checksum: bool = False
    counts: bool = True  # dry run only
    report_path: str = ""
    schema: str = ""
    tables: str = ""
    exclude: str = ""
    batch_size: str = str(DEFAULT_BATCH_SIZE)
    keys: str = ""  # sync only; specs separated by ';' or newlines
    no_key: str = "error"  # sync only
    delete_missing: bool = False  # sync only


def parse_names(text: str) -> list[str] | None:
    """Table names separated by commas or newlines (as ``--tables A,B``); None when empty."""
    names = [n.strip() for n in _NAME_SPLIT.split(text or "")]
    names = [n for n in names if n]
    return names or None


def parse_keys(text: str) -> list[str] | None:
    """``--key`` specs separated by ``;`` or newlines (a spec itself may contain commas)."""
    specs = [s.strip() for s in _KEY_SPLIT.split(text or "")]
    specs = [s for s in specs if s]
    return specs or None


def parse_batch_size(text: str) -> int:
    try:
        value = int(str(text).strip())
    except ValueError:
        raise OptionsError(f"Batch size must be a whole number, got {text!r}") from None
    if value < 1:
        raise OptionsError(f"Batch size must be at least 1, got {value}")
    return value


def _port(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise OptionsError(f"Port must be a number between 1 and 65535, got {text!r}")
    return int(text)


def _require(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise OptionsError(f"{label} is required")
    return value


def mssql_url(f: ServerFields) -> str:
    """``mssql+pyodbc://`` URL from the form fields (the password is URL-encoded)."""
    host = _require(f.host, "SQL Server host")
    if f.instance.strip():
        host = f"{host}\\{f.instance.strip()}"
    query: dict[str, str] = {"driver": _require(f.driver, "SQL Server ODBC driver")}
    if f.windows_auth:
        query["trusted_connection"] = "yes"
    if f.trust_server_certificate:
        query["TrustServerCertificate"] = "yes"
    user = None if f.windows_auth else (f.user.strip() or None)
    password = None if f.windows_auth or user is None else (f.password or None)
    url = URL.create(
        "mssql+pyodbc",
        username=user,
        password=password,
        host=host,
        port=_port(f.port),
        database=_require(f.database, "Database"),
        query=query,
    )
    return url.render_as_string(hide_password=False)


def postgresql_url(f: ServerFields) -> str:
    """``postgresql+psycopg://`` URL from the form fields (the password is URL-encoded)."""
    user = f.user.strip() or None
    query = {"sslmode": f.sslmode.strip()} if f.sslmode.strip() else {}
    url = URL.create(
        "postgresql+psycopg",
        username=user,
        password=(f.password or None) if user else None,
        host=_require(f.host, "PostgreSQL host"),
        port=_port(f.port) or DEFAULT_PG_PORT,
        database=_require(f.database, "Database"),
        query=query,
    )
    return url.render_as_string(hide_password=False)


def target_url(opts: GuiOptions) -> str:
    """The target URL for :func:`acade2sql.convert` (contains the password: never display it;
    use :func:`display_target`)."""
    kind = opts.target_kind
    if kind == "sqlite":
        return sqlite_url(_require(opts.sqlite_path, "SQLite file"))
    if kind == "mssql":
        return mssql_url(opts.server)
    if kind == "postgresql":
        return postgresql_url(opts.server)
    if kind == "url":
        url = _require(opts.url, "Target URL")
        if "://" not in url:
            raise OptionsError(
                "Target URL must be a SQLAlchemy URL such as postgresql+psycopg://..."
            )
        return url
    raise OptionsError(f"Unknown target type {kind!r}")


def display_target(opts: GuiOptions) -> str:
    """Password-masked target for labels and logs; an error text when the form is incomplete."""
    try:
        return mask_url(target_url(opts))
    except OptionsError as exc:
        return f"(incomplete: {exc})"


def convert_kwargs(opts: GuiOptions, *, dry_run: bool) -> dict[str, Any]:
    """Keyword arguments for :func:`acade2sql.convert` (without the progress/cancel hooks).

    Sync-only options (key, no-key, delete-missing) are ignored unless the mode is ``sync``, so
    values left in the disabled fields never trigger a usage error. ``checksum`` is dropped and
    ``counts`` honoured only for a dry run, exactly as the CLI does.
    """
    source = _require(opts.source, "Source database")
    if opts.mode not in MODES:
        raise OptionsError(f"Unknown mode {opts.mode!r}")
    if opts.reader not in READER_CHOICES:
        raise OptionsError(f"Unknown reader {opts.reader!r}")
    sync = opts.mode == "sync"
    no_key = opts.no_key if sync else "error"
    if no_key not in NO_KEY_CHOICES:
        raise OptionsError(f"Unknown no-key choice {no_key!r}")
    return {
        "source": source,
        "target": target_url(opts),
        "mode": opts.mode,
        "tables": parse_names(opts.tables),
        "exclude": parse_names(opts.exclude),
        "batch_size": parse_batch_size(opts.batch_size),
        "checksum": bool(opts.checksum) and not dry_run,
        "copy_to_temp": bool(opts.copy_to_temp),
        "report_path": opts.report_path.strip() or None,
        "key": parse_keys(opts.keys) if sync else None,
        "no_key": no_key,
        "delete_missing": bool(opts.delete_missing) and sync,
        "schema": opts.schema.strip() or None,
        "dry_run": dry_run,
        "counts": bool(opts.counts) if dry_run else True,
        "reader": opts.reader,
    }


def default_sqlite_path(source: str) -> str:
    """``<source folder>/<source stem>.sqlite``; empty when there is no source yet."""
    source = (source or "").strip()
    if not source:
        return ""
    p = Path(source)
    return str(p.with_name(p.stem + ".sqlite"))
