"""M7 desktop GUI: form -> convert() mapping, URL building, worker messages, converter hooks.

Everything except the ``tk`` section is display-free. The Tk tests build the real window and
skip when Tk cannot start (no display / no Tcl).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from tests.fakes import FakeReader, by_name, dump, std_info, std_rows

from acade2sql import cli, converter
from acade2sql.converter import CANCELLED, convert
from acade2sql.errors import SourceNotFoundError
from acade2sql.gui import format as fmt
from acade2sql.gui import options as op
from acade2sql.gui import worker as wk
from acade2sql.validate import Report, TableResult

ROOT = Path(__file__).resolve().parent.parent
SECRET = "S3cr3t@pass:/%"


def make_reader(names=("A", "B", "C"), rows=5):
    return FakeReader({n: (std_info(n), std_rows(range(1, rows + 1))) for n in names})


def server(**kw) -> op.ServerFields:
    base = {"host": "sqlhost", "database": "acade", "user": "acade", "password": SECRET}
    base.update(kw)
    return op.ServerFields(**base)


# ------------------------------------------------------------------------------- parsing


def test_parse_names_commas_newlines_and_blanks():
    assert op.parse_names(" A, B ,\nC D\n\n,") == ["A", "B", "C D"]
    assert op.parse_names("") is None
    assert op.parse_names(" , ") is None


def test_parse_keys_keeps_commas_inside_a_spec():
    assert op.parse_keys("COMP=CAT,MFG ; ID\nX=Y") == ["COMP=CAT,MFG", "ID", "X=Y"]
    assert op.parse_keys("  ") is None


@pytest.mark.parametrize("text", ["0", "-5", "abc", "1.5", ""])
def test_batch_size_invalid(text):
    with pytest.raises(op.OptionsError):
        op.parse_batch_size(text)


def test_batch_size_valid():
    assert op.parse_batch_size(" 250 ") == 250


# ------------------------------------------------------------------------------- URLs


def test_mssql_url_sql_login_encodes_password():
    url = op.mssql_url(server())
    parsed = make_url(url)
    assert parsed.drivername == "mssql+pyodbc"
    assert parsed.password == SECRET and parsed.username == "acade"
    assert parsed.host == "sqlhost" and parsed.database == "acade"
    assert parsed.query["driver"] == "ODBC Driver 18 for SQL Server"
    assert "TrustServerCertificate" not in parsed.query


def test_mssql_url_windows_login_instance_port_trust():
    f = server(instance="SQLEXPRESS", port="1433", windows_auth=True, trust_server_certificate=True)
    parsed = make_url(op.mssql_url(f))
    assert parsed.username is None and parsed.password is None
    assert parsed.host == "sqlhost\\SQLEXPRESS" and parsed.port == 1433
    assert parsed.query["trusted_connection"] == "yes"
    assert parsed.query["TrustServerCertificate"] == "yes"


def test_mssql_url_requires_host_and_database():
    with pytest.raises(op.OptionsError, match="host"):
        op.mssql_url(server(host=" "))
    with pytest.raises(op.OptionsError, match="Database"):
        op.mssql_url(server(database=""))


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_bad_port(port):
    with pytest.raises(op.OptionsError, match="Port"):
        op.postgresql_url(server(port=port))


def test_postgresql_url_default_port_and_sslmode():
    parsed = make_url(op.postgresql_url(server(sslmode="require")))
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.port == 5432 and parsed.password == SECRET
    assert parsed.query == {"sslmode": "require"}
    assert make_url(op.postgresql_url(server(port="6543"))).port == 6543


def test_password_without_user_is_dropped():
    parsed = make_url(op.postgresql_url(server(user="")))
    assert parsed.username is None and parsed.password is None


def test_sqlite_target_is_absolute_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = op.target_url(op.GuiOptions(target_kind="sqlite", sqlite_path="out 100%.sqlite"))
    parsed = make_url(url)
    assert parsed.get_backend_name() == "sqlite"
    assert Path(parsed.database) == (tmp_path / "out 100%.sqlite").resolve()


def test_custom_url_validation():
    with pytest.raises(op.OptionsError):
        op.target_url(op.GuiOptions(target_kind="url", url=""))
    with pytest.raises(op.OptionsError, match="SQLAlchemy URL"):
        op.target_url(op.GuiOptions(target_kind="url", url="not a url"))
    url = "postgresql+psycopg://u:pw@h/db"
    assert op.target_url(op.GuiOptions(target_kind="url", url=url)) == url


@pytest.mark.parametrize("kind", ["mssql", "postgresql"])
def test_display_target_masks_password(kind):
    shown = op.display_target(op.GuiOptions(target_kind=kind, server=server()))
    assert SECRET not in shown and "S3cr3t" not in shown
    assert "***" in shown


def test_display_target_masks_custom_url_and_reports_incomplete():
    shown = op.display_target(
        op.GuiOptions(target_kind="url", url="postgresql+psycopg://u:hunter22@h/db")
    )
    assert "hunter22" not in shown
    assert op.display_target(op.GuiOptions(target_kind="mssql")).startswith("(incomplete")


def test_default_sqlite_path():
    assert op.default_sqlite_path("") == ""
    assert Path(op.default_sqlite_path(r"C:\ACADE\default_cat.mdb")) == Path(
        r"C:\ACADE\default_cat.sqlite"
    )


# ------------------------------------------------------------------------------- convert kwargs


def _opts(**kw) -> op.GuiOptions:
    base = {"source": "cat.mdb", "target_kind": "url", "url": "sqlite:///x.sqlite"}
    base.update(kw)
    return op.GuiOptions(**base)


def test_convert_kwargs_defaults_match_convert_defaults():
    kw = op.convert_kwargs(_opts(), dry_run=False)
    assert kw == {
        "source": "cat.mdb",
        "target": "sqlite:///x.sqlite",
        "mode": "full",
        "tables": None,
        "exclude": None,
        "batch_size": 5000,
        "checksum": False,
        "copy_to_temp": False,
        "report_path": None,
        "key": None,
        "no_key": "error",
        "delete_missing": False,
        "schema": None,
        "dry_run": False,
        "counts": True,
        "reader": "auto",
    }
    # every key is a real convert() parameter
    import inspect

    params = inspect.signature(convert).parameters
    assert set(kw) <= set(params)


def test_convert_kwargs_full_mapping():
    opts = _opts(
        mode="append",
        reader="pure",
        copy_to_temp=True,
        checksum=True,
        report_path=" r.json ",
        schema=" dbo ",
        tables="A, B",
        exclude="C",
        batch_size="100",
    )
    kw = op.convert_kwargs(opts, dry_run=False)
    assert kw["mode"] == "append" and kw["reader"] == "pure"
    assert kw["copy_to_temp"] is True and kw["checksum"] is True
    assert kw["report_path"] == "r.json" and kw["schema"] == "dbo"
    assert kw["tables"] == ["A", "B"] and kw["exclude"] == ["C"]
    assert kw["batch_size"] == 100


def test_sync_only_options_ignored_outside_sync():
    opts = _opts(mode="full", keys="ID", no_key="replace", delete_missing=True)
    kw = op.convert_kwargs(opts, dry_run=False)
    assert kw["key"] is None and kw["no_key"] == "error" and kw["delete_missing"] is False


def test_sync_options_passed_in_sync():
    opts = _opts(mode="sync", keys="COMP=CAT,MFG;ID", no_key="replace", delete_missing=True)
    kw = op.convert_kwargs(opts, dry_run=False)
    assert kw["key"] == ["COMP=CAT,MFG", "ID"]
    assert kw["no_key"] == "replace" and kw["delete_missing"] is True


def test_dry_run_drops_checksum_and_honours_counts():
    kw = op.convert_kwargs(_opts(checksum=True, counts=False), dry_run=True)
    assert kw["dry_run"] is True and kw["checksum"] is False and kw["counts"] is False
    # counts=False is only valid with a dry run: never passed to a real run
    assert op.convert_kwargs(_opts(counts=False), dry_run=False)["counts"] is True


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("source", " ", "Source"),
        ("mode", "merge", "mode"),
        ("reader", "jet", "reader"),
        ("batch_size", "x", "Batch"),
    ],
)
def test_convert_kwargs_errors(field, value, match):
    with pytest.raises(op.OptionsError, match=match):
        op.convert_kwargs(_opts(**{field: value}), dry_run=False)


def test_convert_kwargs_are_accepted_by_convert(tmp_path, install_reader):
    install_reader(make_reader())
    target = tmp_path / "o.sqlite"
    opts = _opts(target_kind="sqlite", sqlite_path=str(target), tables="A,B", checksum=True)
    report = convert(**op.convert_kwargs(opts, dry_run=False))
    assert [t.name for t in report.tables] == ["A", "B"]
    assert report.ok and len(dump(target, "A")) == 5


# ------------------------------------------------------------------------------- converter hooks


def test_table_started_and_progress_order(tmp_path, install_reader):
    install_reader(make_reader())
    events = []
    report = convert(
        "fake.mdb",
        str(tmp_path / "o.sqlite"),
        table_started=lambda n, i, t: events.append(("start", n, i, t)),
        progress=lambda r, i, t: events.append(("done", r.name, i, t)),
    )
    assert report.ok
    assert events == [
        ("start", "A", 1, 3),
        ("done", "A", 1, 3),
        ("start", "B", 2, 3),
        ("done", "B", 2, 3),
        ("start", "C", 3, 3),
        ("done", "C", 3, 3),
    ]


def test_cancel_never_set_changes_nothing(tmp_path, install_reader):
    install_reader(make_reader())
    a = convert("fake.mdb", str(tmp_path / "a.sqlite"), cancel=lambda: False, batch_size=2)
    install_reader(make_reader())
    b = convert("fake.mdb", str(tmp_path / "b.sqlite"), batch_size=2)
    strip = [(t.name, t.status, t.source_rows, t.target_rows) for t in a.tables]
    assert strip == [(t.name, t.status, t.source_rows, t.target_rows) for t in b.tables]


def test_cancel_before_start_cancels_every_table(tmp_path, install_reader):
    install_reader(make_reader())
    done, started = [], []
    report = convert(
        "fake.mdb",
        str(tmp_path / "o.sqlite"),
        cancel=lambda: True,
        table_started=lambda n, i, t: started.append(n),
        progress=lambda r, i, t: done.append(r.name),
    )
    assert started == []
    assert done == ["A", "B", "C"]
    assert all(t.status == "error" and t.error == CANCELLED for t in report.tables)
    assert not report.ok


def test_cancel_between_tables(tmp_path, install_reader):
    install_reader(make_reader())
    flag = threading.Event()
    report = convert(
        "fake.mdb",
        str(tmp_path / "o.sqlite"),
        cancel=flag.is_set,
        progress=lambda r, i, t: flag.set(),  # cancel after the first table
    )
    t = by_name(report)
    assert t["A"].status == "pass"
    assert t["B"].error == CANCELLED and t["C"].error == CANCELLED


def test_cancel_mid_table_between_batches(tmp_path, install_reader):
    reader = make_reader(names=("A", "B"), rows=10)
    real_iter = reader.iter_rows
    flag = threading.Event()

    def iter_rows(name, batch_size=5000):
        for i, batch in enumerate(real_iter(name, batch_size)):
            if i == 1:
                flag.set()  # pressed while the 2nd batch is read
            yield batch

    reader.iter_rows = iter_rows
    install_reader(reader)
    report = convert("fake.mdb", str(tmp_path / "o.sqlite"), batch_size=3, cancel=flag.is_set)
    t = by_name(report)
    assert t["A"].status == "error" and t["A"].error == CANCELLED
    assert t["B"].status == "error" and t["B"].error == CANCELLED


def test_cancel_with_sync_rolls_back(tmp_path, install_reader):
    target = tmp_path / "o.sqlite"
    install_reader(make_reader(names=("A",), rows=4))
    convert("fake.mdb", str(target))
    reader = FakeReader({"A": (std_info("A"), std_rows(range(1, 11), tag="x"))})
    install_reader(reader)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2  # before table: False; after batch 1: False; after batch 2: True

    report = convert("fake.mdb", str(target), mode="sync", batch_size=3, cancel=cancel)
    assert report.tables[0].error == CANCELLED
    rows = dump(target, "A")
    assert len(rows) == 4 and all("x" not in r[1] for r in rows), "sync must be rolled back"


def test_cancel_not_polled_by_dry_run(tmp_path, install_reader):
    install_reader(make_reader())
    report = convert("fake.mdb", str(tmp_path / "o.sqlite"), dry_run=True, cancel=lambda: True)
    assert all(t.status == "planned" for t in report.tables)


def test_sqlite_url_public_alias():
    assert converter._sqlite_url is converter.sqlite_url


# ------------------------------------------------------------------------------- worker


class Collector:
    def __init__(self):
        self.messages: list[wk.Message] = []
        self.finished = threading.Event()

    def __call__(self, msg):
        self.messages.append(msg)
        if msg.kind == wk.FINISHED:
            self.finished.set()

    def kinds(self):
        return [m.kind for m in self.messages]

    def of(self, kind):
        return [m.data for m in self.messages if m.kind == kind]


def _run(job, *, secret_url=None):
    sink = Collector()
    w = wk.Worker(sink)
    assert w.start("job", job, secret_url=secret_url)
    assert sink.finished.wait(10), "job did not finish"
    w.join(5)
    assert not w.busy
    return sink


def fake_report(results, dry_run=False):
    return Report("s", "t", "full", "a", "b", tables=results, dry_run=dry_run)


def test_convert_job_message_sequence():
    def fake_convert(**kw):
        assert kw["source"] == "cat.mdb"
        res = TableResult("A", 2, 2, "pass")
        kw["table_started"]("A", 1, 1)
        kw["progress"](res, 1, 1)
        assert kw["cancel"]() is False
        return fake_report([res])

    sink = _run(wk.convert_job({"source": "cat.mdb"}, convert=fake_convert))
    assert sink.kinds() == [wk.TABLE_STARTED, wk.TABLE_DONE, wk.REPORT, wk.FINISHED]
    assert sink.of(wk.TABLE_STARTED) == [("A", 1, 1)]
    assert sink.of(wk.FINISHED) == [("job", False)]


def test_worker_cancel_reaches_convert():
    entered, seen = threading.Event(), {}

    def fake_convert(**kw):
        entered.set()
        deadline = time.monotonic() + 10
        while not kw["cancel"]() and time.monotonic() < deadline:
            time.sleep(0.01)
        seen["cancelled"] = kw["cancel"]()
        return fake_report([])

    sink = Collector()
    w = wk.Worker(sink)
    w.start("convert", wk.convert_job({}, convert=fake_convert))
    assert entered.wait(5)
    assert w.busy
    assert not w.start("other", lambda ctx: None), "a second job must not start"
    w.cancel()
    assert sink.finished.wait(10)
    assert seen["cancelled"] is True
    assert sink.of(wk.FINISHED) == [("convert", True)]


def test_job_error_is_redacted_and_still_finishes():
    url = f"postgresql+psycopg://u:{SECRET}@h/db"

    def boom(ctx):
        raise RuntimeError(f"cannot connect to {url} (password {SECRET})")

    sink = _run(boom, secret_url=url)
    assert sink.kinds() == [wk.ERROR, wk.FINISHED]
    (text,) = sink.of(wk.ERROR)
    assert SECRET not in text and "RuntimeError" in text and "***" in text


def test_acade2sql_error_shown_without_type_name():
    def boom(ctx):
        raise SourceNotFoundError("Access database not found: x.mdb")

    sink = _run(boom)
    assert sink.of(wk.ERROR) == ["Access database not found: x.mdb"]


def test_error_message_masks_structural_secret_without_url():
    text = wk.error_message(ValueError("PWD=abc;UID=x"), None)
    assert "abc" not in text


def test_doctor_job_posts_code_and_text():
    def fake_run(out):
        out.write("ok  acade2sql  0.3.0\n")
        return 0

    sink = _run(wk.doctor_job(run=fake_run))
    assert sink.of(wk.DOCTOR) == [(0, "ok  acade2sql  0.3.0\n")]


def test_doctor_job_real_doctor_never_raises():
    sink = _run(wk.doctor_job())
    ((rc, text),) = sink.of(wk.DOCTOR)
    assert rc in (0, 1) and "usable readers" in text


def test_list_tables_job(monkeypatch):
    import acade2sql.readers as readers

    seen = {}

    def fake_open(path, **kw):
        seen.update(kw, path=path)
        return make_reader(names=("Z", "A"))

    monkeypatch.setattr(readers, "open_reader", fake_open)
    sink = _run(wk.list_tables_job("cat.mdb", reader="pure", copy_to_temp=True))
    assert sink.of(wk.TABLES) == [["A", "Z"]]
    assert seen == {"path": "cat.mdb", "reader": "pure", "copy_to_temp": True}


def test_list_tables_job_reader_error_is_displayed(tmp_path):
    sink = _run(wk.list_tables_job(str(tmp_path / "missing.mdb")))
    (text,) = sink.of(wk.ERROR)
    assert "not found" in text


def test_queue_log_handler_redacts():
    url = f"mssql+pyodbc://u:{SECRET}@h/db?driver=x"
    sink = Collector()
    handler = wk.QueueLogHandler(sink, lambda: url)
    logger = logging.getLogger("acade2sql.test_m7")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info("connecting", extra={"kv": {"target": url, "pw": SECRET}})
        logger.info("odbc PWD=hunter2;UID=x")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    lines = sink.of(wk.LOG)
    assert len(lines) == 2
    assert all(SECRET not in ln for ln in lines) and "hunter2" not in lines[1]
    assert "connecting" in lines[0] and "INFO" in lines[0]


def test_drain_limit_and_empty():
    q: queue.Queue = queue.Queue()
    for i in range(5):
        q.put(wk.Message(wk.LOG, str(i)))
    assert [m.data for m in wk.drain(q, limit=3)] == ["0", "1", "2"]
    assert [m.data for m in wk.drain(q)] == ["3", "4"]
    assert wk.drain(q) == []


def test_end_to_end_worker_convert(tmp_path, install_reader):
    install_reader(make_reader())
    target = tmp_path / "o.sqlite"
    kwargs = op.convert_kwargs(_opts(target_kind="sqlite", sqlite_path=str(target)), dry_run=False)
    sink = _run(wk.convert_job(kwargs))
    assert sink.kinds().count(wk.TABLE_STARTED) == 3
    assert sink.kinds().count(wk.TABLE_DONE) == 3
    (report,) = sink.of(wk.REPORT)
    assert report.ok and len(dump(target, "C")) == 5


# ------------------------------------------------------------------------------- formatting


def test_result_row_and_details():
    res = TableResult(
        "COMP", 10, 10, "fail", error="x\ny", seconds=1.25, checksum_match=False, warnings=["w"]
    )
    res.inserted, res.updated, res.deleted, res.extra_rows = 1, 2, 0, 3
    row = fmt.result_row(res)
    assert len(row) == len(fmt.RESULT_COLUMNS)
    assert row[:4] == ("COMP", "FAIL", "10", "10")
    assert row[4] == "ins=1 upd=2 del=0 extra=3"
    assert row[5:] == ("MISMATCH", "1.2", "1", "x y")
    text = fmt.details_text(res)
    assert "Warnings:\n  - w" in text and "Checksum: MISMATCH" in text


def test_result_row_dry_run_actions():
    res = TableResult("T", 3, None, "planned", actions=["drop", "create"])
    assert fmt.result_row(res)[4] == "drop,create"
    assert fmt.running_row("T")[:2] == ("T", "RUNNING")
    assert len(fmt.running_row("T")) == len(fmt.RESULT_COLUMNS)


def test_summary_text():
    rep = fake_report([TableResult("A", 2, 2, "pass"), TableResult("B", 1, None, "error")])
    assert fmt.summary_text(rep) == (
        "Conversion finished with problems: 2 tables, 1 passed, 0 failed, 1 errors, 2 target rows"
    )
    assert fmt.summary_text(rep, cancelled=True).endswith("(cancelled)")
    dry = fake_report([TableResult("A", 2, None, "planned")], dry_run=True)
    assert fmt.summary_text(dry).startswith("Dry run OK") and "1 planned" in fmt.summary_text(dry)


# ------------------------------------------------------------------------------- entry points


def test_cli_gui_subcommand_dispatches(monkeypatch):
    import acade2sql.gui as gui

    monkeypatch.setattr(gui, "main", lambda: 7)
    assert cli.main(["gui"]) == 7


def test_gui_script_entry_point_declared():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["gui-scripts"]["acade2sql-gui"] == "acade2sql.gui:main"
    assert data["project"]["scripts"]["acade2sql"] == "acade2sql.cli:main"


def test_gui_main_reports_tcl_error(monkeypatch, capsys):
    import tkinter

    import acade2sql.gui as gui
    import acade2sql.gui.app as app

    def no_display():
        raise tkinter.TclError("no display name")

    monkeypatch.setattr(app, "run", no_display)
    assert gui.main() == 1
    assert "no display name" in capsys.readouterr().err


def test_gui_package_import_does_not_import_tkinter():
    import subprocess
    import sys

    code = "import sys, acade2sql.gui, acade2sql.gui.options, acade2sql.gui.worker; "
    code += "print('tkinter' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert out.stdout.strip() == "False"


# ------------------------------------------------------------------------------- tk (needs display)


@pytest.fixture(scope="module")
def tk_root():
    """One Tk interpreter for the module (creating many in one process is flaky on Windows)."""
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk not available: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(monkeypatch, tk_root):
    import tkinter

    root = tkinter.Toplevel(tk_root)
    root.withdraw()
    from acade2sql.gui import app as app_mod

    for name in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(app_mod.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(app_mod.messagebox, "askokcancel", lambda *a, **k: True)
    a = app_mod.App(root)
    yield a
    a.detach_logging()
    try:
        root.after_cancel(a._poll_id)
        root.destroy()
    except tkinter.TclError:
        pass


def _pump(app, until, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the GUI")


def test_tk_options_roundtrip(app):
    app.v_source.set("C:/ACADE/default_cat.mdb")
    assert Path(app.v_sqlite.get()) == Path("C:/ACADE/default_cat.sqlite")
    app.v_kind.set("postgresql")
    app.v_host.set("pg")
    app.v_database.set("acade")
    app.v_user.set("u")
    app.v_password.set(SECRET)
    app.v_mode.set("sync")
    app.v_keys.set("ID")
    opts = app.options()
    assert opts.target_kind == "postgresql" and opts.server.password == SECRET
    kw = op.convert_kwargs(opts, dry_run=False)
    assert kw["key"] == ["ID"] and make_url(kw["target"]).password == SECRET
    label = app.v_target_label.get()
    assert label.startswith("Target: postgresql+psycopg://u:***@pg") and SECRET not in label


def test_tk_sync_widgets_follow_mode(app):
    app._on_mode()
    assert str(app.e_keys.cget("state")) == "disabled"
    app.v_mode.set("sync")
    app._on_mode()
    assert str(app.e_keys.cget("state")) == "normal"


def test_tk_windows_login_disables_user_password(app):
    app.v_kind.set("mssql")
    app.v_windows_auth.set(True)
    app._on_target_kind()
    assert str(app.e_password.cget("state")) == "disabled"
    app.v_kind.set("postgresql")
    app._on_target_kind()
    assert str(app.e_password.cget("state")) == "normal"


def test_tk_handles_worker_messages(app):
    app._set_busy(True)
    app.handle(wk.Message(wk.TABLE_STARTED, ("A", 1, 2)))
    (item,) = app.tree.get_children()
    assert app.tree.set(item, "status") == "RUNNING"
    res = TableResult("A", 3, 3, "pass", seconds=0.5)
    app.handle(wk.Message(wk.TABLE_DONE, (res, 1, 2)))
    assert app.tree.set(item, "status") == "PASS"
    report = fake_report([res, TableResult("B", None, None, "error", error=CANCELLED)])
    app.handle(wk.Message(wk.REPORT, report))
    assert len(app.tree.get_children()) == 2
    assert "(cancelled)" in app.v_status.get()
    app.handle(wk.Message(wk.LOG, "hello log"))
    assert "hello log" in app.t_log.get("1.0", "end")
    app.handle(wk.Message(wk.DOCTOR, (0, "doctor text")))
    assert "doctor text" in app.t_doctor.get("1.0", "end")
    app.handle(wk.Message(wk.FINISHED, ("convert", True)))
    assert str(app.b_convert.cget("state")) == "normal"
    assert str(app.b_save.cget("state")) == "normal"


def test_tk_save_report(app, tmp_path, monkeypatch):
    app.report = fake_report([TableResult("A", 1, 1, "pass")])
    out = tmp_path / "r.json"
    monkeypatch.setattr(app, "_ask_json", lambda title: str(out))
    app.save_report()
    assert out.exists() and '"A"' in out.read_text(encoding="utf-8")


def test_tk_invalid_options_do_not_start(app):
    app.run_convert(False)  # no source
    assert not app.worker.busy


def test_tk_end_to_end_dry_run_and_convert(app, tmp_path, install_reader):
    target = tmp_path / "o.sqlite"
    app.v_source.set("fake.mdb")
    app.v_sqlite.set(str(target))
    install_reader(make_reader())
    app.run_convert(True)
    _pump(app, lambda: app.report is not None and not app.worker.busy)
    assert app.report.dry_run
    assert "CREATE TABLE" in app.t_plan.get("1.0", "end")
    assert not target.exists()
    install_reader(make_reader())
    app.run_convert(False)
    _pump(app, lambda: app.report is not None and not app.report.dry_run and not app.worker.busy)
    assert app.report.ok
    statuses = [app.tree.set(i, "status") for i in app.tree.get_children()]
    assert statuses == ["PASS", "PASS", "PASS"]
    assert len(dump(target, "A")) == 5
    assert "converted table" in app.t_log.get("1.0", "end")


def test_tk_cancel_button_stops_run(app, tmp_path, install_reader):
    reader = make_reader(rows=10)
    real_iter = reader.iter_rows
    entered, release = threading.Event(), threading.Event()

    def slow_iter(name, batch_size=5000):
        for batch in real_iter(name, batch_size):
            entered.set()
            release.wait(10)  # hold the worker inside the first table until Cancel
            yield batch

    reader.iter_rows = slow_iter
    install_reader(reader)
    app.v_source.set("fake.mdb")
    app.v_sqlite.set(str(tmp_path / "o.sqlite"))
    app.v_batch.set("3")
    app.run_convert(False)
    _pump(app, entered.is_set)
    assert str(app.b_cancel.cget("state")) == "normal"
    app.cancel()
    release.set()
    _pump(app, lambda: app.report is not None and not app.worker.busy)
    assert [t.error for t in app.report.tables] == [CANCELLED] * 3
    assert "(cancelled)" in app.v_status.get()
    assert str(app.b_cancel.cget("state")) == "disabled"


def test_tk_detach_restores_logging(app):
    logger = logging.getLogger("acade2sql")
    saved = app._saved_logging
    assert app._log_handler in logger.handlers and logger.level == logging.INFO
    app.detach_logging()
    assert app._log_handler not in logger.handlers
    assert (logger.level, logger.propagate) == saved
