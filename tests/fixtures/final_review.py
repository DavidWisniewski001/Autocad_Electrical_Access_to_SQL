"""Builders and byte-level helpers for the final-review regression tests (0.3.0).

Ported from the reviewer's scratch scripts (alt.py, chr.py, craft.py): every database is built in a
pytest tmp dir through the Access ODBC driver (see ``build_access``); the helpers below corrupt
*copies* of a built file at the page/row level, using the pure reader's own page parsing to find
where things live.
"""

from __future__ import annotations

import struct
from pathlib import Path

from tests.fixtures.build_access import connect_rw, create_empty_db

ROWS = 45
BIG = 2**40

# Jet 4 / ACE data page: 0x01 0x01, free space (2), owner TDEF page (4 at offset 4), ..., row count
# at 12, row offset table from 14.
OWNER_OFFSET = 4
ROW_COUNT_OFFSET = 12
ROW_TABLE_OFFSET = 14
ROW_DELETED = 0x8000
ROW_OVERFLOW = 0x4000
ROW_MASK = 0x1FFF


def build_core(path: Path) -> Path:
    """NETLIST (45 rows, PK, two var columns) and NOKEY (no key at all, 3 rows)."""
    path = Path(path)
    create_empty_db(path)
    con = connect_rw(path)
    cur = con.cursor()
    try:
        cur.execute(
            "CREATE TABLE NETLIST (ID INTEGER NOT NULL, NAME TEXT(40), TAG TEXT(40), QTY DOUBLE, "
            "CONSTRAINT PrimaryKey PRIMARY KEY (ID))"
        )
        for i in range(ROWS):
            cur.execute(
                "INSERT INTO NETLIST (ID, NAME, TAG, QTY) VALUES (?, ?, ?, ?)",
                i + 1,
                f"name-{i:03d}",
                f"tag-{i:03d}",
                i * 0.5,
            )
        cur.execute("CREATE TABLE NOKEY (A INTEGER, B TEXT(10))")
        for i in range(3):
            cur.execute("INSERT INTO NOKEY (A, B) VALUES (?, ?)", i, f"b{i}")
    finally:
        cur.close()
        con.close()
    return path


def expected_netlist() -> list[tuple]:
    return [(i + 1, f"name-{i:03d}", f"tag-{i:03d}", i * 0.5) for i in range(ROWS)]


def build_alter(path: Path) -> Path:
    """Tables whose definition changed after rows existed (ALTER ADD / DROP COLUMN)."""
    path = Path(path)
    create_empty_db(path)
    con = connect_rw(path)
    cur = con.cursor()
    try:
        cur.execute("CREATE TABLE F (ID INTEGER, A DOUBLE, B SMALLINT)")
        for i in range(1, 21):
            cur.execute("INSERT INTO F VALUES (?, ?, ?)", i, i * 1.5, i)
        cur.execute("ALTER TABLE F ADD COLUMN T TEXT(50)")
        cur.execute("ALTER TABLE F ADD COLUMN M MEMO")
        cur.execute("INSERT INTO F (ID, T, M) VALUES (100, 'new', 'memo')")
        # DROP COLUMN variant (also with an ADD afterwards and an UPDATE of an old row)
        cur.execute("CREATE TABLE V (ID INTEGER, T TEXT(50), U TEXT(50))")
        for i in range(1, 11):
            cur.execute("INSERT INTO V VALUES (?, ?, ?)", i, f"t{i}", None if i % 2 else "u")
        cur.execute("ALTER TABLE V DROP COLUMN T")
        cur.execute("ALTER TABLE V DROP COLUMN U")
        cur.execute("ALTER TABLE V ADD COLUMN W TEXT(10)")
        cur.execute("UPDATE V SET W = 'w' WHERE ID = 3")
        # DROP COLUMN only, columns before and after the dropped one
        cur.execute("CREATE TABLE D (ID INTEGER, X TEXT(20), Y DOUBLE, Z TEXT(20))")
        for i in range(1, 8):
            cur.execute("INSERT INTO D VALUES (?, ?, ?, ?)", i, f"x{i}", i / 4, f"z{i}")
        cur.execute("ALTER TABLE D DROP COLUMN X")
    finally:
        cur.close()
        con.close()
    return path


CHAR_VALUES = ["ab", "â€“x", "", None, "abcdefghij"]


def build_chr(path: Path) -> Path:
    """Fixed-length TEXT (CHAR) columns, plain and with Unicode compression."""
    path = Path(path)
    create_empty_db(path)
    con = connect_rw(path)
    cur = con.cursor()
    try:
        cur.execute("CREATE TABLE C (ID COUNTER PRIMARY KEY, CH CHAR(10), CHC CHAR(10) WITH COMP)")
        for v in CHAR_VALUES:
            cur.execute("INSERT INTO C (CH, CHC) VALUES (?, ?)", v, v)
    finally:
        cur.close()
        con.close()
    return path


