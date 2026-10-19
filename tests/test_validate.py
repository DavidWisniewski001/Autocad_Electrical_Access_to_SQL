"""Unit tests for acade2sql.validate (spec M3 section 3). No Access driver needed."""

from __future__ import annotations

import datetime as dt
import json
import random
import re
from decimal import Decimal

import pytest

validate = pytest.importorskip("acade2sql.validate")
Report = validate.Report
TableResult = validate.TableResult
table_checksum = validate.table_checksum


def cs(*rows, batch=None):
    rows = list(rows)
    if batch is None:
        return table_checksum([rows])
    return table_checksum([rows[i : i + batch] for i in range(0, len(rows), batch)])


# ---- table_checksum -----------------------------------------------------------


def test_checksum_is_hex_string():
    c = cs((1, "a"))
    assert isinstance(c, str) and re.fullmatch(r"[0-9a-f]+", c)


def test_checksum_empty_is_stable_and_differs_from_nonempty():
    assert table_checksum([]) == table_checksum([[]]) == cs()
    assert cs() != cs((1,))


def test_checksum_order_independent():
    rows = [(i, f"v{i}", None if i % 3 == 0 else i * 1.5) for i in range(200)]
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert cs(*rows) == cs(*shuffled)


def test_checksum_batching_independent():
    rows = [(i, f"v{i}") for i in range(101)]
    assert cs(*rows, batch=1) == cs(*rows, batch=7) == cs(*rows)


def test_checksum_multiset_duplicates_matter():
    a = ("x", 1)
    assert cs(a, a) != cs(a)
    assert cs(a, a, ("y", 2)) != cs(a, ("y", 2))


def test_checksum_column_order_and_value_matter():
    assert cs((1, 2)) != cs((2, 1))
    assert cs(("a", "b")) != cs(("a", "c"))
    assert cs((1, 2)) != cs((1, 2, None))
    assert cs((1,), (2,)) != cs((1, 2))


def test_null_differs_from_empty_string():
    assert cs((None,)) != cs(("",))
    assert cs((None, "a")) != cs(("", "a"))


def test_whitespace_not_stripped():
    assert cs(("a ",)) != cs(("a",))
    assert cs((" a",)) != cs(("a",))


def test_bool_differs_from_int():
    assert cs((True,)) != cs((1,))
    assert cs((False,)) != cs((0,))
    assert cs((True,)) != cs((False,))


def test_decimal_normalised():
    assert cs((Decimal("1.10"),)) == cs((Decimal("1.1"),))
    assert cs((Decimal("100"),)) == cs((Decimal("1E+2"),))
    assert cs((Decimal("1.1"),)) != cs((Decimal("1.2"),))
    assert cs((Decimal("-0.0001"),)) != cs((Decimal("0.0001"),))


def test_int_and_float_and_datetime():
    assert cs((5,)) == cs((5,))
    assert cs((1.5,)) == cs((1.5,))
    assert cs((1.5,)) != cs((1.25,))
    d = dt.datetime(2020, 1, 2, 3, 4, 5)
    assert cs((d,)) == cs((dt.datetime(2020, 1, 2, 3, 4, 5),))
    assert cs((d,)) != cs((d + dt.timedelta(seconds=1),))


def test_bytes():
    assert cs((b"\x00\x01",)) == cs((b"\x00\x01",))
    assert cs((b"\x00",)) != cs((b"",))
    assert cs((b"\x00",)) != cs((b"\x01",))
    assert cs((b"\x00",)) != cs((None,))


def test_guid_normalised():
    g = "12345678-1234-1234-1234-123456789abc"
    assert cs((g,)) == cs((g.upper(),))
    assert cs(("{" + g.upper() + "}",)) == cs((g,))
    assert cs(("{" + g + "}",)) == cs((g.upper(),))
    other = "12345678-1234-1234-1234-123456789abd"
    assert cs((g,)) != cs((other,))


def test_non_guid_strings_case_sensitive():
    assert cs(("abc",)) != cs(("ABC",))
    assert cs(("{abc}",)) != cs(("abc",))


