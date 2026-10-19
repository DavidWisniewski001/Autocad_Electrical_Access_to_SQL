"""Build synthetic ACADE-like Access databases for the test-suite.

Empty files are created through the ODBC installer API (odbccp32), then
filled through pyodbc with Access DDL.  Windows + Access driver only.

Findings (driver quirks):
* ``CREATE_DB`` + ``.mdb`` gives an ACE-format .mdb (header "Standard ACE DB");
  ``CREATE_DBV12`` + ``.accdb`` gives an .accdb; ``CREATE_DBV4`` gives a true
  Jet 4 .mdb.  ``CREATE_DB`` with ``.accdb`` returns success but creates nothing.
* ``DECIMAL(p,s)`` DDL only works when the connection uses ``ExtendedAnsiSQL=1``;
  in that mode CATALOG and DEC become reserved words and must be bracket-quoted.
"""

# ruff: noqa: E501, RUF001, RUF005
from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import decimal
import os
from ctypes import wintypes
from pathlib import Path

DRIVER = "Microsoft Access Driver (*.mdb, *.accdb)"
D = decimal.Decimal

BIG_ROWS = 12_345
CATALOG_ROWS = 40


class FixtureError(RuntimeError):
    pass


def create_empty_db(path: Path) -> None:
    path = Path(path)
    ext = path.suffix.lower()
    keyword = "CREATE_DBV12" if ext == ".accdb" else "CREATE_DB"
    fn = ctypes.windll.odbccp32.SQLConfigDataSourceW
    fn.argtypes = [wintypes.HWND, wintypes.WORD, wintypes.LPCWSTR, ctypes.c_void_p]
    fn.restype = wintypes.BOOL
    attrs = f'{keyword}="{path}" General\0\0'
    buf = ctypes.create_unicode_buffer(attrs, len(attrs) + 2)
    ok = fn(None, 1, DRIVER, ctypes.cast(buf, ctypes.c_void_p))
    if not ok or not path.exists():
        msgs = []
        err = ctypes.create_unicode_buffer(512)
        code = wintypes.DWORD()
        n = wintypes.WORD()
        for i in range(1, 5):
            rc = ctypes.windll.odbccp32.SQLInstallerErrorW(
                i, ctypes.byref(code), err, 512, ctypes.byref(n)
            )
            if rc not in (0, 1):
                break
            msgs.append(f"{code.value}: {err.value}")
        raise FixtureError(f"could not create {path} (rc={ok}): {msgs}")


def connect_rw(path: Path):
    import pyodbc

    return pyodbc.connect(
        f"Driver={{{DRIVER}}};Dbq={path};ExtendedAnsiSQL=1;", autocommit=True
    )


def driver_available() -> bool:
    try:
        import pyodbc
    except ImportError:
        return False
    return DRIVER in pyodbc.drivers()


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------

# Column MFG/DESC1/NOTES of the first catalog rows carry these hostile values.
SPECIAL_TEXT = [
    "  ABC  ",  # padded
    "µ · – Ω °",  # micro, middle dot, en dash, Omega, degree
    "日本語テキスト",  # CJK
    "emoji \U0001F600 ok",  # non-BMP
    "line1\r\nline2",  # CR/LF
    "",  # empty string
    None,  # NULL
]

LONG_MEMO = ("0123456789abcdef" * 400) + "\r\nEND-OF-MEMO"  # > 6400 chars


def _catalog_rows(prefix: str):
    """rows: (CATALOG, ASSEMBLYCODE, MFG, DESC1, NOTES); RECNUM is added by the DB."""
    rows = []
    for i in range(CATALOG_ROWS):
        rows.append((f"{prefix}-{i:03d}", f"AS{i % 5}", "ACME", f"item {i}", None))
    for j, v in enumerate(SPECIAL_TEXT):
        rows[j] = (f"{prefix}-{j:03d}", "AS0", v, v, v)
    rows[7] = (f"{prefix}-007", "AS1", "MEMO", "memo row", LONG_MEMO)
    return rows


