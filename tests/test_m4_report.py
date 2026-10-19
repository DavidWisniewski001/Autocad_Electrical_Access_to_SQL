"""render_text / Report additions for append and sync (docs/M4_SPEC.md section 5)."""

from __future__ import annotations

from acade2sql.validate import Report, TableResult


def report(*tables, **kw):
    return Report("src.mdb", "sqlite:///x", kw.pop("mode", "sync"), "t0", "t1", list(tables), **kw)


def test_render_text_shows_sync_counters():
    t = TableResult(
        "COMP", 120, 125, "pass", inserted=3, updated=2, deleted=0, unchanged=115, extra_rows=5
    )
    line = report(t).render_text().splitlines()[0]
    assert line.startswith("PASS COMP source=120 target=125")
    for token in ("inserted=3", "updated=2", "deleted=0", "extra=5"):
        assert token in line, line


def test_render_text_full_mode_has_no_counters():
    line = report(TableResult("COMP", 5, 5, "pass"), mode="full").render_text().splitlines()[0]
    assert line == "PASS COMP source=5 target=5"


def test_table_result_defaults_and_json_fields():
    t = TableResult("X", 1, 1, "pass")
    for f in ("inserted", "updated", "deleted", "unchanged", "target_rows_before", "extra_rows"):
        assert getattr(t, f) is None, f
    doc = report(TableResult("X", 1, 1, "pass", inserted=1)).to_dict()
    row = doc["tables"][0]
    assert row["inserted"] == 1 and "extra_rows" in row and "target_rows_before" in row
    assert doc["dry_run"] is False


def test_planned_status_counts_as_ok():
    rep = report(TableResult("X", None, None, "planned"), mode="full", dry_run=True)
    assert rep.ok is True
    assert rep.to_dict()["dry_run"] is True
    assert rep.render_text().splitlines()[0].startswith("PLANNED X")
