from __future__ import annotations

import struct

import pyodbc
import pytest

from acade2sql import diagnostics
from acade2sql.diagnostics import (
    ACCESS_DRIVER_NAMES,
    diagnose_driver,
    require_driver,
    translate_connect_error,
)
from acade2sql.errors import (
    Acade2SqlError,
    BitnessMismatchError,
    DriverNotFoundError,
)

BITS = struct.calcsize("P") * 8


def test_no_driver_not_ok_and_message_actionable():
    st = diagnose_driver([])
    assert not st.ok
    assert st.python_bits == BITS
    assert f"{BITS}-bit" in st.message
    assert "Microsoft Access Database Engine" in st.message
    assert st.access_drivers == []


def test_unrelated_drivers_not_ok():
    st = diagnose_driver(["SQL Server", "SQLite3 ODBC Driver"])
    assert not st.ok


def test_driver_present_ok():
    st = diagnose_driver([ACCESS_DRIVER_NAMES[0], "SQL Server"])
    assert st.ok
    assert ACCESS_DRIVER_NAMES[0] in st.access_drivers
    assert st.python_bits == BITS


def test_driver_names_include_standard():
    assert "Microsoft Access Driver (*.mdb, *.accdb)" in ACCESS_DRIVER_NAMES


def test_require_driver_raises_when_missing(monkeypatch):
    monkeypatch.setattr(pyodbc, "drivers", lambda: [])
    monkeypatch.setattr(diagnostics, "_other_bitness_has_driver", lambda: False)
    with pytest.raises(DriverNotFoundError) as ei:
        require_driver()
    assert "Microsoft Access Database Engine" in str(ei.value)
    assert f"{BITS}-bit" in str(ei.value)


def test_require_driver_returns_name(monkeypatch):
    monkeypatch.setattr(pyodbc, "drivers", lambda: [ACCESS_DRIVER_NAMES[0]])
    assert require_driver() == ACCESS_DRIVER_NAMES[0]


def test_translate_im002_driver_not_found():
    err = translate_connect_error(pyodbc.Error("IM002", "[IM002] Data source name not found"))
    assert isinstance(err, DriverNotFoundError)
    assert not isinstance(err, BitnessMismatchError)
    assert "Microsoft Access Database Engine" in str(err)
    assert f"{BITS}-bit" in str(err)


def test_translate_im014_bitness():
    err = translate_connect_error(
        pyodbc.Error("IM014", "[IM014] The specified DSN contains an architecture mismatch")
    )
    assert isinstance(err, BitnessMismatchError)
    assert isinstance(err, DriverNotFoundError)
    assert "Microsoft Access Database Engine" in str(err)


def test_translate_other_error_is_acade2sql_error():
    err = translate_connect_error(pyodbc.Error("HY000", "[HY000] something odd"))
    assert isinstance(err, Acade2SqlError)
    assert not isinstance(err, DriverNotFoundError)