TYPED_ROWS = [
    # L, S, B(byte), DBL, REAL, CUR, DT, YESNO, GUID, DEC, BIN, TXT
    (1, -32768, 0, 1.5e300, 3.5, D("1234.5678"), dt.datetime(1900, 1, 1), True,
     "{12345678-1234-1234-1234-123456789ABC}", D("12345678901234.1234"),
     b"\x00\x01\xff", "µΩ°"),
    (2, 32767, 255, -2.5e-300, -0.25, D("-922337203685477.5808"),
     dt.datetime(2099, 12, 31, 23, 59, 59), False,
     "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}", D("-0.0001"), b"\x00", ""),
    (3, 0, 128, 0.0, 0.0, D("0"), dt.datetime(1899, 12, 30, 13, 45, 30), None,
     None, D("0"), None, None),
    (2147483647, None, None, None, None, None, None, True, None, None, b"", " pad "),
]

PROJECT_TABLES = {
    "COMP": dict(
        ddl="CREATE TABLE [COMP] (HDL TEXT(20) NOT NULL, TAGNAME TEXT(60), CAT TEXT(60), MFG TEXT(30), "
            "DESC1 TEXT(60), DESC2 TEXT(60), DESC3 TEXT(60), LOC TEXT(30), INST TEXT(30), "
            "SHEET TEXT(10), REF TEXT(10), CONSTRAINT PrimaryKey PRIMARY KEY (HDL))",
        cols=["HDL", "TAGNAME", "CAT", "MFG", "DESC1", "DESC2", "DESC3", "LOC", "INST", "SHEET", "REF"],
        rows=[(f"H{i:04d}", f"CR{i}", f"CAT-{i}", "AB", f"d1-{i}", None, "", "CAB1", "PNL", str(i), "A")
              for i in range(1, 6)],
        pk=("HDL",), src="discovered", counter=False),
    "PNLCOMP": dict(
        ddl="CREATE TABLE PNLCOMP (HDL TEXT(20), TAGNAME TEXT(60), CAT TEXT(60), PNLNAME TEXT(30), "
            "X DOUBLE, Y DOUBLE)",
        cols=["HDL", "TAGNAME", "CAT", "PNLNAME", "X", "Y"],
        rows=[("H0001", "CR1", "CAT-1", "PNL1", 10.5, 20.25),
              ("H0002", None, "CAT-2", "PNL1", None, None),
              ("H0001", "CR1", "CAT-1", "PNL1", 10.5, 20.25)],  # duplicate row; no key at all
        pk=(), src="none", counter=False),
    "TERMS": dict(
        ddl="CREATE TABLE TERMS (HDL TEXT(20) NOT NULL, TERM TEXT(10) NOT NULL, WIRENO TEXT(30), "
            "PIN TEXT(10), CONSTRAINT PrimaryKey PRIMARY KEY (HDL, TERM))",
        cols=["HDL", "TERM", "WIRENO", "PIN"],
        rows=[("H0001", "1", "W1", "A1"), ("H0001", "2", "W2", None), ("H0002", "1", "", "A2")],
        pk=("HDL", "TERM"), src="discovered", counter=False),
    "WIRENO": dict(
        ddl="CREATE TABLE WIRENO (RECNUM COUNTER, WIRENO TEXT(30), SHEET TEXT(10), COLOR TEXT(20))",
        cols=["WIRENO", "SHEET", "COLOR"],
        rows=[("W1", "1", "RED"), ("W2", "1", None), ("W3", "2", "BLU")],
        pk=("RECNUM",), src="inferred", counter=True),
    "WFRM2ALL": dict(
        ddl="CREATE TABLE WFRM2ALL (RECNUM COUNTER, WIRENO TEXT(30), NAM1 TEXT(30), PIN1 TEXT(10), "
            "NAM2 TEXT(30), PIN2 TEXT(10), LEN DOUBLE)",
        cols=["WIRENO", "NAM1", "PIN1", "NAM2", "PIN2", "LEN"],
        rows=[("W1", "CR1", "A1", "CR2", "A2", 1.25), ("W2", "CR1", "A2", None, None, None)],
        pk=("RECNUM",), src="inferred", counter=True),
    "NETS": dict(
        ddl="CREATE TABLE NETS (RECNUM COUNTER, NETNAME TEXT(30), SHEET TEXT(10))",
        cols=["NETNAME", "SHEET"],
        rows=[("N1", "1"), ("N2", "2")],
        pk=("RECNUM",), src="inferred", counter=True),
}

