"""Conversion validation: per-table results, the run report, an order-independent checksum."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import struct
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from acade2sql.typemap import canonical_type

Status = Literal["pass", "fail", "error", "planned"]
_OK_STATUSES = frozenset({"pass", "planned"})  # "planned": --dry-run, nothing was written

_MOD = 2**256
_GUID_BODY = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
# A GUID string: bare or wrapped in ONE balanced pair of braces (both or neither).
_GUID = re.compile(rf"(?:\{{({_GUID_BODY})\}}|({_GUID_BODY}))")


@dataclass
class TableResult:
    """Outcome for one table."""

    name: str
    source_rows: int | None
    target_rows: int | None
    status: Status
    error: str | None = None
    seconds: float = 0.0
    checksum_match: bool | None = None
    warnings: list[str] = field(default_factory=list)
    # M4 (append / sync / dry-run); None when not applicable to the mode.
    target_rows_before: int | None = None
    inserted: int | None = None
    updated: int | None = None
    deleted: int | None = None
    unchanged: int | None = None
    extra_rows: int | None = None  # sync: target rows whose key is not in the source
    key: list[str] | None = None  # sync: key columns used
    actions: list[str] = field(default_factory=list)  # dry-run: e.g. ["drop", "create"]

    def counters_text(self) -> str:
        """`` inserted=3 updated=2 deleted=0 extra=5`` (only the counters that are set)."""
        parts = []
        if self.target_rows_before is not None:
            parts.append(f"target_before={self.target_rows_before}")
        for label, value in (
            ("inserted", self.inserted),
            ("updated", self.updated),
            ("deleted", self.deleted),
            ("extra", self.extra_rows),
        ):
            if value is not None:
                parts.append(f"{label}={value}")
        return "".join(" " + p for p in parts)


@dataclass
class Report:
    """Outcome of one conversion run. ``target`` is password-masked."""

    source: str
    target: str
    mode: str
    started: str
    finished: str
    tables: list[TableResult] = field(default_factory=list)
    dry_run: bool = False
    schema: str | None = None
    ddl: list[str] = field(default_factory=list)  # dry-run: the DDL that would be executed

    @property
    def ok(self) -> bool:
        return all(t.status in _OK_STATUSES for t in self.tables)

    def _counts(self) -> dict[str, int]:
        return {
            s: sum(1 for t in self.tables if t.status == s)
            for s in ("pass", "fail", "error", "planned")
        }

    def to_dict(self) -> dict[str, Any]:
        counts = self._counts()
        return {
            "source": self.source,
            "target": self.target,
            "mode": self.mode,
            "started": self.started,
            "finished": self.finished,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "schema": self.schema,
            "summary": {
                "tables": len(self.tables),
                "passed": counts["pass"],
                "failed": counts["fail"],
                "errors": counts["error"],
                "planned": counts["planned"],
                "source_rows": sum(t.source_rows or 0 for t in self.tables),
                "target_rows": sum(t.target_rows or 0 for t in self.tables),
            },
            "tables": [asdict(t) for t in self.tables],
            **({"ddl": list(self.ddl)} if self.dry_run else {}),
        }

    def write_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def render_text(self) -> str:
        lines = []
        for t in self.tables:
            source = "-" if t.source_rows is None else str(t.source_rows)
            target = "-" if t.target_rows is None else str(t.target_rows)
            line = f"{t.status.upper()} {t.name} source={source} target={target}"
            line += t.counters_text()
            if self.dry_run and self.mode == "sync":  # the key is in the JSON report otherwise
                line += " key=" + (",".join(t.key) if t.key else "-")
            if t.actions:
                line += " actions=" + ",".join(t.actions)
            if t.checksum_match is not None:
                line += " checksum=" + ("match" if t.checksum_match else "MISMATCH")
            if t.warnings:
                line += f" warnings={len(t.warnings)}"
            if t.error:
                line += f" error={t.error}"
            lines.append(line)
        c = self._counts()
        summary = (
            f"{len(self.tables)} tables: {c['pass']} passed, {c['fail']} failed, "
            f"{c['error']} errors"
        )
        if c["planned"]:
            summary += f", {c['planned']} planned"
        lines.append(summary)
        if self.dry_run:
            lines.append(
                f"dry run: mode={self.mode} target={self.target} "
                f"schema={self.schema or '-'} (nothing was written)"
            )
            for t in self.tables:
                for w in t.warnings:
                    lines.append(f"warning: {t.name}: {w}")
            lines.append("-- DDL")
            lines.extend(stmt.rstrip() + ";" for stmt in self.ddl)
        return "\n".join(lines)


def utc_now() -> str:
    """Current time as ISO-8601 UTC."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


