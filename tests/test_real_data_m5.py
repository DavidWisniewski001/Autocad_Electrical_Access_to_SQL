# ruff: noqa: RUF001
"""M5 acceptance on the real (read-only) sources in data_files/: the pure-Python reader equals the
ODBC reader table-for-table (docs/M5_SPEC.md section 5, points 2 and 3). Never writes there."""

from __future__ import annotations

import json
import time

import pytest
from tests.conftest import DEFAULT_CAT, FOOTPRINT

from acade2sql import cli
from acade2sql.readers import open_reader
from acade2sql.validate import table_checksum

pytestmark = [pytest.mark.real_data, pytest.mark.requires_driver, pytest.mark.slow]


def _snapshot(path):
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def _dir_listing(path):
    return sorted(p.name for p in path.parent.iterdir())


def _compare_all_tables(path, expected_tables):
    before, listing = _snapshot(path), _dir_listing(path)
    schema_bad: list[str] = []
    checksum_bad: list[str] = []
    ok = 0
    timing: dict[str, tuple[float, float]] = {}
    with open_reader(path, reader="odbc") as odbc, open_reader(path, reader="pure") as pure:
        names_o, names_p = odbc.list_tables(), pure.list_tables()
        assert names_p == names_o
        assert len(names_o) == expected_tables
        for name in names_o:
            io_, ip = odbc.get_table(name), pure.get_table(name)
            same_schema = (
                ip.name == io_.name
                and ip.column_names == io_.column_names
                and [c.ordinal for c in ip.columns] == [c.ordinal for c in io_.columns]
                and ip.column_types == io_.column_types
            )
            if not same_schema:
                schema_bad.append(
                    f"{name}: odbc={list(zip(io_.column_names, io_.column_types, strict=True))} "
                    f"pure={list(zip(ip.column_names, ip.column_types, strict=True))}"
                )
                continue
            t0 = time.perf_counter()
            co = table_checksum(odbc.iter_rows(name), io_.column_types)
            t1 = time.perf_counter()
            cp = table_checksum(pure.iter_rows(name), ip.column_types)
            t2 = time.perf_counter()
            timing[name] = (t1 - t0, t2 - t1)
            if co == cp:
                ok += 1
            else:
                checksum_bad.append(name)
    assert schema_bad == [], schema_bad[:5]
    assert checksum_bad == [], f"{len(checksum_bad)} tables differ: {checksum_bad}"
    assert ok == expected_tables, f"{ok}/{expected_tables}"
    assert _snapshot(path) == before, "source file changed"
    assert _dir_listing(path) == listing, "files appeared next to the source (lock file?)"
    return timing


def test_default_cat_pure_equals_odbc_all_168_tables():
    timing = _compare_all_tables(DEFAULT_CAT, 168)
    assert "MS" in timing  # the table with the U+2013 rows


def test_footprint_pure_equals_odbc_all_117_tables():
    _compare_all_tables(FOOTPRINT, 117)


def test_default_cat_ms_has_no_x13_and_keeps_en_dash():
    with open_reader(DEFAULT_CAT, reader="pure") as r:
        info = r.get_table("MS")
        text_cols = [
            i for i, c in enumerate(info.columns) if c.access_type in ("VARCHAR", "LONGCHAR")
        ]
        dashes = 0
        for batch in r.iter_rows("MS"):
            for row in batch:
                for i in text_cols:
                    v = row[i]
                    if v is None:
                        continue
                    assert "\x13" not in v and "�" not in v, row
                    dashes += v.count("–")
    assert dashes > 0


def test_footprint_convert_pure_checksum_117_pass(capsys, tmp_path):
    before, listing = _snapshot(FOOTPRINT), _dir_listing(FOOTPRINT)
    rp = tmp_path / "rep.json"
    rc = cli.main(
        ["convert", str(FOOTPRINT), "--target", str(tmp_path / "fp.sqlite"),
         "--reader", "pure", "--checksum", "--report", str(rp)]
    )  # fmt: skip
    cap = capsys.readouterr()
    assert rc == 0, cap.err[-2000:]
    assert "117 tables: 117 passed, 0 failed, 0 errors" in cap.out
    assert cap.out.count("checksum=match") == 117
    doc = json.loads(rp.read_text("utf-8"))
    assert doc["reader"] == "pure" and doc["ok"] is True
    assert doc["summary"]["source_rows"] == doc["summary"]["target_rows"] == 261_858
    assert _snapshot(FOOTPRINT) == before
    assert _dir_listing(FOOTPRINT) == listing


def test_default_cat_convert_pure_checksum_168_pass(capsys, tmp_path):
    before, listing = _snapshot(DEFAULT_CAT), _dir_listing(DEFAULT_CAT)
    rc = cli.main(
        ["convert", str(DEFAULT_CAT), "--target", str(tmp_path / "dc.sqlite"),
         "--reader", "pure", "--checksum"]
    )  # fmt: skip
    cap = capsys.readouterr()
    assert rc == 0, cap.err[-2000:]
    assert "168 tables: 168 passed, 0 failed, 0 errors" in cap.out
    assert cap.out.count("checksum=match") == 168
    assert _snapshot(DEFAULT_CAT) == before
    assert _dir_listing(DEFAULT_CAT) == listing
