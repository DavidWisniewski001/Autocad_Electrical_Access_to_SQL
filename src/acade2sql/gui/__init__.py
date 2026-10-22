"""Desktop GUI (tkinter/ttk) for acade2sql: ``acade2sql-gui`` or ``acade2sql gui``.

The window drives the same Python API as the CLI (:func:`acade2sql.convert`,
:mod:`acade2sql.doctor`, :func:`acade2sql.open_reader`); it adds no conversion logic. tkinter is
imported only when the window is opened, so importing this package needs no display.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Open the main window; returns the exit code (1 when Tk cannot start)."""
    try:
        import tkinter

        from acade2sql.gui.app import run
    except ImportError as exc:  # Python built without tkinter
        _fail(f"tkinter is not available in this Python ({exc}); install Python with Tcl/Tk.")
        return 1
    try:
        return run()
    except tkinter.TclError as exc:  # e.g. no display
        _fail(f"cannot open the acade2sql window: {exc}")
        return 1


def _fail(text: str) -> None:
    stream = sys.stderr
    if stream is not None:  # pythonw / gui-scripts have no console
        stream.write(f"error: {text}\n")
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "acade2sql", 0x10)  # type: ignore[attr-defined]
    except Exception:
        pass