HOSTILE = {
    "ALLEN BRADLEY": ["DESC", "RANGE", "DUCT SIZE W x H", "COIL/VOLTAGE/STYLE"],
    "ALLEN-BRADLEY": ["CATALOG"],
    "KRAUS & NAIMER": ["CATALOG"],
    "DO": ["CATALOG"],
    "_PINLIST_BRUNDY/HUBBEL": ["PIN"],
}


def _hostile_rows(cols):
    return [("a1",) * len(cols), ("b'q",) + (None,) * (len(cols) - 1)]


def _expected_with_counter(spec):
    if not spec["counter"]:
        return [tuple(r) for r in spec["rows"]]
    return [(i,) + tuple(r) for i, r in enumerate(spec["rows"], 1)]


def big_row(i: int):
    """Expected BIG row (without RECNUM) for 1-based i."""
    return (f"K{i:06d}", i * 3, None if i % 7 == 0 else f"t{i}")


def _as_read(row: tuple) -> tuple:
    """What the Access driver hands back for a TYPED row.

    Driver facts (observed, not interface clauses): a YESNO column cannot hold NULL, so a NULL
    insert reads back as False; GUIDs come back as str without the surrounding braces.
    """
    row = list(row)
    yn, g = 7, 8  # positions in TYPED_ROWS tuples
    if row[yn] is None:
        row[yn] = False
    if row[g] is not None:
        row[g] = row[g].strip("{}")
    return tuple(row)


def build_manifest() -> dict:
    """Expected structure/content, independent of any database."""
    m: dict[str, dict] = {}
    for name, spec in PROJECT_TABLES.items():
        cols = (["RECNUM"] if spec["counter"] else []) + spec["cols"]
        m[name] = dict(count=len(spec["rows"]), pk=spec["pk"], pk_source=spec["src"],
                       columns=cols, rows=_expected_with_counter(spec))
    for fam in ("XX", "_PINLIST_XX"):
        rows = _catalog_rows("P" if fam == "XX" else "Q")
        if fam == "XX":
            exp = [tuple(r) + (i,) for i, r in enumerate(rows, 1)]
            cols = ["CATALOG", "ASSEMBLYCODE", "MFG", "DESC1", "NOTES", "RECNUM"]
        else:
            exp = [(i,) + tuple(r) for i, r in enumerate(rows, 1)]
            cols = ["RECNUM", "CATALOG", "ASSEMBLYCODE", "MFG", "DESC1", "NOTES"]
        m[fam] = dict(count=len(rows), pk=("RECNUM",), pk_source="inferred", columns=cols,
                      rows=exp, index=("CATALOG", "ASSEMBLYCODE"))
    m["EMPTYFAM"] = dict(count=0, pk=("RECNUM",), pk_source="inferred",
                         columns=["CATALOG", "RECNUM"], rows=[])
    for name, cols in HOSTILE.items():
        rows = [(i,) + tuple(r) for i, r in enumerate(_hostile_rows(cols), 1)]
        m[name] = dict(count=2, pk=("RECNUM",), pk_source="inferred",
                       columns=["RECNUM"] + cols, rows=rows)
    m["TYPED"] = dict(
        count=len(TYPED_ROWS), pk=("ID",), pk_source="discovered",
        columns=["ID", "L", "S", "B", "DBL", "RL", "CUR", "DT", "YN", "G", "DEC", "BIN", "TXT"],
        rows=[(i,) + _as_read(r) for i, r in enumerate(TYPED_ROWS, 1)],
        access_types={"ID": "COUNTER", "L": "INTEGER", "S": "SMALLINT", "B": "BYTE",
                      "DBL": "DOUBLE", "RL": "REAL", "CUR": "CURRENCY", "DT": "DATETIME",
                      "YN": "BIT", "G": "GUID", "DEC": "DECIMAL", "BIN": "LONGBINARY",
                      "TXT": "VARCHAR"},
    )
    m["BIG"] = dict(count=BIG_ROWS, pk=("RECNUM",), pk_source="inferred",
                    columns=["RECNUM", "KEY1", "VAL", "TXT"])
    return m


