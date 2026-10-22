"""Tk-free text for the results table and the status line."""

from __future__ import annotations

from acade2sql.validate import Report, TableResult

RESULT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    # (id, heading, width in pixels)
    ("table", "Table", 200),
    ("status", "Status", 90),
    ("source", "Source rows", 90),
    ("target", "Target rows", 90),
    ("changes", "Changes", 170),
    ("checksum", "Checksum", 95),
    ("seconds", "Seconds", 65),
    ("warnings", "Warnings", 70),
    ("error", "Error", 320),
)


def _num(value: int | None) -> str:
    return "" if value is None else str(value)


def changes_text(res: TableResult) -> str:
    """``+3 ~2 -0`` style counters (inserted/updated/deleted) or the dry-run actions."""
    if res.actions:
        return ",".join(res.actions)
    parts = []
    for label, value in (("ins", res.inserted), ("upd", res.updated), ("del", res.deleted)):
        if value is not None:
            parts.append(f"{label}={value}")
    if res.extra_rows is not None:
        parts.append(f"extra={res.extra_rows}")
    return " ".join(parts)


def result_row(res: TableResult) -> tuple[str, ...]:
    """Values for one row of the results table, in :data:`RESULT_COLUMNS` order."""
    checksum = "" if res.checksum_match is None else ("match" if res.checksum_match else "MISMATCH")
    error = (res.error or "").replace("\n", " ")
    return (
        res.name,
        res.status.upper(),
        _num(res.source_rows),
        _num(res.target_rows),
        changes_text(res),
        checksum,
        f"{res.seconds:.1f}" if res.seconds else "",
        str(len(res.warnings)) if res.warnings else "",
        error,
    )


def running_row(name: str) -> tuple[str, ...]:
    return (name, "RUNNING", *([""] * (len(RESULT_COLUMNS) - 2)))


def details_text(res: TableResult) -> str:
    """Full detail of one table for the details dialog."""
    lines = [f"Table: {res.name}", f"Status: {res.status.upper()}"]
    lines.append(f"Source rows: {_num(res.source_rows) or '-'}")
    lines.append(f"Target rows: {_num(res.target_rows) or '-'}")
    if res.target_rows_before is not None:
        lines.append(f"Target rows before: {res.target_rows_before}")
    if changes_text(res):
        lines.append(f"Changes: {changes_text(res)}")
    if res.key:
        lines.append(f"Key: {', '.join(res.key)}")
    if res.checksum_match is not None:
        lines.append(f"Checksum: {'match' if res.checksum_match else 'MISMATCH'}")
    if res.error:
        lines.append(f"Error: {res.error}")
    if res.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in res.warnings)
    return "\n".join(lines)


def summary_text(report: Report, *, cancelled: bool = False) -> str:
    """One status line for a finished run."""
    d = report.to_dict()["summary"]
    kind = "Dry run" if report.dry_run else "Conversion"
    text = (
        f"{kind} {'OK' if report.ok else 'finished with problems'}: {d['tables']} tables, "
        f"{d['passed']} passed, {d['failed']} failed, {d['errors']} errors"
    )
    if report.dry_run:
        text += f", {d['planned']} planned"
    else:
        text += f", {d['target_rows']} target rows"
    if cancelled:
        text += " (cancelled)"
    return text