def test_unicode():
    assert cs(("µΩ°",)) == cs(("µΩ°",))
    assert cs(("a\r\nb",)) != cs(("a\nb",))
    assert cs(("\U0001f600",)) != cs(("?",))


# ---- Report ------------------------------------------------------------------------


def make_report(**kw):
    tables = kw.pop(
        "tables",
        [
            TableResult("A", 3, 3, "pass", seconds=0.5, checksum_match=True),
            TableResult("B", 3, 2, "fail"),
            TableResult("C", 4, None, "error", error="boom: it broke"),
        ],
    )
    return Report(
        source=kw.get("source", "C:/x/src.mdb"),
        target=kw.get("target", "sqlite:///C:/x/out.sqlite"),
        mode="full",
        started="2026-01-01T00:00:00+00:00",
        finished="2026-01-01T00:00:05+00:00",
        tables=tables,
    )


def test_table_result_defaults():
    r = TableResult("T", 1, 1, "pass")
    assert r.error is None and r.seconds == 0.0 and r.checksum_match is None


def test_ok_all_pass():
    rep = make_report(tables=[TableResult("A", 1, 1, "pass"), TableResult("B", 0, 0, "pass")])
    assert rep.ok is True


def test_ok_empty_list():
    assert make_report(tables=[]).ok is True


@pytest.mark.parametrize("bad", ["fail", "error"])
def test_ok_false_if_any_not_pass(bad):
    rep = make_report(tables=[TableResult("A", 1, 1, "pass"), TableResult("B", 1, 0, bad)])
    assert rep.ok is False


def test_render_text_lines_and_summary():
    text = make_report().render_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].startswith("PASS A") and "source=3" in lines[0] and "target=3" in lines[0]
    assert lines[1].startswith("FAIL B") and "source=3" in lines[1] and "target=2" in lines[1]
    assert lines[2].startswith("ERROR C") and "source=4" in lines[2]
    assert lines[-1] == "3 tables: 1 passed, 1 failed, 1 errors"


def test_render_text_pass_format_exact():
    rep = make_report(tables=[TableResult("COMP", 5, 5, "pass")])
    lines = rep.render_text().splitlines()
    assert re.fullmatch(r"PASS\s+COMP\s+source=5\s+target=5", lines[0].strip())
    assert lines[-1] == "1 tables: 1 passed, 0 failed, 0 errors"


def test_render_text_empty():
    assert make_report(tables=[]).render_text().splitlines()[-1] == (
        "0 tables: 0 passed, 0 failed, 0 errors"
    )


def test_to_dict_is_json_serialisable():
    d = make_report().to_dict()
    json.dumps(d)
    for key in ("source", "target", "mode", "started", "finished", "tables"):
        assert key in d
    assert d["mode"] == "full"
    assert [t["name"] for t in d["tables"]] == ["A", "B", "C"]
    assert [t["status"] for t in d["tables"]] == ["pass", "fail", "error"]
    assert d["tables"][2]["error"] == "boom: it broke"
    assert d["tables"][0]["checksum_match"] is True
    assert d["tables"][2]["target_rows"] is None
    assert d["tables"][1]["source_rows"] == 3 and d["tables"][1]["target_rows"] == 2


def test_write_json(tmp_path):
    p = tmp_path / "r.json"
    make_report().write_json(p)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["source"] == "C:/x/src.mdb"
    assert len(doc["tables"]) == 3


def test_write_json_accepts_str_path(tmp_path):
    p = tmp_path / "r2.json"
    make_report().write_json(str(p))
    json.loads(p.read_text(encoding="utf-8"))


def test_report_does_not_leak_password_when_given_masked_target():
    from sqlalchemy.engine import make_url

    masked = make_url("postgresql://user:s3cr3t@host/db").render_as_string(hide_password=True)
    rep = make_report(target=masked)
    assert "s3cr3t" not in rep.render_text()
    assert "s3cr3t" not in json.dumps(rep.to_dict())