def build_bigint(path: Path) -> Path:
    """An .accdb with a BIGINT column holding +/-2**40 (the ACE driver accepts the DDL)."""
    path = Path(path)
    create_empty_db(path)
    con = connect_rw(path)
    cur = con.cursor()
    try:
        cur.execute("CREATE TABLE BIG64 (ID INTEGER, BI BIGINT)")
        # BIGINT cannot be bound as a parameter by the driver: literals only
        for i, v in enumerate((BIG, -BIG, 0, "NULL"), 1):
            cur.execute(f"INSERT INTO BIG64 (ID, BI) VALUES ({i}, {v})")
        # second table: lets a test check that the other tables still convert
        cur.execute("CREATE TABLE OKTBL (ID INTEGER, S TEXT(10))")
        cur.execute("INSERT INTO OKTBL VALUES (1, 'x')")
    finally:
        cur.close()
        con.close()
    return path


# ------------------------------------------------------------------------------ file internals


class Layout:
    """Where one table lives in a (pure-reader readable) database file."""

    def __init__(self, path: Path, table: str) -> None:
        from acade2sql.readers.access_parser_reader import AccessParserReader

        with AccessParserReader(path) as r:
            jet = r._file()
            tdef = r._table_def(table)
            self.page_size = jet.page_size
            self.tdef_page = tdef.page
            self.usage_map = tdef.usage_map
            self.data_pages = jet.data_pages(tdef)
        assert self.data_pages, f"{table}: no data pages found"

    def row_table(self, buf: bytes | bytearray, page: int) -> tuple[int, ...]:
        base = page * self.page_size
        count = struct.unpack_from("<H", buf, base + ROW_COUNT_OFFSET)[0]
        return struct.unpack_from(f"<{count}H", buf, base + ROW_TABLE_OFFSET)

    def row_span(self, buf: bytes | bytearray, page: int, index: int) -> tuple[int, int]:
        """Absolute (start, end) file offsets of row ``index`` of a data page."""
        offsets = self.row_table(buf, page)
        start = offsets[index] & ROW_MASK
        end = self.page_size if index == 0 else offsets[index - 1] & ROW_MASK
        base = page * self.page_size
        return base + start, base + end


def set_page_owner(buf: bytearray, layout: Layout, page: int, owner: int) -> None:
    struct.pack_into("<I", buf, page * layout.page_size + OWNER_OFFSET, owner)


def set_row_slot(buf: bytearray, layout: Layout, page: int, index: int, value: int) -> None:
    struct.pack_into("<H", buf, page * layout.page_size + ROW_TABLE_OFFSET + 2 * index, value)


def row_var_info(buf: bytes | bytearray, start: int, end: int) -> tuple[int, int, int]:
    """(mask_start, var-count position, var-count) of a Jet 4 row spanning ``start..end``."""
    n = end - start
    ncols = struct.unpack_from("<H", buf, start)[0]
    mask_start = n - (ncols + 7) // 8
    count_pos = start + mask_start - 2
    return mask_start, count_pos, struct.unpack_from("<H", buf, count_pos)[0]


def drop_var_slot(buf: bytearray, layout: Layout) -> None:
    """First row: claim one variable column fewer, so its (present) last column has no slot."""
    page = layout.data_pages[0]
    start, end = layout.row_span(buf, page, 0)
    _, count_pos, count = row_var_info(buf, start, end)
    assert count >= 2, count
    struct.pack_into("<H", buf, count_pos, count - 1)


def swap_var_offsets(buf: bytearray, layout: Layout) -> None:
    """First row: swap the offsets of the first two variable columns (decreasing offsets)."""
    page = layout.data_pages[0]
    start, end = layout.row_span(buf, page, 0)
    mask_start, _, count = row_var_info(buf, start, end)
    first = start + mask_start - 4 - 2 * count  # stored backwards: offsets[j] at first+2*(count-j)
    pos0 = first + 2 * count
    pos1 = first + 2 * (count - 1)
    a, b = struct.unpack_from("<H", buf, pos0)[0], struct.unpack_from("<H", buf, pos1)[0]
    assert a < b, (a, b)
    struct.pack_into("<H", buf, pos0, b)
    struct.pack_into("<H", buf, pos1, a)


def overflow_slot_at_page_end(buf: bytearray, layout: Layout) -> None:
    """Row 0 flagged 'overflow' (4-byte pointer) but starting 2 bytes before the page end."""
    set_row_slot(buf, layout, layout.data_pages[0], 0, ROW_OVERFLOW | (layout.page_size - 2))


def empty_usage_map_record(buf: bytearray, layout: Layout) -> None:
    """The table's usage-map record gets zero length (its slot equals the previous row's end)."""
    page, index = layout.usage_map >> 8, layout.usage_map & 0xFF
    offsets = struct.unpack_from(
        f"<{struct.unpack_from('<H', buf, page * layout.page_size + ROW_COUNT_OFFSET)[0]}H",
        buf,
        page * layout.page_size + ROW_TABLE_OFFSET,
    )
    end = layout.page_size if index == 0 else offsets[index - 1] & ROW_MASK
    set_row_slot(buf, layout, page, index, end)


def empty_data_row(buf: bytearray, layout: Layout) -> None:
    set_row_slot(buf, layout, layout.data_pages[0], 0, layout.page_size)


def huge_column_count(buf: bytearray, layout: Layout) -> None:
    start, _ = layout.row_span(buf, layout.data_pages[0], 0)
    struct.pack_into("<H", buf, start, 0xFFFF)
