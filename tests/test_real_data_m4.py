"""M4 acceptance on the real (read-only) sources in data_files/. Never writes there."""

from __future__ import annotations

import json
import re

import pytest
from tests.conftest import DEFAULT_CAT, FOOTPRINT

from acade2sql import cli, converter

pytestmark = [pytest.mark.real_data, pytest.mark.requires_driver]

MSSQL = "mssql+pyodbc://u:s3cr3tP4ss@h/db?driver=ODBC+Driver+18+for+SQL+Server"
PG = "postgresql+psycopg://u:s3cr3tP4ss@h/db"


@pytest.mark.slow
def test_footprint_full_then_sync_is_noop(tmp_path):
    url = "sqlite:///" + (tmp_path / "fp.sqlite").as_posix()
    assert converter.convert(FOOTPRINT, url).ok
    rep = converter.convert(FOOTPRINT, url, mode="sync", checksum=True)
    assert rep.ok, rep.render_text()
    assert len(rep.tables) == 117
    for t in rep.tables:
        assert t.status == "pass", t
        assert (t.inserted, t.updated, t.deleted) == (0, 0, 0), t.name
        assert t.unchanged == t.source_rows == t.target_rows, t.name
        assert t.checksum_match is True, t.name
    assert sum(t.source_rows for t in rep.tables) == 261_858


@pytest.mark.slow
def test_footprint_append_fresh_equals_full_and_second_append_errors(tmp_path):
    full = "sqlite:///" + (tmp_path / "full.sqlite").as_posix()
    app = "sqlite:///" + (tmp_path / "app.sqlite").as_posix()
    assert converter.convert(FOOTPRINT, full).ok
    rep = converter.convert(FOOTPRINT, app, mode="append")
    assert rep.ok, rep.render_text()
    assert sum(t.target_rows for t in rep.tables) == 261_858
    rep2 = converter.convert(FOOTPRINT, app, mode="append")
    errs = [t for t in rep2.tables if t.status == "error"]
    assert errs and all(t.source_rows for t in errs)
    assert {t.name for t in rep2.tables if t.status == "pass"} <= {
        t.name for t in rep2.tables if not t.source_rows
    }
    rep3 = converter.convert(FOOTPRINT, app, mode="sync", checksum=True)
    assert rep3.ok, rep3.render_text()  # target still equals the source: nothing was appended
    assert all((t.inserted, t.updated, t.deleted) == (0, 0, 0) for t in rep3.tables)


@pytest.mark.parametrize(
    ("url", "needle"), [(MSSQL, "NVARCHAR(max)"), (PG, "TEXT")], ids=["mssql", "pg"]
)
def test_default_cat_dry_run(capsys, tmp_path, url, needle):
    rp = tmp_path / "r.json"
    rc = cli.main(
        ["convert", str(DEFAULT_CAT), "--target", url, "--dry-run", "--no-counts",
         "--report", str(rp)]
    )  # fmt: skip
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    assert needle in cap.out
    assert len(re.findall(r"CREATE TABLE", cap.out)) == 168
    assert "s3cr3tP4ss" not in cap.out + cap.err + rp.read_text("utf-8")
    doc = json.loads(rp.read_text("utf-8"))
    assert doc["dry_run"] is True and doc["ok"] is True
    assert len(doc["tables"]) == 168
