"""Sync key selection: ``--key COL[,COL...]`` (global) and ``--key TABLE=COL[,COL...]``."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from acade2sql.errors import SyncKeyError, UsageError
from acade2sql.schema import TableInfo

log = logging.getLogger(__name__)

NO_KEY_CHOICES = ("error", "replace")
NO_KEY_MESSAGE = "no key for sync; use --key or --no-key replace"


@dataclass(frozen=True)
class KeySpec:
    """Parsed ``--key`` options. Table names are stored case-folded."""

    global_columns: tuple[str, ...] = ()
    per_table: dict[str, tuple[str, ...]] = field(default_factory=dict)
    table_names: dict[str, str] = field(default_factory=dict)  # folded -> as given

    def __bool__(self) -> bool:
        return bool(self.global_columns or self.per_table)


def _columns(text: str, spec: str) -> tuple[str, ...]:
    cols = tuple(c.strip() for c in text.split(","))
    if not cols or any(not c for c in cols):
        raise UsageError(f"invalid --key {spec!r}: expected COL[,COL...] or TABLE=COL[,COL...]")
    return cols


def parse_key_specs(specs: str | Sequence[str] | None) -> KeySpec:
    """Parse ``--key`` values; a later spec for the same table (or global) replaces the earlier."""
    if specs is None:
        return KeySpec()
    if isinstance(specs, str):
        specs = [specs]
    global_columns: tuple[str, ...] = ()
    per_table: dict[str, tuple[str, ...]] = {}
    names: dict[str, str] = {}
    for spec in specs:
        if "=" in spec:
            table, _, cols = spec.partition("=")
            table = table.strip()
            if not table:
                raise UsageError(f"invalid --key {spec!r}: empty table name")
            per_table[table.casefold()] = _columns(cols, spec)
            names[table.casefold()] = table
        else:
            global_columns = _columns(spec, spec)
    return KeySpec(global_columns, per_table, names)


def _resolve(info: TableInfo, name: str) -> str | None:
    try:
        return info.column(name).name  # exact, then case-insensitive
    except KeyError:
        return None


def key_for(info: TableInfo, spec: KeySpec) -> tuple[str, ...] | None:
    """The sync key of ``info``: table-specific ``--key`` > global ``--key`` (when the table has
    all those columns) > the table's primary key > ``None``.

    Raises :class:`SyncKeyError` when a table-specific key names a column the table lacks.
    """
    cols = spec.per_table.get(info.name.casefold())
    if cols is not None:
        resolved = [_resolve(info, c) for c in cols]
        missing = [c for c, r in zip(cols, resolved, strict=True) if r is None]
        if missing:
            raise SyncKeyError(
                f"key column(s) {', '.join(missing)} not found in source table {info.name}"
            )
        return tuple(r for r in resolved if r is not None)
    if spec.global_columns:
        resolved = [_resolve(info, c) for c in spec.global_columns]
        if all(r is not None for r in resolved):
            return tuple(r for r in resolved if r is not None)
    return tuple(info.primary_key) or None


def unmatched_tables(spec: KeySpec, table_names: Sequence[str]) -> list[str]:
    """Table names used in ``--key TABLE=...`` that are not among ``table_names``."""
    present = {n.casefold() for n in table_names}
    return [spec.table_names[t] for t in spec.per_table if t not in present]
