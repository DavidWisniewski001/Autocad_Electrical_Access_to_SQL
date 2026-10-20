"""M5 CLI polish (docs/M5_SPEC.md sections 3-4): progress, doctor, --tables-file/--exclude-file,
--checksum with --dry-run. Driver-free except where noted (an in-memory reader is installed)."""

from __future__ import annotations

import io
import json
import platform
import re
import sys

import pytest
from tests.fakes import FakeReader, std_info, std_rows

from acade2sql import __version__, cli

PROGRESS = re.compile(r"^\[(\d+)/(\d+)\] (.+) (\d+) rows (\d+(?:\.\d+)?)s$")


def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def make_reader(names=("A_TBL", "ALLEN BRADLEY", "Order")):
    return FakeReader({n: (std_info(n), std_rows(range(1, i + 3))) for i, n in enumerate(names)})


def _progress_lines(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if ln.startswith("[")]


class TtyStderr(io.StringIO):
    def isatty(self) -> bool:
        return True


# ------------------------------------------------------------------------------------ progress


def test_progress_flag_writes_one_line_per_table_to_stderr(capsys, tmp_path, install_reader):
    rd = install_reader(make_reader())
    rc, out, err = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"), "--progress"
    )
    assert rc == 0, err
    lines = _progress_lines(err)
    assert len(lines) == len(rd.tables) == 3
    seen = []
    for i, line in enumerate(lines, 1):
        m = PROGRESS.match(line)
        assert m, line
        assert (int(m[1]), int(m[2])) == (i, 3)
        name, rows = m[3], int(m[4])
        assert rows == len(rd.tables[name][1])
        seen.append(name)
    assert seen == ["A_TBL", "ALLEN BRADLEY", "Order"]  # names with spaces survive, order kept
    assert not any(PROGRESS.match(ln) for ln in out.splitlines()), "progress leaked to stdout"
    assert "[1/3]" not in out


def test_progress_does_not_change_stdout(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    rc1, out1, err1 = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "a.sqlite"), "--progress"
    )
    rc2, out2, err2 = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "b.sqlite"), "--no-progress"
    )
    assert rc1 == rc2 == 0
    assert out1 == out2
    assert _progress_lines(err1) and not _progress_lines(err2)


def test_no_progress_writes_none(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    rc, _out, err = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"), "--no-progress"
    )
    assert rc == 0
    assert _progress_lines(err) == []
    assert not any(PROGRESS.match(ln) for ln in err.splitlines())


def test_default_with_non_tty_stderr_writes_none(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    assert not sys.stderr.isatty()
    rc, _out, err = run(capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"))
    assert rc == 0
    assert _progress_lines(err) == []


def test_default_with_tty_stderr_writes_progress(monkeypatch, capsys, tmp_path, install_reader):
    install_reader(make_reader())
    tty = TtyStderr()
    monkeypatch.setattr(sys, "stderr", tty)
    rc = cli.main(["convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite")])
    out = capsys.readouterr().out
    assert rc == 0
    lines = _progress_lines(tty.getvalue())
    assert len(lines) == 3 and all(PROGRESS.match(ln) for ln in lines)
    assert "[1/3]" not in out


def test_no_progress_wins_over_tty(monkeypatch, capsys, tmp_path, install_reader):
    install_reader(make_reader())
    tty = TtyStderr()
    monkeypatch.setattr(sys, "stderr", tty)
    rc = cli.main(["convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"), "--no-progress"])
    capsys.readouterr()
    assert rc == 0
    assert _progress_lines(tty.getvalue()) == []


def test_progress_counts_selected_tables_only(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    rc, _out, err = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"),
        "--progress", "--exclude", "Order",
    )  # fmt: skip
    assert rc == 0, err
    lines = _progress_lines(err)
    assert [PROGRESS.match(ln)[2] for ln in lines] == ["2", "2"]


# ---------------------------------------------------------------------- --checksum + --dry-run


def test_checksum_with_dry_run_warns_and_keeps_exit_code(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    target = str(tmp_path / "never.sqlite")
    rc0, out0, err0 = run(capsys, "convert", "fake.mdb", "--target", target, "--dry-run")
    rc1, out1, err1 = run(
        capsys, "convert", "fake.mdb", "--target", target, "--dry-run", "--checksum"
    )
    assert rc0 == rc1 == 0
    assert "ignored" in err1.lower() and "dry-run" in err1.lower()
    assert "ignored" not in err0.lower()
    assert "checksum=" not in out1
    assert not (tmp_path / "never.sqlite").exists()
    assert out0 == out1


def test_checksum_without_dry_run_no_ignored_warning(capsys, tmp_path, install_reader):
    install_reader(make_reader())
    rc, out, err = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"), "--checksum"
    )
    assert rc == 0
    assert "ignored" not in err.lower()
    assert out.count("checksum=match") == 3


# ------------------------------------------------------------ --tables-file / --exclude-file


COMMA_TABLES = ("A,B", "A", "B", "C", "Ünï")


def comma_reader():
    return FakeReader({n: (std_info(n), std_rows(range(1, 3))) for n in COMMA_TABLES})


def converted(capsys, tmp_path, *extra):
    rp = tmp_path / "rep.json"
    rc, out, err = run(
        capsys, "convert", "fake.mdb", "--target", str(tmp_path / "o.sqlite"),
        "--report", str(rp), *extra,
    )  # fmt: skip
    names = (
        {t["name"] for t in json.loads(rp.read_text("utf-8"))["tables"]} if rp.exists() else None
    )
    return rc, names, out, err


def write_lines(path, text, newline="\n"):
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return str(path)


def test_tables_file_name_with_comma_is_one_name(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "A,B\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables-file", f)
    assert rc == 0, err
    assert names == {"A,B"}  # not {"A", "B"}


