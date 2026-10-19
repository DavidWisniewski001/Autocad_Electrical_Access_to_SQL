"""`acade2sql convert` CLI tests (spec M3 section 5)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from tests.fixtures.build_access import MANIFEST

cli = pytest.importorskip("acade2sql.cli")

pytestmark = pytest.mark.requires_driver

N = len(MANIFEST)
PASS_LINE = f"{N} tables: {N} passed, 0 failed, 0 errors"


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def _usage_exit_code(argv):
    try:
        return cli.main(argv)
    except SystemExit as e:
        return e.code


def _url(p):
    return "sqlite:///" + p.as_posix()


def _tables_in(p):
    con = sqlite3.connect(p)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_convert_ok(capsys, mdb_db, tmp_path):
    out_db = tmp_path / "o.sqlite"
    rc, out, err = run(capsys, "convert", str(mdb_db), "--target", str(out_db), "--checksum")
    assert rc == 0, err
    assert out.splitlines()[-1].strip() == PASS_LINE
    assert out.count("PASS") >= N
    assert "FAIL" not in out and "ERROR" not in out
    assert _tables_in(out_db) == set(MANIFEST)
    assert "Traceback" not in err


def test_convert_url_target(capsys, mdb_db, tmp_path):
    out_db = tmp_path / "u.sqlite"
    rc, out, err = run(capsys, "convert", str(mdb_db), "--target", _url(out_db))
    assert rc == 0, err
    assert PASS_LINE in out
    assert out_db.exists()


def test_convert_tables_exclude_batch_size(capsys, mdb_db, tmp_path):
    out_db = tmp_path / "t.sqlite"
    rc, out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(out_db), "--batch-size", "2",
        "--tables", "comp", "TERMS", "NETS", "--exclude", "nets", "--mode", "full",
    )  # fmt: skip
    assert rc == 0, err
    assert out.splitlines()[-1].strip() == "2 tables: 2 passed, 0 failed, 0 errors"
    assert _tables_in(out_db) == {"COMP", "TERMS"}


def test_convert_copy_to_temp(capsys, mdb_db, tmp_path):
    rc, out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "c.sqlite"),
        "--tables", "COMP", "--copy-to-temp",
    )  # fmt: skip
    assert rc == 0, err
    assert "1 tables: 1 passed, 0 failed, 0 errors" in out


def test_convert_unknown_table_exit_1_nothing_written(capsys, mdb_db, tmp_path):
    out_db = tmp_path / "no.sqlite"
    rc, _out, err = run(capsys, "convert", str(mdb_db), "--target", str(out_db), "--tables", "NOPE")
    assert rc == 1
    assert "NOPE" in err and "Traceback" not in err
    assert not out_db.exists()


def test_convert_unknown_table_among_valid(capsys, mdb_db, tmp_path):
    out_db = tmp_path / "no2.sqlite"
    rc, out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(out_db), "--tables", "COMP", "NOPE"
    )
    assert rc == 1
    assert "NOPE" in err
    assert out.strip() == ""
    assert not out_db.exists()


def test_convert_missing_source(capsys, tmp_path):
    out_db = tmp_path / "ms.sqlite"
    rc, out, err = run(capsys, "convert", str(tmp_path / "missing.mdb"), "--target", str(out_db))
    assert rc == 1
    assert "missing.mdb" in err
    assert "Traceback" not in err and "Traceback" not in out
    assert not out_db.exists()


def test_convert_missing_source_arg_is_usage_error(capsys):
    assert _usage_exit_code(["convert"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_convert_bad_mode_is_usage_error(capsys, mdb_db):
    assert _usage_exit_code(["convert", str(mdb_db), "--mode", "bogus"]) == 2
    capsys.readouterr()


def test_convert_missing_driver_actionable(capsys, mdb_db, tmp_path, monkeypatch):
    import pyodbc

    from acade2sql import diagnostics

    monkeypatch.setattr(pyodbc, "drivers", lambda: [])
    monkeypatch.setattr(diagnostics, "_other_bitness_has_driver", lambda: False)
    out_db = tmp_path / "d.sqlite"
    rc, _out, err = run(capsys, "convert", str(mdb_db), "--target", str(out_db))
    assert rc == 1
    assert "Microsoft Access Database Engine" in err
    assert "Traceback" not in err
    assert not out_db.exists()


def test_convert_report_json(capsys, mdb_db, tmp_path):
    rp = tmp_path / "rep.json"
    rc, _out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "r.sqlite"),
        "--checksum", "--report", str(rp),
    )  # fmt: skip
    assert rc == 0, err
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert len(doc["tables"]) == N
    assert all(t["status"] == "pass" and t["checksum_match"] is True for t in doc["tables"])
    assert {t["name"] for t in doc["tables"]} == set(MANIFEST)


def test_convert_honours_env_target(capsys, mdb_db, tmp_path, monkeypatch):
    out_db = tmp_path / "from_env.sqlite"
    monkeypatch.setenv("ACADE2SQL_TARGET", _url(out_db))
    rc, _out, err = run(capsys, "convert", str(mdb_db), "--tables", "COMP")
    assert rc == 0, err
    assert out_db.exists()
    assert _tables_in(out_db) == {"COMP"}


def test_convert_explicit_target_beats_env(capsys, mdb_db, tmp_path, monkeypatch):
    env_db, arg_db = tmp_path / "env.sqlite", tmp_path / "arg.sqlite"
    monkeypatch.setenv("ACADE2SQL_TARGET", _url(env_db))
    rc, _out, err = run(capsys, "convert", str(mdb_db), "--target", str(arg_db), "--tables", "COMP")
    assert rc == 0, err
    assert arg_db.exists() and not env_db.exists()


def test_convert_default_target_in_cwd(capsys, mdb_db, tmp_path, monkeypatch):
    monkeypatch.delenv("ACADE2SQL_TARGET", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    rc, _out, err = run(capsys, "convert", str(mdb_db), "--tables", "COMP")
    assert rc == 0, err
    assert (work / f"{mdb_db.stem}.sqlite").exists()


def test_convert_injected_failure_exit_3(capsys, mdb_db, tmp_path, monkeypatch):
    from acade2sql.errors import WriteError
    from acade2sql.writers.sqlalchemy_writer import SqlAlchemyWriter

    orig = SqlAlchemyWriter.write_batches

    def fake(self, info, batches):
        if info.name == "TERMS":
            raise WriteError("TERMS", RuntimeError("injected boom"))
        return orig(self, info, batches)

    monkeypatch.setattr(SqlAlchemyWriter, "write_batches", fake)
    out_db = tmp_path / "f.sqlite"
    rp = tmp_path / "f.json"
    rc, out, _err = run(
        capsys, "convert", str(mdb_db), "--target", str(out_db), "--report", str(rp),
        "--tables", "COMP", "TERMS", "NETS",
    )  # fmt: skip
    assert rc == 3
    lines = out.splitlines()
    assert any(ln.startswith("ERROR") and "TERMS" in ln for ln in lines)
    assert lines[-1].strip() == "3 tables: 2 passed, 0 failed, 1 errors"
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert {t["name"]: t["status"] for t in doc["tables"]} == {
        "COMP": "pass", "TERMS": "error", "NETS": "pass",
    }  # fmt: skip


def test_convert_verbose_logs_to_stderr_only(capsys, mdb_db, tmp_path):
    rc, out, err = run(
        capsys, "--verbose", "convert", str(mdb_db), "--target", str(tmp_path / "v.sqlite"),
        "--tables", "COMP",
    )  # fmt: skip
    assert rc == 0, err
    assert out.splitlines()[-1].strip() == "1 tables: 1 passed, 0 failed, 0 errors"
    assert "COMP" in err  # one INFO/DEBUG log line per table


def test_convert_never_prints_target_password(capsys, mdb_db):
    """No server needed: connecting fails (or the driver is missing); the secret must not leak."""
    secret = "s3cr3tPassw0rd"
    target = f"postgresql://someuser:{secret}@127.0.0.1:1/nodb"
    exc_text = ""
    try:
        rc, out, err = run(capsys, "--verbose", "convert", str(mdb_db), "--target", target)
    except Exception as exc:  # not an Acade2SqlError; still must not contain the secret
        exc_text = str(exc)
        cap = capsys.readouterr()
        out, err, rc = cap.out, cap.err, None
    assert secret not in out
    assert secret not in err
    assert secret not in exc_text
    assert rc != 0
