"""The acade2sql main window (tkinter/ttk).

Layout, top to bottom: Source, Target, Options, the action bar (Check setup / List tables /
Dry run / Convert / Cancel / Save report, progress bar, status line) and a notebook with the
Results, Plan / DDL, Setup check and Log tabs.

All work runs on a :class:`~acade2sql.gui.worker.Worker` thread; this module only builds
widgets, turns them into :class:`~acade2sql.gui.options.GuiOptions` and renders the worker's
messages, which it drains from a queue on a Tk timer.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from acade2sql import __version__
from acade2sql.gui import format as fmt
from acade2sql.gui import worker as wk
from acade2sql.gui.options import (
    MSSQL_DRIVERS,
    PG_SSLMODES,
    TARGET_KINDS,
    TARGET_LABELS,
    GuiOptions,
    OptionsError,
    ServerFields,
    convert_kwargs,
    default_sqlite_path,
    display_target,
)
from acade2sql.keys import NO_KEY_CHOICES
from acade2sql.readers import READER_CHOICES
from acade2sql.redact import mask_url
from acade2sql.validate import Report
from acade2sql.writers.sqlalchemy_writer import MODES

log = logging.getLogger(__name__)

POLL_MS = 100
ACCESS_TYPES = [("Access databases", "*.mdb *.accdb"), ("All files", "*.*")]
SQLITE_TYPES = [("SQLite databases", "*.sqlite *.db *.sqlite3"), ("All files", "*.*")]
JSON_TYPES = [("JSON report", "*.json"), ("All files", "*.*")]
PAD = {"padx": 4, "pady": 2}


class App:
    """Builds the window on ``root`` and wires it to a background worker."""

    def __init__(self, root: tk.Tk | tk.Toplevel) -> None:
        self.root = root
        self.queue: queue.Queue[wk.Message] = queue.Queue()
        self.worker = wk.Worker(self.queue.put)
        self.report: Report | None = None
        self._job_url: str | None = None  # target URL of the running job, for redaction
        self._rows: dict[str, str] = {}  # table name -> results tree item id
        self._tables_dialog: tk.Toplevel | None = None
        self._log_handler = wk.QueueLogHandler(self.queue.put, lambda: self._job_url)
        root.title(f"acade2sql {__version__} - Access to SQL")
        root.minsize(900, 640)
        self._vars()
        self._build()
        self._install_logging()
        self._on_target_kind()
        self._on_mode()
        self._update_target_label()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(POLL_MS, self._poll)

    # -- state -------------------------------------------------------------------------------

    def _vars(self) -> None:
        sv, bv = tk.StringVar, tk.BooleanVar
        self.v_source = sv()
        self.v_kind = sv(value="sqlite")
        self.v_sqlite = sv()
        self.v_host, self.v_port, self.v_instance = sv(), sv(), sv()
        self.v_database, self.v_user, self.v_password = sv(), sv(), sv()
        self.v_driver = sv(value=MSSQL_DRIVERS[0])
        self.v_windows_auth, self.v_trust_cert = bv(), bv()
        self.v_sslmode = sv()
        self.v_url = sv()
        self.v_show_url = bv()
        self.v_mode = sv(value="full")
        self.v_reader = sv(value="auto")
        self.v_copy_to_temp, self.v_checksum = bv(), bv()
        self.v_counts = bv(value=True)
        self.v_verbose = bv()
        self.v_report = sv()
        self.v_schema = sv()
        self.v_tables, self.v_exclude = sv(), sv()
        self.v_batch = sv(value="5000")
        self.v_keys = sv()
        self.v_no_key = sv(value="error")
        self.v_delete_missing = bv()
        self.v_target_label = sv()
        self.v_status = sv(value="Ready.")
        self._sqlite_auto = True  # SQLite path follows the source until the user edits it
        for var in (
            self.v_kind, self.v_sqlite, self.v_host, self.v_port, self.v_instance,
            self.v_database, self.v_user, self.v_password, self.v_driver, self.v_windows_auth,
            self.v_trust_cert, self.v_sslmode, self.v_url,
        ):  # fmt: skip
            var.trace_add("write", lambda *_: self._update_target_label())
        self.v_source.trace_add("write", lambda *_: self._on_source_changed())

    def options(self) -> GuiOptions:
        """The current form values."""
        server = ServerFields(
            host=self.v_host.get(),
            port=self.v_port.get(),
            database=self.v_database.get(),
            user=self.v_user.get(),
            password=self.v_password.get(),
            instance=self.v_instance.get(),
            driver=self.v_driver.get(),
            windows_auth=self.v_windows_auth.get(),
            trust_server_certificate=self.v_trust_cert.get(),
            sslmode=self.v_sslmode.get(),
        )
        return GuiOptions(
            source=self.v_source.get(),
            target_kind=self.v_kind.get(),  # type: ignore[arg-type]
            sqlite_path=self.v_sqlite.get(),
            server=server,
            url=self.v_url.get(),
            mode=self.v_mode.get(),
            reader=self.v_reader.get(),
            copy_to_temp=self.v_copy_to_temp.get(),
            checksum=self.v_checksum.get(),
            counts=self.v_counts.get(),
            report_path=self.v_report.get(),
            schema=self.v_schema.get(),
            tables=self.v_tables.get(),
            exclude=self.v_exclude.get(),
            batch_size=self.v_batch.get(),
            keys=self.v_keys.get(),
            no_key=self.v_no_key.get(),
            delete_missing=self.v_delete_missing.get(),
        )

    # -- layout ------------------------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        self._build_source(outer).grid(row=0, column=0, sticky="ew")
        self._build_target(outer).grid(row=1, column=0, sticky="ew")
        self._build_options(outer).grid(row=2, column=0, sticky="ew")
        self._build_actions(outer).grid(row=3, column=0, sticky="ew", pady=(4, 2))
        self._build_notebook(outer).grid(row=4, column=0, sticky="nsew")
        outer.rowconfigure(4, weight=1)

    def _build_source(self, parent: ttk.Frame) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="Source (Access database, opened read-only)")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="File:").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.v_source).grid(row=0, column=1, sticky="ew", **PAD)
        ttk.Button(box, text="Browse...", command=self._browse_source).grid(row=0, column=2, **PAD)
        return box

    def _build_target(self, parent: ttk.Frame) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="Target")
        box.columnconfigure(0, weight=1)
        kinds = ttk.Frame(box)
        kinds.grid(row=0, column=0, sticky="w", **PAD)
        for kind in TARGET_KINDS:
            ttk.Radiobutton(
                kinds,
                text=TARGET_LABELS[kind],
                value=kind,
                variable=self.v_kind,
                command=self._on_target_kind,
            ).pack(side="left", padx=(0, 12))
        self.f_sqlite = self._build_sqlite_frame(box)
        self.f_server = self._build_server_frame(box)
        self.f_url = self._build_url_frame(box)
        for frame in (self.f_sqlite, self.f_server, self.f_url):
            frame.grid(row=1, column=0, sticky="ew")
        ttk.Label(box, textvariable=self.v_target_label, foreground="#555").grid(
            row=2, column=0, sticky="w", **PAD
        )
        return box

    def _build_sqlite_frame(self, parent: ttk.LabelFrame) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="SQLite file:").grid(row=0, column=0, sticky="w", **PAD)
        entry = ttk.Entry(f, textvariable=self.v_sqlite)
        entry.grid(row=0, column=1, sticky="ew", **PAD)
        entry.bind("<Key>", lambda _e: setattr(self, "_sqlite_auto", False))
        ttk.Button(f, text="Save as...", command=self._browse_sqlite).grid(row=0, column=2, **PAD)
        return f

    def _build_server_frame(self, parent: ttk.LabelFrame) -> ttk.Frame:
        f = ttk.Frame(parent)
        for col in (1, 3, 5):
            f.columnconfigure(col, weight=1)

        def field(row: int, col: int, label: str, var: tk.StringVar, **kw: Any) -> ttk.Entry:
            ttk.Label(f, text=label).grid(row=row, column=col, sticky="w", **PAD)
            e = ttk.Entry(f, textvariable=var, **kw)
            e.grid(row=row, column=col + 1, sticky="ew", **PAD)
            return e

        field(0, 0, "Host:", self.v_host)
        self.e_instance = field(0, 2, "Instance:", self.v_instance, width=14)
        field(0, 4, "Port:", self.v_port, width=7)
        field(1, 0, "Database:", self.v_database)
        self.e_user = field(1, 2, "User:", self.v_user, width=14)
        self.e_password = field(1, 4, "Password:", self.v_password, show="•", width=14)
        self.l_driver = ttk.Label(f, text="ODBC driver:")
        self.l_driver.grid(row=2, column=0, sticky="w", **PAD)
        self.c_driver = ttk.Combobox(f, textvariable=self.v_driver, values=MSSQL_DRIVERS)
        self.c_driver.grid(row=2, column=1, sticky="ew", **PAD)
        self.k_windows = ttk.Checkbutton(
            f, text="Windows login", variable=self.v_windows_auth, command=self._on_target_kind
        )
        self.k_windows.grid(row=2, column=2, columnspan=2, sticky="w", **PAD)
        self.k_trust = ttk.Checkbutton(
            f, text="Trust server certificate", variable=self.v_trust_cert
        )
        self.k_trust.grid(row=2, column=4, columnspan=2, sticky="w", **PAD)
        self.l_sslmode = ttk.Label(f, text="SSL mode:")
        self.l_sslmode.grid(row=2, column=0, sticky="w", **PAD)
        self.c_sslmode = ttk.Combobox(
            f, textvariable=self.v_sslmode, values=PG_SSLMODES, state="readonly", width=12
        )
        self.c_sslmode.grid(row=2, column=1, sticky="w", **PAD)
        return f

    def _build_url_frame(self, parent: ttk.LabelFrame) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="SQLAlchemy URL:").grid(row=0, column=0, sticky="w", **PAD)
        self.e_url = ttk.Entry(f, textvariable=self.v_url, show="•")
        self.e_url.grid(row=0, column=1, sticky="ew", **PAD)
        ttk.Checkbutton(
            f,
            text="Show",
            variable=self.v_show_url,
            command=lambda: self.e_url.configure(show="" if self.v_show_url.get() else "•"),
        ).grid(row=0, column=2, **PAD)
        return f

    def _build_options(self, parent: ttk.Frame) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="Options")
        for col in (1, 3, 5):
            box.columnconfigure(col, weight=1)
        ttk.Label(box, text="Mode:").grid(row=0, column=0, sticky="w", **PAD)
        mode = ttk.Combobox(box, textvariable=self.v_mode, values=MODES, state="readonly", width=10)
        mode.grid(row=0, column=1, sticky="w", **PAD)
        mode.bind("<<ComboboxSelected>>", lambda _e: self._on_mode())
        ttk.Label(box, text="Reader:").grid(row=0, column=2, sticky="w", **PAD)
        ttk.Combobox(
            box, textvariable=self.v_reader, values=READER_CHOICES, state="readonly", width=8
        ).grid(row=0, column=3, sticky="w", **PAD)
        ttk.Label(box, text="Batch size:").grid(row=0, column=4, sticky="w", **PAD)
        ttk.Spinbox(
            box, textvariable=self.v_batch, from_=1, to=1_000_000, increment=1000, width=9
        ).grid(row=0, column=5, sticky="w", **PAD)

        checks = ttk.Frame(box)
        checks.grid(row=1, column=0, columnspan=6, sticky="w", **PAD)
        for text, var in (
            ("Copy source to temp", self.v_copy_to_temp),
            ("Checksum (re-reads every table)", self.v_checksum),
            ("Count rows in dry run", self.v_counts),
            ("Verbose log", self.v_verbose),
        ):
            ttk.Checkbutton(checks, text=text, variable=var).pack(side="left", padx=(0, 12))
        self.v_verbose.trace_add("write", lambda *_: self._apply_log_level())

        ttk.Label(box, text="Tables:").grid(row=2, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.v_tables).grid(
            row=2, column=1, columnspan=2, sticky="ew", **PAD
        )
        ttk.Label(box, text="Exclude:").grid(row=2, column=3, sticky="e", **PAD)
        ttk.Entry(box, textvariable=self.v_exclude).grid(
            row=2, column=4, columnspan=2, sticky="ew", **PAD
        )
        ttk.Label(box, text="Schema:").grid(row=3, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.v_schema, width=16).grid(
            row=3, column=1, sticky="w", **PAD
        )
        ttk.Label(box, text="Report JSON:").grid(row=3, column=2, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.v_report).grid(
            row=3, column=3, columnspan=2, sticky="ew", **PAD
        )
        ttk.Button(box, text="Browse...", command=self._browse_report).grid(
            row=3, column=5, sticky="w", **PAD
        )

        self.f_sync = ttk.Frame(box)
        self.f_sync.grid(row=4, column=0, columnspan=6, sticky="ew")
        self.f_sync.columnconfigure(1, weight=1)
        ttk.Label(self.f_sync, text="Sync key(s):").grid(row=0, column=0, sticky="w", **PAD)
        self.e_keys = ttk.Entry(self.f_sync, textvariable=self.v_keys)
        self.e_keys.grid(row=0, column=1, sticky="ew", **PAD)
        ttk.Label(self.f_sync, text="Table without key:").grid(row=0, column=2, **PAD)
        self.c_no_key = ttk.Combobox(
            self.f_sync, textvariable=self.v_no_key, values=NO_KEY_CHOICES, width=8,
            state="readonly",
        )  # fmt: skip
        self.c_no_key.grid(row=0, column=3, **PAD)
        self.k_delete = ttk.Checkbutton(
            self.f_sync, text="Delete missing rows", variable=self.v_delete_missing
        )
        self.k_delete.grid(row=0, column=4, **PAD)
        ttk.Label(
            box,
            text="Table names: comma-separated. Sync keys: COL[,COL] or TABLE=COL[,COL], "
            "separated by ';' (default: primary key).",
            foreground="#555",
        ).grid(row=5, column=0, columnspan=6, sticky="w", **PAD)
        return box

    def _build_actions(self, parent: ttk.Frame) -> ttk.Frame:
        bar = ttk.Frame(parent)
        self.b_doctor = ttk.Button(bar, text="Check setup", command=self.check_setup)
        self.b_list = ttk.Button(bar, text="List tables...", command=self.list_tables)
        self.b_dry = ttk.Button(bar, text="Dry run", command=lambda: self.run_convert(True))
        self.b_convert = ttk.Button(bar, text="Convert", command=lambda: self.run_convert(False))
        self.b_cancel = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.b_save = ttk.Button(
            bar, text="Save report...", command=self.save_report, state="disabled"
        )
        for b in (self.b_doctor, self.b_list, self.b_dry, self.b_convert, self.b_cancel):
            b.pack(side="left", padx=(0, 4))
        self.b_save.pack(side="left", padx=(12, 4))
        self.progress = ttk.Progressbar(bar, mode="determinate", length=180)
        self.progress.pack(side="left", padx=8)
        ttk.Label(bar, textvariable=self.v_status).pack(side="left", fill="x", expand=True)
        return bar

    def _build_notebook(self, parent: ttk.Frame) -> ttk.Notebook:
        nb = ttk.Notebook(parent)
        self.notebook = nb
        res = ttk.Frame(nb)
        cols = [c[0] for c in fmt.RESULT_COLUMNS]
        self.tree = ttk.Treeview(res, columns=cols, show="headings", selectmode="browse")
        for cid, heading, width in fmt.RESULT_COLUMNS:
            self.tree.heading(cid, text=heading)
            self.tree.column(cid, width=width, stretch=cid in ("table", "error"))
        for status, colour in (("FAIL", "#b36b00"), ("ERROR", "#b00020"), ("RUNNING", "#1f5fbf")):
            self.tree.tag_configure(status, foreground=colour)
        self.tree.bind("<Double-1>", lambda _e: self._show_details())
        self.tree.bind("<Return>", lambda _e: self._show_details())
        _scrolled(res, self.tree)
        nb.add(res, text="Results")
        self.t_plan = self._text_tab(nb, "Plan / DDL")
        self.t_doctor = self._text_tab(nb, "Setup check")
        self.t_log = self._text_tab(nb, "Log")
        return nb

    def _text_tab(self, nb: ttk.Notebook, title: str) -> tk.Text:
        frame = ttk.Frame(nb)
        text = tk.Text(frame, wrap="none", height=12, font=("Consolas", 9), state="disabled")
        _scrolled(frame, text)
        nb.add(frame, text=title)
        return text

    # -- logging -----------------------------------------------------------------------------

    def _install_logging(self) -> None:
        logger = logging.getLogger("acade2sql")
        self._saved_logging = (logger.level, logger.propagate)
        logger.propagate = False
        if self._log_handler not in logger.handlers:
            logger.addHandler(self._log_handler)
        self._apply_log_level()

    def detach_logging(self) -> None:
        """Remove the log handler and restore the ``acade2sql`` logger's level/propagation."""
        logger = logging.getLogger("acade2sql")
        logger.removeHandler(self._log_handler)
        logger.setLevel(self._saved_logging[0])
        logger.propagate = self._saved_logging[1]

    def _apply_log_level(self) -> None:
        level = logging.DEBUG if self.v_verbose.get() else logging.INFO
        logging.getLogger("acade2sql").setLevel(level)

    # -- reactions ---------------------------------------------------------------------------

    def _on_source_changed(self) -> None:
        if self._sqlite_auto:
            self.v_sqlite.set(default_sqlite_path(self.v_source.get()))

    def _on_target_kind(self) -> None:
        kind = self.v_kind.get()
        frames = {"sqlite": self.f_sqlite, "mssql": self.f_server, "postgresql": self.f_server}
        shown = frames.get(kind, self.f_url)
        for frame in (self.f_sqlite, self.f_server, self.f_url):
            if frame is shown:
                frame.grid()
            else:
                frame.grid_remove()
        mssql = kind == "mssql"
        for w in (self.l_driver, self.c_driver, self.k_windows, self.k_trust):
            w.grid() if mssql else w.grid_remove()
        for w in (self.l_sslmode, self.c_sslmode):
            w.grid() if kind == "postgresql" else w.grid_remove()
        self.e_instance.configure(state="normal" if mssql else "disabled")
        login = "disabled" if mssql and self.v_windows_auth.get() else "normal"
        self.e_user.configure(state=login)
        self.e_password.configure(state=login)
        self._update_target_label()

    def _on_mode(self) -> None:
        state = "normal" if self.v_mode.get() == "sync" else "disabled"
        self.e_keys.configure(state=state)
        self.c_no_key.configure(state="readonly" if state == "normal" else "disabled")
        self.k_delete.configure(state=state)

    def _update_target_label(self) -> None:
        self.v_target_label.set("Target: " + display_target(self.options()))

    # -- dialogs -----------------------------------------------------------------------------

    def _browse_source(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Access database", filetypes=ACCESS_TYPES
        )
        if path:
            self.v_source.set(str(Path(path)))

    def _browse_sqlite(self) -> None:
        current = Path(self.v_sqlite.get()) if self.v_sqlite.get() else None
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="SQLite target file",
            filetypes=SQLITE_TYPES,
            defaultextension=".sqlite",
            initialdir=str(current.parent) if current else None,
            initialfile=current.name if current else None,
            confirmoverwrite=False,  # an existing file is fine (append / sync)
        )
        if path:
            self._sqlite_auto = False
            self.v_sqlite.set(str(Path(path)))

    def _browse_report(self) -> None:
        path = self._ask_json("Report file")
        if path:
            self.v_report.set(path)

    def _ask_json(self, title: str) -> str:
        path = filedialog.asksaveasfilename(
            parent=self.root, title=title, filetypes=JSON_TYPES, defaultextension=".json"
        )
        return str(Path(path)) if path else ""

    def _show_details(self) -> None:
        sel = self.tree.selection()
        if not sel or self.report is None:
            return
        name = self.tree.set(sel[0], "table")
        for res in self.report.tables:
            if res.name == name:
                messagebox.showinfo(f"Table {name}", fmt.details_text(res), parent=self.root)
                return

    # -- actions -----------------------------------------------------------------------------

    def _start(self, name: str, job: wk.Job, status: str, url: str | None = None) -> bool:
        self._job_url = url
        if not self.worker.start(name, job, secret_url=url):
            messagebox.showwarning("Busy", "Another task is still running.", parent=self.root)
            return False
        self._set_busy(True)
        self.v_status.set(status)
        return True

    def check_setup(self) -> None:
        self._start("doctor", wk.doctor_job(), "Checking setup...")

    def list_tables(self) -> None:
        opts = self.options()
        if not opts.source.strip():
            messagebox.showerror("acade2sql", "Choose a source database first.", parent=self.root)
            return
        job = wk.list_tables_job(
            opts.source.strip(), reader=opts.reader, copy_to_temp=opts.copy_to_temp
        )
        self._start("tables", job, "Reading table list...")

    def run_convert(self, dry_run: bool) -> None:
        try:
            kwargs = convert_kwargs(self.options(), dry_run=dry_run)
        except OptionsError as exc:
            messagebox.showerror("acade2sql", str(exc), parent=self.root)
            return
        url = kwargs["target"]
        if not dry_run and kwargs["mode"] == "full" and not self._confirm_full(url):
            return
        self._sqlite_auto = False  # keep the path that was used
        self._clear_results()
        what = "Planning (dry run)" if dry_run else "Converting"
        name = "dry_run" if dry_run else "convert"
        if self._start(name, wk.convert_job(kwargs), f"{what}...", url):
            self._append(self.t_log, f"--- {what}: {kwargs['source']} -> {mask_url(url)}\n")

    def _confirm_full(self, url: str) -> bool:
        """Ask before ``full`` mode drops tables; not asked for a new SQLite file."""
        if self.v_kind.get() == "sqlite" and not Path(self.v_sqlite.get().strip()).exists():
            return True
        return messagebox.askokcancel(
            "Full conversion",
            "Mode 'full' drops and re-creates the converted tables in\n"
            f"{mask_url(url)}\n\nContinue?",
            parent=self.root,
        )

    def cancel(self) -> None:
        if self.worker.busy:
            self.worker.cancel()
            self.v_status.set("Cancelling (after the current batch)...")
            self.b_cancel.configure(state="disabled")

    def save_report(self) -> None:
        if self.report is None:
            return
        path = self._ask_json("Save report")
        if not path:
            return
        try:
            self.report.write_json(path)
        except OSError as exc:
            messagebox.showerror("Save report", f"Cannot write {path}: {exc}", parent=self.root)
            return
        self.v_status.set(f"Report saved to {path}")

    def close(self) -> None:
        if self.worker.busy:
            if not messagebox.askyesno(
                "Quit", "A task is still running. Cancel it and quit?", parent=self.root
            ):
                return
            self.worker.cancel()
            self.worker.join(timeout=5)
        self.detach_logging()
        self.root.after_cancel(self._poll_id)
        self.root.destroy()

    # -- worker messages ---------------------------------------------------------------------

    def _poll(self) -> None:
        for msg in wk.drain(self.queue):
            try:
                self.handle(msg)
            except Exception:  # a rendering bug must not stop the poll loop
                log.exception("cannot handle GUI message %s", msg.kind)
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def handle(self, msg: wk.Message) -> None:
        """Render one worker message (main thread only)."""
        kind, data = msg.kind, msg.data
        if kind == wk.LOG:
            self._append(self.t_log, data + "\n")
        elif kind == wk.TABLE_STARTED:
            name, index, total = data
            self._set_row(name, fmt.running_row(name), "RUNNING")
            self.progress.configure(maximum=total, value=index - 1)
            self.v_status.set(f"[{index}/{total}] {name}...")
        elif kind == wk.TABLE_DONE:
            res, index, total = data
            self._set_row(res.name, fmt.result_row(res), res.status.upper())
            self.progress.configure(maximum=total, value=index)
        elif kind == wk.REPORT:
            self._show_report(data)
        elif kind == wk.DOCTOR:
            rc, text = data
            self._replace(self.t_doctor, text)
            self.notebook.select(2)
            self.v_status.set("Setup OK." if rc == 0 else "Setup problem: no usable Access reader.")
        elif kind == wk.TABLES:
            self._show_tables_dialog(data)
            self.v_status.set(f"{len(data)} tables in the source.")
        elif kind == wk.ERROR:
            self._append(self.t_log, f"ERROR {data}\n")
            self.v_status.set("Failed: " + data.splitlines()[0][:200] if data else "Failed.")
            messagebox.showerror("acade2sql", data, parent=self.root)
        elif kind == wk.FINISHED:
            self._set_busy(False)
            self._job_url = None

    def _show_report(self, report: Report) -> None:
        self.report = report
        for res in report.tables:  # rows the progress hook never sent (dry run, schema errors)
            self._set_row(res.name, fmt.result_row(res), res.status.upper())
        self.progress.configure(maximum=max(len(report.tables), 1), value=len(report.tables))
        if report.dry_run:
            self._replace(self.t_plan, report.render_text() + "\n")
            self.notebook.select(1)
        else:
            self.notebook.select(0)
        cancelled = any(t.error == "cancelled by user" for t in report.tables)
        self.v_status.set(fmt.summary_text(report, cancelled=cancelled))
        self._append(self.t_log, fmt.summary_text(report, cancelled=cancelled) + "\n")
        self.b_save.configure(state="normal")

    def _show_tables_dialog(self, names: list[str]) -> None:
        if self._tables_dialog is not None and self._tables_dialog.winfo_exists():
            self._tables_dialog.destroy()
        dlg = tk.Toplevel(self.root)
        self._tables_dialog = dlg
        dlg.title(f"Tables ({len(names)})")
        dlg.transient(self.root)
        frame = ttk.Frame(dlg, padding=6)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Select tables (Ctrl/Shift-click for several):").pack(anchor="w")
        inner = ttk.Frame(frame)
        inner.pack(fill="both", expand=True, pady=4)
        lb = tk.Listbox(inner, selectmode="extended", height=20, width=48, exportselection=False)
        for n in names:
            lb.insert("end", n)
        _scrolled(inner, lb)

        def chosen() -> str:
            return ", ".join(lb.get(i) for i in lb.curselection())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        for text, var in (("Convert only these", self.v_tables), ("Exclude these", self.v_exclude)):
            ttk.Button(buttons, text=text, command=lambda v=var: v.set(chosen())).pack(
                side="left", padx=(0, 4)
            )
        ttk.Button(buttons, text="Close", command=dlg.destroy).pack(side="right")

    # -- widget helpers ----------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for b in (self.b_doctor, self.b_list, self.b_dry, self.b_convert):
            b.configure(state=state)
        self.b_cancel.configure(state="normal" if busy else "disabled")
        if busy:
            self.b_save.configure(state="disabled")
        elif self.report is not None:
            self.b_save.configure(state="normal")

    def _clear_results(self) -> None:
        self.report = None
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self.progress.configure(value=0)

    def _set_row(self, name: str, values: tuple[str, ...], tag: str) -> None:
        item = self._rows.get(name)
        if item is None or not self.tree.exists(item):
            self._rows[name] = self.tree.insert("", "end", values=values, tags=(tag,))
            self.tree.see(self._rows[name])
        else:
            self.tree.item(item, values=values, tags=(tag,))

    @staticmethod
    def _append(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    @staticmethod
    def _replace(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")


def _scrolled(parent: tk.Misc, widget: Any) -> None:
    """Grid ``widget`` with vertical and horizontal scrollbars into ``parent``."""
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    widget.grid(row=0, column=0, sticky="nsew")
    ys = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
    ys.grid(row=0, column=1, sticky="ns")
    widget.configure(yscrollcommand=ys.set)
    if hasattr(widget, "xview"):
        xs = ttk.Scrollbar(parent, orient="horizontal", command=widget.xview)
        xs.grid(row=1, column=0, sticky="ew")
        widget.configure(xscrollcommand=xs.set)


def _enable_dpi_awareness() -> None:
    """Crisp text on scaled Windows displays (no-op elsewhere)."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def run() -> int:
    """Create the root window and run the Tk main loop until it is closed."""
    _enable_dpi_awareness()
    root = tk.Tk()
    with contextlib.suppress(tk.TclError):
        ttk.Style(root).theme_use("vista")
    App(root)
    root.mainloop()
    return 0