MANIFEST = build_manifest()
EXPECTED_USER_TABLES = sorted(MANIFEST, key=str.lower)


def _insert(cur, table: str, cols: list[str], rows) -> None:
    q = ", ".join(f"[{c}]" for c in cols)
    ph = ", ".join("?" for _ in cols)
    cur.executemany(f"INSERT INTO [{table}] ({q}) VALUES ({ph})", [list(r) for r in rows])


def build(path: Path) -> Path:
    path = Path(path)
    create_empty_db(path)
    con = connect_rw(path)
    cur = con.cursor()
    try:
        # (a) catalog-like
        cur.execute("CREATE TABLE XX ([CATALOG] TEXT(60), ASSEMBLYCODE TEXT(30), MFG TEXT(255), "
                    "DESC1 TEXT(255), NOTES MEMO, RECNUM COUNTER)")
        cur.execute("CREATE INDEX IDX_CAT ON XX ([CATALOG], ASSEMBLYCODE)")
        _insert(cur, "XX", ["CATALOG", "ASSEMBLYCODE", "MFG", "DESC1", "NOTES"], _catalog_rows("P"))
        cur.execute("CREATE TABLE [_PINLIST_XX] (RECNUM COUNTER, [CATALOG] TEXT(60), ASSEMBLYCODE TEXT(30), "
                    "MFG TEXT(255), DESC1 TEXT(255), NOTES MEMO)")
        cur.execute("CREATE INDEX IDX_CAT ON [_PINLIST_XX] ([CATALOG], ASSEMBLYCODE)")
        _insert(cur, "_PINLIST_XX", ["CATALOG", "ASSEMBLYCODE", "MFG", "DESC1", "NOTES"],
                _catalog_rows("Q"))
        cur.execute("CREATE TABLE EMPTYFAM ([CATALOG] TEXT(30), RECNUM COUNTER)")

        # (b) hostile names
        cur.execute("CREATE TABLE [ALLEN BRADLEY] (RECNUM COUNTER, [DESC] TEXT(20), [RANGE] TEXT(20), "
                    "[DUCT SIZE W x H] TEXT(20), [COIL/VOLTAGE/STYLE] TEXT(20))")
        for name in ("ALLEN-BRADLEY", "KRAUS & NAIMER", "DO"):
            cur.execute(f"CREATE TABLE [{name}] (RECNUM COUNTER, [CATALOG] TEXT(20))")
        cur.execute("CREATE TABLE [_PINLIST_BRUNDY/HUBBEL] (RECNUM COUNTER, PIN TEXT(20))")
        for name, cols in HOSTILE.items():
            _insert(cur, name, cols, _hostile_rows(cols))

        # (c) project tables
        for name, spec in PROJECT_TABLES.items():
            cur.execute(spec["ddl"])
            _insert(cur, name, spec["cols"], spec["rows"])

        # (d) typed
        cur.execute("CREATE TABLE TYPED (ID COUNTER, L LONG, S SMALLINT, B BYTE, DBL DOUBLE, RL REAL, "
                    "CUR CURRENCY, DT DATETIME, YN YESNO, G GUID, [DEC] DECIMAL(18,4), BIN LONGBINARY, "
                    "TXT TEXT(20), CONSTRAINT PrimaryKey PRIMARY KEY (ID))")
        _insert(cur, "TYPED",
                ["L", "S", "B", "DBL", "RL", "CUR", "DT", "YN", "G", "DEC", "BIN", "TXT"], TYPED_ROWS)

        # (e) big
        cur.execute("CREATE TABLE BIG (RECNUM COUNTER, KEY1 TEXT(20), VAL LONG, TXT TEXT(30))")
        _insert(cur, "BIG", ["KEY1", "VAL", "TXT"], [big_row(i) for i in range(1, BIG_ROWS + 1)])
    finally:
        cur.close()
        con.close()
    for lock in (".ldb", ".laccdb"):
        p = path.with_suffix(lock)
        if p.exists():
            with contextlib.suppress(OSError):
                os.remove(p)
    return path