def test_tables_file_comments_and_blank_lines(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "# header comment\n\nA\n   \n# B is off\n\t\nC\n\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables-file", f)
    assert rc == 0, err
    assert names == {"A", "C"}


def test_tables_file_utf8_no_bom(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    p = tmp_path / "t.txt"
    p.write_bytes("Ünï\n".encode())
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables-file", str(p))
    assert rc == 0, err
    assert names == {"Ünï"}


def test_tables_file_crlf_line_endings(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "# c\r\nA\r\nA,B\r\n", newline="")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables-file", f)
    assert rc == 0, err
    assert names == {"A", "A,B"}


def test_tables_file_combines_with_tables(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "C\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables", "A", "--tables-file", f)
    assert rc == 0, err
    assert names == {"A", "C"}


def test_tables_comma_split_vs_file_exact(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "A,B\n")
    # --tables A,B means the tables A and B; the file adds the one table called "A,B"
    rc, names, _out, err = converted(capsys, tmp_path, "--tables", "A,B", "--tables-file", f)
    assert rc == 0, err
    assert names == {"A", "B", "A,B"}


def test_tables_file_unknown_name_fails_like_tables(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "NOPE\n")
    rc, _names, _out, err = converted(capsys, tmp_path, "--tables-file", f)
    assert rc == 1 and "NOPE" in err
    assert not (tmp_path / "o.sqlite").exists()


def test_tables_file_missing_file(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    missing = tmp_path / "nope.txt"
    rc, _names, _out, err = converted(capsys, tmp_path, "--tables-file", str(missing))
    assert rc in (1, 2)
    assert "nope.txt" in err
    assert "Traceback" not in err
    assert not (tmp_path / "o.sqlite").exists()


def test_exclude_file(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "x.txt", "# skip\nA,B\n\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--exclude-file", f)
    assert rc == 0, err
    assert names == {"A", "B", "C", "Ünï"}


def test_exclude_file_combines_with_exclude(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "x.txt", "A,B\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--exclude", "C", "--exclude-file", f)
    assert rc == 0, err
    assert names == {"A", "B", "Ünï"}


def test_tables_file_and_exclude_file_together(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    t = write_lines(tmp_path / "t.txt", "A,B\nA\nC\n")
    x = write_lines(tmp_path / "x.txt", "A\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables-file", t, "--exclude-file", x)
    assert rc == 0, err
    assert names == {"A,B", "C"}


def test_exclude_file_only_comments_excludes_nothing(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    x = write_lines(tmp_path / "x.txt", "# nothing\n\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--exclude-file", x)
    assert rc == 0, err
    assert names == set(COMMA_TABLES)


def test_tables_file_in_dry_run(capsys, tmp_path, install_reader):
    install_reader(comma_reader())
    f = write_lines(tmp_path / "t.txt", "A,B\n")
    rc, names, _out, err = converted(capsys, tmp_path, "--tables-file", f, "--dry-run")
    assert rc == 0, err
    assert names == {"A,B"}


@pytest.mark.requires_driver
def test_tables_file_real_comma_table(capsys, tmp_path, mdb_db):
    f = write_lines(tmp_path / "t.txt", "COMMA,NAME\n")
    rp = tmp_path / "rep.json"
    rc, _out, err = run(
        capsys, "convert", str(mdb_db), "--target", str(tmp_path / "o.sqlite"),
        "--tables-file", f, "--report", str(rp), "--checksum",
    )  # fmt: skip
    assert rc == 0, err
    doc = json.loads(rp.read_text("utf-8"))
    assert [t["name"] for t in doc["tables"]] == ["COMMA,NAME"]
    assert doc["tables"][0]["checksum_match"] is True and doc["tables"][0]["target_rows"] == 2


# ---------------------------------------------------------------------------------- doctor


# Expects "usable readers: odbc, pure"; the Linux test container has no Access driver.
@pytest.mark.requires_driver
def test_doctor_reports_sections_and_exits_0(capsys):
    rc, out, err = run(capsys, "doctor")
    text = out + err
    assert rc == 0, text
    low = text.lower()
    assert platform.python_version() in text
    assert "64" in text or "32" in text  # bitness
    assert __version__ in text
    for needle in ("access", "odbc", "pure", "psycopg", "pyodbc", "sql server"):
        assert needle in low, needle
    assert "reader" in low
    assert "usable readers: odbc, pure" in text
    assert "(64-bit)" in text if platform.architecture()[0] == "64bit" else "(32-bit)" in text


@pytest.mark.requires_driver
def test_doctor_lists_access_driver_and_odbc_usable(capsys):
    rc, out, err = run(capsys, "doctor")
    assert rc == 0
    assert "Microsoft Access Driver (*.mdb, *.accdb)" in out + err


def test_doctor_writes_stdout_not_only_stderr(capsys):
    _rc, out, _err = run(capsys, "doctor")
    assert out.strip()


def test_doctor_without_driver_still_ok_via_pure(capsys, no_odbc_driver):
    rc, out, err = run(capsys, "doctor")
    assert rc == 0, out + err
    assert "usable readers: pure" in out
    assert "odbc" not in out.split("usable readers:")[1]


def test_doctor_no_usable_reader_exits_1(capsys, monkeypatch, no_odbc_driver):
    # make the pure reader unusable as well: access_parser cannot be imported
    monkeypatch.setitem(sys.modules, "access_parser", None)
    rc, out, err = run(capsys, "doctor")
    assert rc == 1, out + err
    assert "Traceback" not in out + err
    assert "usable readers: none" in out.lower() or "no usable reader" in (out + err).lower()


def test_doctor_is_listed_in_help(capsys):
    rc, out, _err = run(capsys, "--help")
    assert rc == 0 and "doctor" in out
