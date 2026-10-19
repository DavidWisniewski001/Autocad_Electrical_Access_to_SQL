"""Conversion validation: per-table results, the run report, an order-independent checksum."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

Status = Literal["pass", "fail", "error"]

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


@dataclass
class Report:
    """Outcome of one conversion run. ``target`` is password-masked."""

    source: str
    target: str
    mode: str
    started: str
    finished: str
    tables: list[TableResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(t.status == "pass" for t in self.tables)

    def _counts(self) -> dict[str, int]:
        return {s: sum(1 for t in self.tables if t.status == s) for s in ("pass", "fail", "error")}

    def to_dict(self) -> dict[str, Any]:
        counts = self._counts()
        return {
            "source": self.source,
            "target": self.target,
            "mode": self.mode,
            "started": self.started,
            "finished": self.finished,
            "ok": self.ok,
            "summary": {
                "tables": len(self.tables),
                "passed": counts["pass"],
                "failed": counts["fail"],
                "errors": counts["error"],
                "source_rows": sum(t.source_rows or 0 for t in self.tables),
                "target_rows": sum(t.target_rows or 0 for t in self.tables),
            },
            "tables": [asdict(t) for t in self.tables],
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
            if t.checksum_match is not None:
                line += " checksum=" + ("match" if t.checksum_match else "MISMATCH")
            if t.warnings:
                line += f" warnings={len(t.warnings)}"
            if t.error:
                line += f" error={t.error}"
            lines.append(line)
        c = self._counts()
        lines.append(
            f"{len(self.tables)} tables: {c['pass']} passed, {c['fail']} failed, "
            f"{c['error']} errors"
        )
        return "\n".join(lines)


def utc_now() -> str:
    """Current time as ISO-8601 UTC."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _norm(value: Any) -> bytes:
    """Type-tagged canonical bytes for one value (see M3_SPEC section 3)."""
    if value is None:
        return b"N"
    if isinstance(value, str):
        guid = _GUID.fullmatch(value)
        if guid:
            return b"S" + (guid.group(1) or guid.group(2)).upper().encode("ascii")
        return b"S" + value.encode("utf-8")
    if isinstance(value, bool):
        return b"B1" if value else b"B0"
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"F" + repr(value).encode("ascii")
    if isinstance(value, Decimal):
        return b"D" + str(value.normalize()).encode("ascii")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return b"T" + value.isoformat().encode("ascii")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"X" + bytes(value).hex().encode("ascii")
    return b"O" + repr(value).encode("utf-8")


def _row_digest(row: tuple) -> int:
    h = hashlib.sha256()
    for value in row:
        part = _norm(value)
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return int.from_bytes(h.digest(), "big")


def table_checksum(rows: Iterable[list[tuple]]) -> str:
    """Order-independent checksum of batches of rows: sum of per-row SHA-256 mod 2**256, hex."""
    total = 0
    for batch in rows:
        for row in batch:
            total = (total + _row_digest(row)) % _MOD
    return f"{total:064x}"
