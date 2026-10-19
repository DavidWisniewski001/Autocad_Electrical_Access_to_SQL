"""Final-review regression tests for 0.3.0 (one section per review item).

Databases are built in pytest tmp dirs with the Access ODBC driver (hence ``requires_driver``);
corruption is applied to *copies* with byte-level helpers from ``tests.fixtures.final_review``.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import importlib.util
import io
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.fixtures import build_access, final_review
from tests.fixtures.final_review import ROWS, Layout

from acade2sql import __version__, cli, doctor, errors
from acade2sql.converter import convert
from acade2sql.errors import Acade2SqlError
from acade2sql.readers import open_reader
from acade2sql.redact import mask_url, redact

pytestmark = pytest.mark.requires_driver

ROOT = Path(__file__).resolve().parent.parent
FORMATS = ["mdb", "accdb"]
SOURCE_INTEGRITY = getattr(errors, "SourceIntegrityError", Acade2SqlError)


# ------------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module", params=FORMATS)
def core_db(request, tmp_path_factory) -> Path:
    """NETLIST (45 rows) + NOKEY. NEVER modified by tests (they copy it)."""
    d = tmp_path_factory.mktemp(f"fr_core_{request.param}")
    return final_review.build_core(d / f"core.{request.param}")


@pytest.fixture(scope="module", params=FORMATS)
def alter_db(request, tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp(f"fr_alter_{request.param}")
    return final_review.build_alter(d / f"alter.{request.param}")


@pytest.fixture(scope="module", params=FORMATS)
def chr_db(request, tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp(f"fr_chr_{request.param}")
    return final_review.build_chr(d / f"chr.{request.param}")


@pytest.fixture(scope="module", params=FORMATS)
def ancient_db(request, tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp(f"fr_old_{request.param}")
    return build_access.build_ancient(d / f"old.{request.param}")


@pytest.fixture(scope="module")
def netlist_expected() -> list[tuple]:
    return final_review.expected_netlist()


def count_sqlite(path: Path, table: str) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def sqlite_tables(path: Path) -> list[str]:
    con = sqlite3.connect(str(path))
    try:
        return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()


def read_all(path: Path, table: str = "NETLIST", reader: str = "pure") -> tuple[int, list[tuple]]:
    with open_reader(path, reader=reader) as r:  # type: ignore[arg-type]
        n = r.count_rows(table)
        rows = [tuple(row) for batch in r.iter_rows(table, 7) for row in batch]
    return n, rows


def corrupt_copy(src: Path, tmp_path: Path, mutate, table: str = "NETLIST") -> Path:
    layout = Layout(src, table)
    buf = bytearray(src.read_bytes())
    mutate(buf, layout)
    bad = tmp_path / f"corrupt{src.suffix}"
    bad.write_bytes(bytes(buf))
    return bad


def run_py(args: list[str], cwd: Path, **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        **kw,
    )


# ============================================================ 1. lost pages (owner changed)


def lose_first_page(buf, layout):
    final_review.set_page_owner(buf, layout, layout.data_pages[0], 0x7777)


@pytest.fixture
def lost_pages(core_db, tmp_path) -> Path:
    return corrupt_copy(core_db, tmp_path, lose_first_page)


def test_unmodified_core_reads_exactly(core_db, netlist_expected):
    n, rows = read_all(core_db)
    assert n == ROWS == len(rows)
    assert sorted(rows) == netlist_expected


def test_lost_pages_count_rows_raises_naming_table(lost_pages):
    with open_reader(lost_pages, reader="pure") as r, pytest.raises(SOURCE_INTEGRITY) as ei:
        r.count_rows("NETLIST")
    assert isinstance(ei.value, Acade2SqlError)
    assert "NETLIST" in str(ei.value)


def test_lost_pages_iter_rows_raises_naming_table(lost_pages):
    with open_reader(lost_pages, reader="pure") as r, pytest.raises(SOURCE_INTEGRITY) as ei:
        for _ in r.iter_rows("NETLIST"):
            pass
    assert "NETLIST" in str(ei.value)


def test_lost_pages_convert_full_is_table_error(lost_pages, tmp_path):
    target = tmp_path / "o.sqlite"
    report = convert(lost_pages, str(target), mode="full", tables=["NETLIST"], reader="pure")
    (res,) = report.tables
    assert res.name == "NETLIST"
    assert res.status == "error", res
    assert "NETLIST" in (res.error or "") or res.error
    assert report.ok is False


def test_lost_pages_sync_delete_missing_checksum_keeps_good_target(core_db, lost_pages, tmp_path):
    target = tmp_path / "t.sqlite"
    good = convert(core_db, str(target), mode="full", tables=["NETLIST"], reader="pure")
    assert good.ok
    assert count_sqlite(target, "NETLIST") == ROWS
    report_path = tmp_path / "r.json"
    rc = cli.main(
        [
            "convert",
            str(lost_pages),
            "--reader",
            "pure",
            "--target",
            str(target),
            "--tables",
            "NETLIST",
            "--mode",
            "sync",
            "--delete-missing",
            "--checksum",
            "--report",
            str(report_path),
        ]
    )
    assert rc == 3
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    assert doc["ok"] is False
    assert [t["status"] for t in doc["tables"]] == ["error"]
    # the lost rows must not have been interpreted as "deleted at the source"
    assert count_sqlite(target, "NETLIST") == ROWS


def test_lost_pages_sync_api_report_not_ok(core_db, lost_pages, tmp_path):
    target = tmp_path / "t.sqlite"
    convert(core_db, str(target), mode="full", tables=["NETLIST"], reader="pure")
    report = convert(
        lost_pages,
        str(target),
        mode="sync",
        tables=["NETLIST"],
        delete_missing=True,
        checksum=True,
        reader="pure",
    )
    assert report.ok is False
    assert report.tables[0].status == "error"
    assert count_sqlite(target, "NETLIST") == ROWS


# ================================================================== 2. pyodbc not importable

NO_PYODBC_CLI = (
    "import sys; sys.modules['pyodbc'] = None\n"
    "from acade2sql.cli import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)
NO_PYODBC_AUTO = (
    "import sys, logging; sys.modules['pyodbc'] = None\n"
    "logging.basicConfig(level=logging.WARNING)\n"
    "from acade2sql.readers import open_reader\n"
    "r = open_reader(sys.argv[1])\n"
    "print('KIND', r.kind, sorted(r.list_tables()))\n"
    "r.close()\n"
)
NO_PYODBC_ODBC = (
    "import sys; sys.modules['pyodbc'] = None\n"
    "from acade2sql.errors import Acade2SqlError\n"
    "from acade2sql.readers import open_reader\n"
    "try:\n"
    "    open_reader(sys.argv[1], reader='odbc')\n"
    "except Acade2SqlError as exc:\n"
    "    print('CLEAN', type(exc).__name__, exc)\n"
    "    sys.exit(0)\n"
    "print('NO ERROR')\n"
    "sys.exit(5)\n"
)


def no_pyodbc(tmp_path, code, *args):
    return run_py(["-c", code, *map(str, args)], tmp_path)


def test_no_pyodbc_probe_really_fails_import(tmp_path):
    p = run_py(["-c", "import sys; sys.modules['pyodbc'] = None; import pyodbc"], tmp_path)
    assert p.returncode != 0 and "ModuleNotFoundError" in p.stderr


def test_no_pyodbc_open_reader_auto_is_pure_with_warning(core_db, tmp_path):
    p = no_pyodbc(tmp_path, NO_PYODBC_AUTO, core_db)
    assert p.returncode == 0, p.stderr
    assert "KIND pure ['NETLIST', 'NOKEY']" in p.stdout
    assert "WARNING" in p.stderr and "falling back" in p.stderr.lower(), p.stderr
    assert "Traceback" not in p.stderr


def test_no_pyodbc_inspect_works(core_db, tmp_path):
    p = no_pyodbc(tmp_path, NO_PYODBC_CLI, "inspect", core_db, "--json")
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr
    doc = json.loads(p.stdout)
    assert doc["reader"] == "pure"
    by = {t["name"]: t for t in doc["tables"]}
    assert by["NETLIST"]["row_count"] == ROWS


def test_no_pyodbc_inspect_text_works(core_db, tmp_path):
    p = no_pyodbc(tmp_path, NO_PYODBC_CLI, "inspect", core_db)
    assert p.returncode == 0, p.stderr
    assert "NETLIST" in p.stdout and "Traceback" not in p.stderr


def test_no_pyodbc_convert_works(core_db, tmp_path):
    target = tmp_path / "np.sqlite"
    p = no_pyodbc(tmp_path, NO_PYODBC_CLI, "convert", core_db, "--target", target, "--checksum")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Traceback" not in p.stderr
    assert "PASS NETLIST" in p.stdout
    assert count_sqlite(target, "NETLIST") == ROWS


def test_no_pyodbc_doctor_reports_odbc_unusable_and_auto_pure(tmp_path):
    p = no_pyodbc(tmp_path, NO_PYODBC_CLI, "doctor")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Traceback" not in p.stderr
    odbc_lines = [ln for ln in p.stdout.splitlines() if "odbc reader" in ln]
    assert odbc_lines and all(ln.startswith("--") for ln in odbc_lines), p.stdout
    assert "auto uses pure" in p.stdout


def test_no_pyodbc_reader_odbc_api_is_clean_error(core_db, tmp_path):
    p = no_pyodbc(tmp_path, NO_PYODBC_ODBC, core_db)
    assert p.returncode == 0, p.stdout + p.stderr  # 5 = no error, 1 = uncontrolled traceback
    assert p.stdout.startswith("CLEAN ")
    assert len(p.stdout.strip().split(" ", 2)[2]) > 10  # an actionable message


@pytest.mark.parametrize("command", ["inspect", "convert"])
def test_no_pyodbc_reader_odbc_cli_exit_1_no_traceback(command, core_db, tmp_path):
    extra = ["--target", str(tmp_path / "x.sqlite")] if command == "convert" else []
    p = no_pyodbc(tmp_path, NO_PYODBC_CLI, command, core_db, "--reader", "odbc", *extra)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "Traceback" not in p.stderr and "Traceback" not in p.stdout
    assert p.stderr.startswith("error:") or "\nerror:" in p.stderr, p.stderr
    assert not (tmp_path / "x.sqlite").exists()


# ================================================================= 3. ALTER / DROP COLUMN


def both_readers(path: Path, table: str):
    out = {}
    for kind in ("pure", "odbc"):
        with open_reader(path, reader=kind) as r:  # type: ignore[arg-type]
            info = r.get_table(table)
            rows = sorted((tuple(row) for b in r.iter_rows(table) for row in b), key=lambda t: t[0])
            out[kind] = (info, rows, r.count_rows(table))
    return out


def typed(rows):
    return [tuple(type(v).__name__ for v in row) for row in rows]


@pytest.mark.parametrize("table", ["F", "V", "D"])
def test_alter_tables_pure_equals_odbc(alter_db, table):
    got = both_readers(alter_db, table)
    (pi, prows, pn), (oi, orows, on) = got["pure"], got["odbc"]
    assert pn == on == len(prows) == len(orows)
    assert prows == orows
    assert typed(prows) == typed(orows)
    assert pi == oi  # names, order, access_type, data_type, size, nullability, indexes, pk


def test_alter_add_column_after_rows_exact_values(alter_db):
    got = both_readers(alter_db, "F")
    for kind, (info, rows, n) in got.items():
        assert n == 21, kind
        assert [c.name for c in info.columns] == ["ID", "A", "B", "T", "M"], kind
        assert [c.access_type for c in info.columns] == [
            "INTEGER",
            "DOUBLE",
            "SMALLINT",
            "VARCHAR",
            "LONGCHAR",
        ], kind
        assert rows[:20] == [(i, i * 1.5, i, None, None) for i in range(1, 21)], kind
        assert rows[20] == (100, None, None, "new", "memo"), kind


def test_alter_drop_column_exact_values(alter_db):
    for kind, (info, rows, _n) in both_readers(alter_db, "V").items():
        assert [c.name for c in info.columns] == ["ID", "W"], kind
        assert rows == [(i, "w" if i == 3 else None) for i in range(1, 11)], kind
    for kind, (info, rows, _n) in both_readers(alter_db, "D").items():
        assert [c.name for c in info.columns] == ["ID", "Y", "Z"], kind
        assert rows == [(i, i / 4, f"z{i}") for i in range(1, 8)], kind


# =========================================================== 4. crafted files and fuzz

CRAFTED_MUST_RAISE = {
    "overflow_slot_at_page_end": final_review.overflow_slot_at_page_end,
    "zero_length_usage_map_record": final_review.empty_usage_map_record,
}
CRAFTED_CONTROLLED = {
    "empty_data_row": final_review.empty_data_row,
    "huge_column_count": final_review.huge_column_count,
}


@pytest.mark.parametrize("name", sorted(CRAFTED_MUST_RAISE))
def test_crafted_file_raises_acade2sql_error(core_db, tmp_path, name):
    bad = corrupt_copy(core_db, tmp_path, CRAFTED_MUST_RAISE[name])
    with pytest.raises(Acade2SqlError):  # not struct.error / IndexError / ValueError
        read_all(bad)


@pytest.mark.parametrize("name", sorted(CRAFTED_CONTROLLED))
def test_crafted_file_never_raises_uncontrolled_error(core_db, tmp_path, name):
    bad = corrupt_copy(core_db, tmp_path, CRAFTED_CONTROLLED[name])
    with contextlib.suppress(Acade2SqlError):
        read_all(bad)


def read_everything(path: Path) -> int:
    """Open, list, describe, count and fully read every table with the pure reader."""
    total = 0
    with open_reader(path, reader="pure") as r:
        for name in r.list_tables():
            r.get_table(name)
            r.count_rows(name)
            for batch in r.iter_rows(name, 1000):
                total += len(batch)
    return total


def interesting_pages(path: Path) -> tuple[int, list[int]]:
    from acade2sql.readers.access_parser_reader import AccessParserReader

    pages = {0, 1, 2}
    layout = Layout(path, "NETLIST")
    pages |= {layout.tdef_page, layout.usage_map >> 8, *layout.data_pages}
    with AccessParserReader(path) as r:
        jet = r._file()
        pages |= set(jet.data_pages(jet.table_def("MSysObjects", 2)))
        size = jet.page_size
    return size, sorted(pages)


@pytest.mark.slow
def test_seeded_fuzz_only_success_or_acade2sql_error(core_db, tmp_path):
    rng = random.Random(20261019)
    base = core_db.read_bytes()
    size, hot = interesting_pages(core_db)
    n_pages = len(base) // size
    path = tmp_path / f"fuzz{core_db.suffix}"
    bad: list[str] = []
    outcomes = {"ok": 0, "acade2sql": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        for case in range(200):
            buf = bytearray(base)
            edits = []
            for _ in range(rng.randint(1, 8)):
                page = rng.choice(hot) if rng.random() < 0.8 else rng.randrange(n_pages)
                pos = page * size + rng.randrange(size)
                val = rng.choice([0, 0xFF, 0x80, 0x40, 1, rng.randrange(256)])
                buf[pos] = val
                edits.append((pos, val))
            path.write_bytes(bytes(buf))
            started = time.perf_counter()
            future = pool.submit(read_everything, path)
            try:
                future.result(timeout=5)
                outcomes["ok"] += 1
            except Acade2SqlError:
                outcomes["acade2sql"] += 1
            except concurrent.futures.TimeoutError:
                bad.append(f"case {case}: read took more than 5 s, edits={edits}")
                break  # the worker is stuck; stop instead of piling up threads
            except BaseException as exc:
                bad.append(f"case {case}: {type(exc).__name__}: {exc}; edits={edits}")
            elapsed = time.perf_counter() - started
            if elapsed >= 5:
                bad.append(f"case {case}: {elapsed:.1f} s; edits={edits}")
    assert not bad, f"{len(bad)} uncontrolled outcome(s) of 200 (first 5):\n" + "\n".join(bad[:5])
    assert outcomes["ok"] + outcomes["acade2sql"] == 200
    assert outcomes["acade2sql"] >= 10, outcomes  # the mutations must actually bite


# ============================================================== 5. corrupt variable offsets


def test_present_bit_without_var_slot_raises(core_db, tmp_path):
    bad = corrupt_copy(core_db, tmp_path, final_review.drop_var_slot)
    with pytest.raises(Acade2SqlError):  # was: silently None
        read_all(bad)


def test_decreasing_var_offsets_raise(core_db, tmp_path):
    bad = corrupt_copy(core_db, tmp_path, final_review.swap_var_offsets)
    with pytest.raises(Acade2SqlError):  # was: silently ''
        read_all(bad)


# ====================================================================================== 6. BIGINT


@pytest.fixture(scope="module")
def bigint_db(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("fr_bigint")
    try:
        return final_review.build_bigint(d / "big.accdb")
    except Exception as exc:  # the driver of this machine cannot create BIGINT columns
        pytest.skip(f"cannot create a BIGINT column here: {exc}")


def bigint_supported(path: Path) -> bool:
    """True if get_table accepts BIGINT, False if it refuses it up front (both are allowed)."""
    with open_reader(path, reader="pure") as r:
        try:
            r.get_table("BIG64")
        except Acade2SqlError:
            return False
    return True


def test_bigint_is_read_as_int_or_refused_up_front(bigint_db):
    with open_reader(bigint_db, reader="pure") as r:
        try:
            info = r.get_table("BIG64")
        except Acade2SqlError as exc:
            assert "BI" in str(exc) or "BIG64" in str(exc), str(exc)
            return
        rows = sorted((tuple(x) for b in r.iter_rows("BIG64") for x in b), key=lambda t: t[0])
    assert [c.name for c in info.columns] == ["ID", "BI"]
    assert rows == [(1, 2**40), (2, -(2**40)), (3, 0), (4, None)]
    assert all(type(row[1]) in (int, type(None)) for row in rows)


def test_bigint_convert_matches_the_decision(bigint_db, tmp_path):
    target = tmp_path / "b.sqlite"
    report = convert(bigint_db, str(target), reader="pure")
    by = {t.name: t for t in report.tables}
    assert by["OKTBL"].status == "pass"  # one bad table never stops the others
    if bigint_supported(bigint_db):
        assert by["BIG64"].status == "pass", by["BIG64"]
        con = sqlite3.connect(str(target))
        try:
            got = sorted(con.execute("SELECT ID, BI FROM BIG64"))
        finally:
            con.close()
        assert got == [(1, 2**40), (2, -(2**40)), (3, 0), (4, None)]
    else:
        assert by["BIG64"].status == "error"
        assert report.ok is False
        assert "BIG64" not in sqlite_tables(target)  # no target table for a refused source table


# =========================================================== 7. live-writer warning (lock file)


def lock_name(path: Path) -> Path:
    return path.with_suffix(".ldb" if path.suffix.lower() == ".mdb" else ".laccdb")


def cts_warnings(caplog) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "--copy-to-temp" in rec.getMessage()
    ]


def test_lock_file_next_to_source_warns_about_copy_to_temp(core_db, tmp_path, caplog):
    copy = tmp_path / core_db.name
    shutil.copy2(core_db, copy)
    lock = lock_name(copy)
    lock.write_bytes(b"\x00" * 64)
    with caplog.at_level(logging.WARNING):
        n, _ = read_all(copy)
    assert n == ROWS
    assert cts_warnings(caplog), [r.getMessage() for r in caplog.records]
    assert lock.exists() and lock.read_bytes() == b"\x00" * 64  # the reader never touches it


def test_no_lock_file_no_warning(core_db, tmp_path, caplog):
    copy = tmp_path / core_db.name
    shutil.copy2(core_db, copy)
    with caplog.at_level(logging.WARNING):
        read_all(copy)
    assert not cts_warnings(caplog)


def test_copy_to_temp_reads_the_copy(core_db, tmp_path, netlist_expected):
    copy = tmp_path / core_db.name
    shutil.copy2(core_db, copy)
    lock_name(copy).write_bytes(b"\x00" * 64)
    with open_reader(copy, reader="pure", copy_to_temp=True) as r:
        copy.unlink()  # the original is gone: only a private copy can still be read
        lock_name(copy).unlink()
        assert r.count_rows("NETLIST") == ROWS
        rows = sorted(tuple(x) for b in r.iter_rows("NETLIST") for x in b)
    assert rows == netlist_expected


# ======================================================================================= 8. redact


def test_redact_query_password_value_masked_and_neighbour_kept():
    out = redact("postgresql://u:p@h/db?password=ab&sslmode=require")
    assert out.endswith("password=***&sslmode=require"), out
    assert "password=ab" not in out
    assert ":p@" not in out


def test_redact_query_password_value_masked_with_url_context():
    url = "postgresql://u:p@h/db?password=ab&sslmode=require"
    out = redact(f"connection to {url} failed", url)
    assert "password=***&sslmode=require" in out and "password=ab" not in out


def test_mask_url_query_password():
    out = mask_url("postgresql://u:p@h/db?password=ab&sslmode=require")
    assert "password=***" in out and "sslmode=require" in out
    assert "password=ab" not in out and ":p@" not in out


@pytest.mark.parametrize("text", ["DRIVER=x;PWD=a&b;UID=u", "DRIVER=x;PASSWORD=a&b;UID=u"])
def test_redact_odbc_pwd_with_ampersand_does_not_leak_any_part(text):
    out = redact(text)
    assert "a&b" not in out and "&b" not in out, out
    assert "UID=u" in out and "DRIVER=x" in out and "***" in out


def test_redact_odbc_pwd_with_ampersand_inside_url():
    url = "mssql+pyodbc:///?odbc_connect=DRIVER%3Dx%3BPWD%3Da%26b%3BUID%3Du"
    for out in (mask_url(url), redact(f"cannot connect {url}", url)):
        assert "a%26b" not in out and "a&b" not in out and "%26b" not in out and "&b" not in out, (
            out
        )


# =========================================================================== 9. compare_readers

COMPARE = ROOT / "scripts" / "compare_readers.py"


def compare(tmp_path, *args):
    return run_py([str(COMPARE), *map(str, args)], tmp_path)


def test_compare_readers_identical_copies_exit_0(core_db, tmp_path):
    a, b = tmp_path / f"a{core_db.suffix}", tmp_path / f"b{core_db.suffix}"
    shutil.copy2(core_db, a)
    shutil.copy2(core_db, b)
    p = compare(tmp_path, a, b)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Traceback" not in p.stderr
    assert p.stdout.count("identical") >= 2 and "DIFF" not in p.stdout


@pytest.mark.parametrize("missing_first", [True, False])
def test_compare_readers_missing_source_exit_2_but_compares_the_rest(
    core_db, tmp_path, missing_first
):
    good = tmp_path / f"good{core_db.suffix}"
    shutil.copy2(core_db, good)
    gone = tmp_path / f"gone{core_db.suffix}"
    out = tmp_path / "cmp.json"
    order = [gone, good] if missing_first else [good, gone]
    p = compare(tmp_path, *order, "--json", out)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "Traceback" not in p.stderr and "Traceback" not in p.stdout
    assert "gone" in p.stderr + p.stdout, "the message must name the missing file"
    assert "NETLIST" in p.stdout  # the other database was still compared
    assert out.exists(), "--json must still be written"
    docs = json.loads(out.read_text(encoding="utf-8"))
    by_source = {d["source"]: d for d in docs}
    assert by_source[str(good)]["identical"] == by_source[str(good)]["total"] > 0
    assert not by_source[str(good)].get("error")
    if str(gone) in by_source:  # the missing one may be listed, but only with its error
        assert by_source[str(gone)]["error"] and by_source[str(gone)]["tables"] == []


def _load_compare_module():
    spec = importlib.util.spec_from_file_location("compare_readers_under_test", COMPARE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up here
    spec.loader.exec_module(mod)
    return mod


class AlteringReader:
    """Delegates to a real reader but changes one value of NETLIST (as if it mis-read it)."""

    def __init__(self, inner):
        self._inner = inner
        self.kind = inner.kind

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def iter_rows(self, name, batch_size=5000):
        for k, batch in enumerate(self._inner.iter_rows(name, batch_size)):
            if name == "NETLIST" and k == 0:
                first = list(batch[0])
                first[1] = first[1] + "!"
                batch = [tuple(first), *batch[1:]]
            yield batch


def test_compare_readers_difference_exits_1_and_lists_the_table(core_db, tmp_path, capsys):
    """A read that disagrees between the two readers must give exit 1 naming the table.

    compare_readers compares the two readers on each file (never two files with each other), so a
    value changed in a copy cannot differ between them; the disagreement is injected by wrapping
    the pure reader.
    """
    mod = _load_compare_module()
    real = mod.open_reader

    def patched(path, **kw):
        reader = real(path, **kw)
        return AlteringReader(reader) if kw.get("reader") == "pure" else reader

    mod.open_reader = patched
    out = tmp_path / "d.json"
    rc = mod.main([str(core_db), "--json", str(out)])
    cap = capsys.readouterr().out
    assert rc == 1
    assert "DIFF NETLIST" in cap
    assert "checksum differs" in cap
    doc = json.loads(out.read_text(encoding="utf-8"))[0]
    assert [t["name"] for t in doc["tables"] if not t["identical"]] == ["NETLIST"]


# ======================================================================================= 10. doctor


def doctor_lines(monkeypatch, drivers: list[str]) -> tuple[int, list[str]]:
    import pyodbc

    monkeypatch.setattr(pyodbc, "drivers", lambda: list(drivers))
    buf = io.StringIO()
    rc = doctor.run(buf)
    return rc, buf.getvalue().splitlines()


def sql_server_line(lines: list[str]) -> str:
    found = [ln for ln in lines if "SQL Server ODBC driver" in ln]
    assert len(found) == 1, lines
    return found[0]


def test_doctor_old_sql_server_driver_is_not_ok(monkeypatch):
    _rc, lines = doctor_lines(monkeypatch, ["SQL Server"])
    line = sql_server_line(lines)
    assert "not supported by the writer" in line
    assert not line.startswith("ok"), line
    assert not any(ln.startswith("ok") and "SQL Server" in ln and "ODBC" in ln for ln in lines)


def test_doctor_modern_sql_server_driver_is_ok(monkeypatch):
    _rc, lines = doctor_lines(monkeypatch, ["ODBC Driver 18 for SQL Server"])
    line = sql_server_line(lines)
    assert line.startswith("ok"), line
    assert "ODBC Driver 18 for SQL Server" in line
    assert "not supported" not in line


# ======================================================================================== 11. nits


def test_char_column_pure_matches_odbc(chr_db):
    got = {}
    for kind in ("pure", "odbc"):
        with open_reader(chr_db, reader=kind) as r:  # type: ignore[arg-type]
            got[kind] = (
                r.get_table("C"),
                sorted((tuple(x) for b in r.iter_rows("C") for x in b), key=lambda t: t[0]),
            )
    for name in ("CH", "CHC"):
        pure = next(c for c in got["pure"][0].columns if c.name == name)
        odbc = next(c for c in got["odbc"][0].columns if c.name == name)
        assert odbc.access_type == "CHAR" and odbc.data_type == -8, odbc  # the reference
        assert (pure.access_type, pure.data_type) == ("CHAR", -8), pure
        assert pure == odbc
    assert got["pure"][0] == got["odbc"][0]
    assert got["pure"][1] == got["odbc"][1]
    # CHAR(10) is blank-padded by the engine; NULL stays NULL
    padded = [None if v is None else v.ljust(10) for v in final_review.CHAR_VALUES]
    assert [r[1] for r in got["pure"][1]] == padded


def test_python_dash_m_version(tmp_path):
    p = run_py(["-m", "acade2sql", "--version"], tmp_path)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == f"acade2sql {__version__}"
    assert "Traceback" not in p.stderr


def _dry_run(core_db, tmp_path, capsys, *extra):
    target = tmp_path / "dry.sqlite"
    rc = cli.main(
        [
            "convert",
            str(core_db),
            "--reader",
            "pure",
            "--mode",
            "sync",
            "--tables",
            "NOKEY",
            "--target",
            str(target),
            *extra,
        ]
    )
    cap = capsys.readouterr()
    return rc, cap.out, cap.err, target


def test_dry_run_keyless_sync_default_matches_real_run(core_db, tmp_path, capsys):
    """DOCS/CLI.md: a missing sync key is ERROR in a dry run too (exit 3), as in the real run."""
    rc, out, _err, target = _dry_run(core_db, tmp_path, capsys, "--dry-run")
    assert rc == 3, out
    assert "ERROR NOKEY" in out and "no key for sync" in out
    assert "actions=" not in out.split("ERROR NOKEY", 1)[1].splitlines()[0]  # DECISIONS #42
    assert not target.exists(), "--dry-run must not create the SQLite file"
    real_rc, real_out, _e, _t = _dry_run(core_db, tmp_path / "real", capsys)
    assert real_rc == rc
    assert "ERROR NOKEY" in real_out and "no key for sync" in real_out


def test_dry_run_keyless_sync_no_key_replace_plans_replace_rows(core_db, tmp_path, capsys):
    rc, out, _err, target = _dry_run(core_db, tmp_path, capsys, "--dry-run", "--no-key", "replace")
    assert rc == 0, out
    assert "PLANNED NOKEY" in out and "replace-rows" in out
    assert not target.exists()


@pytest.mark.parametrize("kind", ["pure", "odbc"])
def test_pre_1753_dates_warn_but_do_not_change_status(ancient_db, tmp_path, kind):
    target = tmp_path / f"old_{kind}.sqlite"
    report = convert(ancient_db, str(target), reader=kind, checksum=True)
    (res,) = report.tables
    assert res.name == "DATES"
    assert res.status == "pass", res
    assert report.ok
    hits = [w for w in res.warnings if "dates before 1753" in w.lower()]
    assert len(hits) == 1, res.warnings
    assert count_sqlite(target, "DATES") == len(build_access.ANCIENT_DATES)


def test_no_pre_1753_warning_without_such_dates(core_db, tmp_path):
    report = convert(core_db, str(tmp_path / "n.sqlite"), reader="pure")
    for res in report.tables:
        assert not [w for w in res.warnings if "1753" in w], (res.name, res.warnings)
