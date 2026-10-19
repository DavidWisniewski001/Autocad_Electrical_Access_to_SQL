from __future__ import annotations

import json
import re
import shutil

import pytest
from tests.fixtures.build_access import MANIFEST

cli = pytest.importorskip("acade2sql.cli")

pytestmark = pytest.mark.requires_driver

TOTAL_ROWS = sum(m["count"] for m in MANIFEST.values())


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_inspect_text_summary(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db))
    assert rc == 0, err
    assert f"{len(MANIFEST)} tables, {TOTAL_ROWS} rows" in out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for name in MANIFEST:
        assert any(name in ln for ln in lines), name
    assert "Traceback" not in err


def test_inspect_no_counts_still_lists_tables(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--no-counts")
    assert rc == 0, err
    for name in MANIFEST:
        assert name in out


def test_inspect_table_detail(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--table", "TERMS")
    assert rc == 0, err
    assert "TERMS" in out
    for col in ("HDL", "TERM", "WIRENO", "PIN"):
        assert col in out
    assert "discovered" in out
    assert "ALLEN" not in out
    assert not re.search(r"\bWIRENO\s+\(?\d+", out.split("TERMS")[0])


def test_inspect_multiple_tables(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--table", "COMP", "--table", "NETS")
    assert rc == 0, err
    assert "COMP" in out and "NETS" in out and "WFRM2ALL" not in out


def test_inspect_unknown_table(capsys, mdb_db):
    rc, _out, err = run(capsys, "inspect", str(mdb_db), "--table", "NOPE")
    assert rc == 1
    assert "NOPE" in err
    assert "Traceback" not in err


def test_inspect_json(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--json")
    assert rc == 0, err
    doc = json.loads(out)  # stdout must be pure JSON
    assert "source" in doc and str(mdb_db.name) in doc["source"]
    assert len(doc["tables"]) == len(MANIFEST)
    by_name = {t["name"]: t for t in doc["tables"]}
    assert set(by_name) == set(MANIFEST)
    for name, m in MANIFEST.items():
        t = by_name[name]
        assert t["row_count"] == m["count"], name
        assert t["primary_key"] == list(m["pk"])
        assert t["primary_key_source"] == m["pk_source"]
        assert [c["name"] for c in t["columns"]] == m["columns"]


def test_inspect_json_table_filter(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--json", "--table", "comp")
    assert rc == 0, err
    doc = json.loads(out)
    assert [t["name"] for t in doc["tables"]] == ["COMP"]


def test_verbose_logs_go_to_stderr_only(capsys, mdb_db):
    rc, out, err = run(capsys, "--verbose", "inspect", str(mdb_db), "--json")
    assert rc == 0, err
    json.loads(out)  # still pure JSON on stdout


def test_inspect_json_no_counts(capsys, mdb_db):
    rc, out, err = run(capsys, "inspect", str(mdb_db), "--json", "--no-counts")
    assert rc == 0, err
    json.loads(out)


def test_convert_not_implemented(capsys, mdb_db, tmp_path):
    rc, out, err = run(capsys, "convert", str(mdb_db), str(tmp_path / "o.sqlite"))
    assert rc == 2
    assert "not implemented yet" in (out + err)
    assert not (tmp_path / "o.sqlite").exists()


def test_convert_bare_not_implemented(capsys, mdb_db):
    try:
        rc, _out, _err = run(capsys, "convert", str(mdb_db))
    except SystemExit as e:  # argparse usage error is also exit 2
        assert e.code == 2
    else:
        assert rc == 2


def test_missing_file(capsys, tmp_path):
    rc, out, err = run(capsys, "inspect", str(tmp_path / "missing.mdb"))
    assert rc == 1
    assert "missing.mdb" in err
    assert "Traceback" not in err and "Traceback" not in out
    assert out.strip() == ""


def test_wrong_suffix(capsys, tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    rc, _out, err = run(capsys, "inspect", str(p))
    assert rc == 1
    assert "Traceback" not in err
    assert err.strip()


def _usage_exit_code(argv):
    try:
        return cli.main(argv)
    except SystemExit as e:  # argparse-style usage error
        return e.code


def test_no_args_is_usage_error(capsys):
    assert _usage_exit_code([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_inspect_missing_path_arg_is_usage_error(capsys):
    assert _usage_exit_code(["inspect"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_inspect_does_not_modify_source(capsys, mdb_db, tmp_path):
    copy = tmp_path / "c.mdb"
    shutil.copy2(mdb_db, copy)
    before = copy.stat()
    rc, _, err = run(capsys, "inspect", str(copy))
    assert rc == 0, err
    after = copy.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert not [p for p in tmp_path.iterdir() if p.suffix in (".ldb", ".laccdb")]


def test_missing_driver_gives_actionable_error(capsys, mdb_db, monkeypatch):
    import pyodbc

    from acade2sql import diagnostics

    monkeypatch.setattr(pyodbc, "drivers", lambda: [])
    monkeypatch.setattr(diagnostics, "_other_bitness_has_driver", lambda: False)
    rc, _out, err = run(capsys, "inspect", str(mdb_db))
    assert rc == 1
    assert "Microsoft Access Database Engine" in err
    assert "-bit" in err
    assert "Traceback" not in err