Kind = Literal["GUID", "REAL"] | None


def value_kind(access_type: str | None) -> Kind:
    """The normalisation kind of a column: ``"GUID"``, ``"REAL"`` (Single) or None (exact)."""
    if access_type is None:
        return None
    canonical = canonical_type(access_type)
    if canonical == "GUID":
        return "GUID"
    if canonical == "REAL":
        return "REAL"
    return None


def column_kinds(column_types: Sequence[str] | None, width: int) -> tuple[Kind, ...]:
    """Per-column kinds for rows of ``width`` values (all None without ``column_types``)."""
    if column_types is None:
        return (None,) * width
    if len(column_types) != width:
        raise ValueError(f"{len(column_types)} column types for rows of {width} values")
    return tuple(value_kind(t) for t in column_types)


def normalize_value(value: Any, access_type: str | None = None) -> bytes:
    """Canonical, type-tagged bytes of one value: equal bytes <=> equal for validation.

    ``access_type`` (``ColumnInfo.access_type``) enables the column-specific folding: GUID
    columns compare upper-case without one balanced pair of braces, REAL (Single) columns compare
    floats rounded to float32. Without it (and for every other type) strings compare exactly.
    """
    return _norm(value, value_kind(access_type))


def _float32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:  # outside the float32 range: cannot come from a Single column
        return value


def _norm(value: Any, kind: Kind = None) -> bytes:
    """Type-tagged canonical bytes for one value (see M3_SPEC section 3, DECISIONS #25)."""
    if value is None:
        return b"N"
    if isinstance(value, str):
        if kind == "GUID":
            guid = _GUID.fullmatch(value)
            if guid:
                return b"S" + (guid.group(1) or guid.group(2)).upper().encode("ascii")
        return b"S" + value.encode("utf-8")
    if isinstance(value, bool):
        return b"B1" if value else b"B0"
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    if isinstance(value, float):
        if kind == "REAL":
            value = _float32(value)
        return b"F" + repr(value).encode("ascii")
    if isinstance(value, Decimal):
        return b"D" + str(value.normalize()).encode("ascii")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return b"T" + value.isoformat().encode("ascii")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"X" + bytes(value).hex().encode("ascii")
    if isinstance(value, uuid.UUID) and kind == "GUID":
        return b"S" + str(value).upper().encode("ascii")
    return b"O" + repr(value).encode("utf-8")


def same_row(a: Sequence[Any], b: Sequence[Any], column_types: Sequence[str] | None = None) -> bool:
    """True when two rows are equal for validation (``''`` != NULL, padding kept).

    ``column_types``: the ``access_type`` of each column, in column order (see
    :func:`normalize_value`); None compares every string exactly.
    """
    if len(a) != len(b):
        return False
    if tuple(a) == tuple(b) and all(type(x) is type(y) for x, y in zip(a, b, strict=True)):
        return True
    kinds = column_kinds(column_types, len(a))
    return all(_norm(x, k) == _norm(y, k) for x, y, k in zip(a, b, kinds, strict=True))


def _row_digest(row: tuple, kinds: tuple[Kind, ...]) -> int:
    h = hashlib.sha256()
    for value, kind in zip(row, kinds, strict=True):
        part = _norm(value, kind)
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return int.from_bytes(h.digest(), "big")


def table_checksum(rows: Iterable[list[tuple]], column_types: Sequence[str] | None = None) -> str:
    """Order-independent checksum of batches of rows: sum of per-row SHA-256 mod 2**256, hex.

    ``column_types``: the ``access_type`` of each column, in column order (GUID and REAL
    folding, see :func:`normalize_value`); None (legacy) normalises without column folding.
    """
    total = 0
    kinds: tuple[Kind, ...] | None = None
    for batch in rows:
        for row in batch:
            if kinds is None or len(kinds) != len(row):
                kinds = column_kinds(column_types, len(row))
            total = (total + _row_digest(row, kinds)) % _MOD
    return f"{total:064x}"
