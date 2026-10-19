"""Pure-Python Access reader (no ODBC driver), built on access-parser's TDEF parsing.

Used when the Microsoft Access ODBC driver is not installed (``--reader pure``, or ``auto`` as a
fallback). Same interface and value contract as :class:`~acade2sql.readers.PyodbcReader`.

How access-parser 0.0.6 is used (DECISIONS #37)
-----------------------------------------------
access-parser is used only for what it parses correctly: the table definition (TDEF) structures
(``parsing_primitives.parse_table_head`` / ``parse_table_data``: columns, names, flags, indexes).
Its value layer is *not* used: ``AccessParser``/``AccessTable``/``utils.parse_type`` guess the
text encoding (UTF-8 then Latin-1, ``errors="ignore"``), ignore the compressed-unicode mode toggles
(U+2013 became ``'\\x13'``), strip NUL characters, return dates/money as formatted strings, read
BYTE as signed, GUIDs big-endian, mis-mask overflow-row offsets and silently drop rows they cannot
parse. Nothing is monkeypatched. This module owns, from the raw file bytes:

* the file header (version, page size, RC4-masked header: database password, encoding key),
* the catalog (``MSysObjects`` rows), the table's usage map (its data pages), row slots with the
  deleted / overflow flags, the null mask, fixed and variable column offsets (Jet 3 jump tables),
* LVAL (MEMO/OLE) chains and every value conversion, text through :func:`decode_jet_text`.

The file is opened once with ``open(path, "rb")`` and memory-mapped read-only: no lock file, no
write, any number of concurrent readers. Rows are streamed page by page (nothing is cached
between calls, so only the current batch is held in memory).

What the file cannot provide like the ODBC driver does
------------------------------------------------------
* ``indexes``: read from the TDEF (logical indexes with their names, columns in key order and the
  unique flag; foreign-key reference entries are skipped, as SQLStatistics does), so a
  ``PrimaryKey`` index gives ``primary_key_source="discovered"`` exactly as with ODBC.
* ``nullable``: reproduces the driver (COUNTER and BIT not nullable, everything else nullable;
  discovered PK columns NOT NULL). The TDEF "required" flag is not used, as the driver ignores it.
* ``size``/``decimal_digits``/``data_type``: the driver's values for each type (VARCHAR size in
  characters, DECIMAL precision/scale from the TDEF).
* Dates before 1753-01-01: the driver's calendar drifts there (about one day per leap year) from
  what Access displays; this reader reproduces the driver exactly (DECISIONS #39).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import mmap
import struct
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

from access_parser.parsing_primitives import parse_table_data, parse_table_head
from construct import ConstructError

from acade2sql.errors import Acade2SqlError, SourceIntegrityError, UnsupportedAccessFeature
from acade2sql.readers.base import (
    Reader,
    TempCopy,
    build_table_info,
    check_batch_size,
    resolve_table,
    validate_source,
)
from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo

log = logging.getLogger(__name__)

# UnsupportedAccessFeature lives in acade2sql.errors; re-exported here for compatibility.
__all__ = [
    "AccessParserReader",
    "UnsupportedAccessFeature",
    "column_type_codes",
    "decode_jet_text",
]

# ---------------------------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------------------------

_COMPRESSED_PREFIX = b"\xff\xfe"


def decode_jet_text(raw: bytes, version: int, codepage: str = "cp1252") -> str:
    """Decode a TEXT/MEMO value exactly as stored; never lossy.

    * Jet 4 / ACE (``version > 3``): a value starting with ``FF FE`` is *compressed unicode*: it
      starts in compressed mode (one byte = one character U+0000-U+00FF); a ``0x00`` byte switches
      to uncompressed mode (UTF-16LE code units). In uncompressed mode the first code unit is always
      data; afterwards a ``0x00`` byte at a code-unit boundary switches back to compressed mode
      (Access writes a single ``0x00``; a ``0x00 0x00`` pair is accepted as one switch as well).
      Without the prefix the whole value is UTF-16LE.
    * Jet 3 (Access 97, ``version == 3``): bytes in the database code page (``codepage``).

    Undecodable data (an odd number of UTF-16 bytes, a lone surrogate, a byte undefined in the
    code page) raises :class:`Acade2SqlError`; nothing is ignored or replaced.
    """
    data = bytes(raw)
    if version <= 3:
        try:
            return data.decode(codepage)
        except (UnicodeDecodeError, LookupError) as exc:
            raise Acade2SqlError(f"undecodable Jet 3 text ({codepage}): {exc}") from None
    try:
        if data.startswith(_COMPRESSED_PREFIX):
            return _decode_compressed(data[2:])
        return data.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise Acade2SqlError(f"undecodable Jet 4 text: {exc}") from None


def _decode_compressed(body: bytes) -> str:
    """Body of a compressed-unicode value (after ``FF FE``); see :func:`decode_jet_text`."""
    if 0 not in body:
        return body.decode("latin-1")  # the common case: one compressed run
    parts: list[str] = []
    n = len(body)
    i = 0
    compressed = True
    while i < n:
        if compressed:
            j = body.find(0, i)
            if j < 0:
                parts.append(body[i:].decode("latin-1"))
                break
            parts.append(body[i:j].decode("latin-1"))
            i, compressed = j + 1, False
            continue
        j = i + 2  # the first code unit of a run is data, even if its low byte is 0x00
        while j < n and body[j] != 0:
            j += 2
        if j > n:
            raise Acade2SqlError(
                f"undecodable Jet 4 text: odd byte count in an uncompressed run at offset {i + 2}"
            )
        parts.append(body[i:j].decode("utf-16-le"))  # strict: lone surrogates raise
        if j >= n:
            break
        i = j + (2 if j + 1 < n and body[j + 1] == 0 else 1)
        compressed = True
    return "".join(parts)


# ---------------------------------------------------------------------------------------------
# File format constants
# ---------------------------------------------------------------------------------------------

_MAGIC = b"\x00\x01\x00\x00"
_FORMAT_IDS = (b"Standard Jet DB", b"Standard ACE DB")
_VERSION_OFFSET = 0x14
_VERSIONS = {0: 3, 1: 4, 2: 12, 3: 14, 4: 15, 5: 16, 6: 17}  # header byte -> Jet/ACE version
_PAGE_SIZE = {3: 2048, 4: 4096}

_HEADER_MASK_START = 0x18
_HEADER_MASK_LEN = 128
_HEADER_RC4_KEY = (0x6B39DAC7).to_bytes(4, "little")
_CODE_PAGE_OFFSET = 0x3C
_ENCODING_KEY_OFFSET = 0x3E
_PASSWORD_OFFSET = 0x42
_PASSWORD_LEN = {3: 20, 4: 40}
_HEADER_DATE_OFFSET = 0x72

_PAGE_DATA = b"\x01\x01"
_PAGE_TDEF = b"\x02\x01"
_CATALOG_PAGE = 2
_ROW_OFFSET_MASK = 0x1FFF
_ROW_DELETED = 0x8000  # deleted row, or the target of an overflow pointer (hidden from scans)
_ROW_OVERFLOW = 0x4000  # the row holds a 4-byte pointer to the real row
_MAX_OVERFLOW_HOPS = 8
_USAGE_MAP_MIN = 5  # map type byte + start page (type 0) / first map page (type 1)

_LVAL_INLINE = 0x80000000
_LVAL_SINGLE = 0x40000000
_LVAL_LENGTH_MASK = 0x3FFFFFFF

_OBJ_TABLE = 1
_FLAG_SYSTEM = 0x80000000
_FLAG_SYSTEM_ALT = 0x00000002
_FLAG_UNLISTED = 0x00030000  # e.g. MSysComplexType_* templates: SQLTables does not list them
_INDEX_FOREIGN_KEY = 2
_INDEX_UNIQUE = 0x01
# TDEF layout sizes (Jet 3, Jet 4): real-index header entry, column entry, column-name length
# prefix, real-index entry and the offset of its flags byte. access-parser 0.0.6 reads the Jet 4
# flags 4 bytes too early, so the unique flag is read here from the raw TDEF.
_REAL_INDEX_SIZE = (8, 12)
_COLUMN_SIZE = (18, 25)
_NAME_PREFIX = (1, 2)
_REAL_INDEX2_SIZE = (39, 52)
_REAL_INDEX2_FLAGS = (38, 46)
_NO_COLUMN = 0xFFFF

# Access column type codes.
T_BOOL, T_BYTE, T_INT, T_LONG, T_MONEY, T_FLOAT, T_DOUBLE, T_DATETIME = 1, 2, 3, 4, 5, 6, 7, 8
T_BINARY, T_TEXT, T_OLE, T_MEMO, T_GUID, T_NUMERIC = 9, 10, 11, 12, 15, 16
T_COMPLEX, T_BIGINT = 18, 19
_UNSUPPORTED_TYPES = {
    17: "unknown type 17",
    T_COMPLEX: "attachment / multi-value field",
    20: "extended date/time (Date/Time Extended)",
}

_LONG_SIZE = 1073741823  # what the driver reports for LONGCHAR / LONGBINARY
# type code -> (access_type, ODBC data_type, size, decimal_digits); None = from the TDEF.
_TYPE_INFO: dict[int, tuple[str, int, int | None, int | None]] = {
    T_BOOL: ("BIT", -7, 1, 0),
    T_BYTE: ("BYTE", -6, 3, 0),
    T_INT: ("SMALLINT", 5, 5, 0),
    T_LONG: ("INTEGER", 4, 10, 0),
    T_MONEY: ("CURRENCY", 2, 19, 4),
    T_FLOAT: ("REAL", 7, 24, None),
    T_DOUBLE: ("DOUBLE", 8, 53, None),
    T_DATETIME: ("DATETIME", 93, 19, 0),
    T_BINARY: ("BINARY", -2, None, None),
    T_TEXT: ("VARCHAR", -9, None, None),
    T_OLE: ("LONGBINARY", -4, _LONG_SIZE, None),
    T_MEMO: ("LONGCHAR", -10, _LONG_SIZE, None),
    T_GUID: ("GUID", -11, 36, None),
    T_NUMERIC: ("DECIMAL", 2, None, None),
    T_BIGINT: ("BIGINT", -5, 19, 0),
}

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_I16 = struct.Struct("<h")
_I32 = struct.Struct("<i")
_I64 = struct.Struct("<q")
_F32 = struct.Struct("<f")
_F64 = struct.Struct("<d")
_NUMERIC = struct.Struct("<BIIII")

_EPOCH = dt.datetime(1899, 12, 30)
# The Access ODBC driver truncates to whole seconds after adding ~1e-4 s (measured, DECISIONS #37):
# 10.9998 s -> 10 s, 10.9999 s -> 11 s.
_SECONDS_EPSILON = 1e-4


# ---------------------------------------------------------------------------------------------
# Column and table definitions
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Column:
    name: str
    number: int  # column number: null-mask bit and index references
    var_number: int  # slot in the variable-length offset table
    display: int  # ordinal position (as SQLColumns reports it)
    type_code: int
    fixed: bool
    fixed_offset: int
    length: int
    precision: int
    scale: int
    autonumber: bool
    calculated: bool


@dataclass(frozen=True)
class _TableDef:
    name: str
    page: int
    columns: tuple[_Column, ...]  # in ordinal order
    indexes: tuple[IndexInfo, ...]
    row_count: int  # as recorded in the TDEF
    usage_map: int  # record pointer of the row usage map


def _rc4(key: bytes, length: int) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for _ in range(length):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _bits(bitmap: bytes) -> Iterator[int]:
    for byte_index, byte in enumerate(bitmap):
        if byte:
            for bit in range(8):
                if byte & (1 << bit):
                    yield byte_index * 8 + bit


# ---------------------------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------------------------


class _JetFile:
    """Read-only, memory-mapped view of one Access file: pages, rows, LVAL chains."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mm = None
        try:
            self._fh = open(path, "rb")  # noqa: SIM115 - closed in close()
        except OSError as exc:
            raise Acade2SqlError(
                f"Cannot open {path} for reading ({exc.strerror or exc}); is it opened "
                "exclusively by another program (Access/AutoCAD Electrical)?"
            ) from None
        try:
            try:
                self._mm: mmap.mmap | None = mmap.mmap(
                    self._fh.fileno(), 0, access=mmap.ACCESS_READ
                )
            except ValueError:  # empty file
                raise Acade2SqlError(f"Not a valid Access database (empty file): {path}") from None
            self._parse_header()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None  # type: ignore[assignment]

    # -- header --------------------------------------------------------------------------------

    def _parse_header(self) -> None:
        mm = self._map()
        head = mm[: _HEADER_MASK_START + _HEADER_MASK_LEN]
        if len(head) < _HEADER_MASK_START + _HEADER_MASK_LEN or head[:4] != _MAGIC:
            raise Acade2SqlError(f"Not a valid Access database (bad file header): {self.path}")
        if not any(head[4 : 4 + len(f)] == f for f in _FORMAT_IDS):
            raise Acade2SqlError(f"Not a valid Access database (unknown format id): {self.path}")
        code = head[_VERSION_OFFSET]
        self.version = _VERSIONS.get(code, 4 if code > 0 else 3)
        self.jet3 = self.version == 3
        self.page_size = _PAGE_SIZE[3 if self.jet3 else 4]
        if len(mm) < self.page_size * (_CATALOG_PAGE + 1):
            raise Acade2SqlError(f"Not a valid Access database (file too short): {self.path}")
        mask = _rc4(_HEADER_RC4_KEY, _HEADER_MASK_LEN)
        plain = bytearray(head)
        for k in range(_HEADER_MASK_LEN):
            plain[_HEADER_MASK_START + k] ^= mask[k]
        self._check_not_encrypted(bytes(plain))
        self.codepage = "cp1252"
        if self.jet3:
            cp = _U16.unpack_from(plain, _CODE_PAGE_OFFSET)[0]
            if cp and cp != 1252:
                try:
                    "".encode(f"cp{cp}")
                    self.codepage = f"cp{cp}"
                except LookupError:
                    log.warning("unknown Jet 3 code page, using cp1252", extra={"kv": {"cp": cp}})
        self.row_count_size = 1 if self.jet3 else 2
        self._rows_at = 8 if self.jet3 else 12  # data page: row count offset
        if self._page(_CATALOG_PAGE)[:2] != _PAGE_TDEF:
            raise UnsupportedAccessFeature(
                f"Cannot read {self.path}: the catalog page is not readable (encrypted or "
                "corrupted database). Use the ODBC reader (--reader odbc)."
            )

    def _check_not_encrypted(self, plain: bytes) -> None:
        encoding_key = _U32.unpack_from(plain, _ENCODING_KEY_OFFSET)[0]
        size = _PASSWORD_LEN[3 if self.jet3 else 4]
        pwd = bytearray(plain[_PASSWORD_OFFSET : _PASSWORD_OFFSET + size])
        if not self.jet3:
            date = _F64.unpack_from(plain, _HEADER_DATE_OFFSET)[0]
            if math.isfinite(date) and abs(date) < 2**31:
                pwd_mask = _I32.pack(int(date))
                for k in range(len(pwd)):
                    pwd[k] ^= pwd_mask[k % 4]
        if any(pwd):
            raise UnsupportedAccessFeature(
                f"Cannot read {self.path}: the database is password-protected, which acade2sql "
                "does not support. Remove the password in Access (File > Info > Decrypt "
                "Database / Unset Database Password) or convert a copy without one."
            )
        if encoding_key:
            raise UnsupportedAccessFeature(
                f"Cannot read {self.path}: the database is encrypted/encoded. The pure reader "
                "cannot decrypt it; use the ODBC reader (--reader odbc) or decrypt it in Access."
            )

    # -- pages and rows ------------------------------------------------------------------------

    def _map(self) -> mmap.mmap:
        if self._mm is None:
            raise Acade2SqlError(f"Reader for {self.path} is closed")
        return self._mm

    def _page(self, number: int) -> bytes:
        mm = self._map()
        start = number * self.page_size
        if number < 0 or start + self.page_size > len(mm):
            raise Acade2SqlError(f"Corrupt database {self.path}: page {number} is out of range")
        return mm[start : start + self.page_size]

    def _row_table(self, page: bytes) -> tuple[int, ...]:
        count = _U16.unpack_from(page, self._rows_at)[0]
        try:
            return struct.unpack_from(f"<{count}H", page, self._rows_at + 2)
        except struct.error:
            raise Acade2SqlError(
                f"Corrupt database {self.path}: data page row table overruns the page"
            ) from None

    def _row_bounds(self, offsets: tuple[int, ...], index: int) -> tuple[int, int]:
        start = offsets[index] & _ROW_OFFSET_MASK
        end = self.page_size if index == 0 else offsets[index - 1] & _ROW_OFFSET_MASK
        if not 0 < start <= end <= self.page_size:
            raise Acade2SqlError(f"Corrupt database {self.path}: bad row bounds {start}..{end}")
        return start, end

    def row_at(self, pointer: int) -> bytes:
        """Raw bytes of the row addressed by a record pointer (page << 8 | row)."""
        page_no, index = pointer >> 8, pointer & 0xFF
        page = self._page(page_no)
        if page[:2] != _PAGE_DATA:
            raise Acade2SqlError(f"Corrupt database {self.path}: page {page_no} is not a data page")
        offsets = self._row_table(page)
        if index >= len(offsets):
            raise Acade2SqlError(f"Corrupt database {self.path}: no row {index} on page {page_no}")
        start, end = self._row_bounds(offsets, index)
        return page[start:end]

    def data_pages(self, tdef: _TableDef) -> list[int]:
        """The table's data pages, from its row usage map (in page order)."""
        return self.page_scan(tdef)[0]

    def page_scan(self, tdef: _TableDef) -> tuple[list[int], list[int]]:
        """``(owned, skipped)``: the usage map's pages that are data pages owned by the table, and
        the listed pages that are not (another table's page, a freed page, a non-data page)."""
        record = self.row_at(tdef.usage_map)
        if len(record) < _USAGE_MAP_MIN:
            raise Acade2SqlError(f"usage map record too short ({len(record)} bytes)")
        kind = record[0]
        if kind == 0:
            first = _U32.unpack_from(record, 1)[0]
            candidates = [first + b for b in _bits(record[5:])]
        elif kind == 1:
            per_map = (self.page_size - 4) * 8
            candidates = []
            for k in range((len(record) - 1) // 4):
                map_page = _U32.unpack_from(record, 1 + 4 * k)[0]
                if map_page:
                    bitmap = self._page(map_page)[4:]
                    candidates.extend(k * per_map + b for b in _bits(bitmap))
        else:
            raise Acade2SqlError(f"unknown usage map type {kind}")
        pages: list[int] = []
        skipped: list[int] = []
        owner = _U32.pack(tdef.page)
        for number in candidates:
            page = self._page(number)
            if page[:2] == _PAGE_DATA and page[4:8] == owner:
                pages.append(number)
            else:
                skipped.append(number)
        return pages, skipped

    def records(self, tdef: _TableDef, *, resolve: bool = True) -> Iterator[bytes]:
        """Raw bytes of every live row (``resolve=False``: empty placeholders, for counting).

        Raises :class:`SourceIntegrityError` (before any row when a listed page is skipped, else
        after the last row) unless every listed page is the table's and the live rows found
        equal the TDEF row count: a partial read must never pass for a complete one.
        """
        for _page_no, record in self.page_records(tdef, resolve=resolve):
            yield record

    def page_records(self, tdef: _TableDef, *, resolve: bool = True) -> Iterator[tuple[int, bytes]]:
        """:meth:`records` with the page number of each row. Errors name the table and page."""
        try:
            pages, skipped = self.page_scan(tdef)
        except Exception as exc:
            raise _scan_error(tdef, None, exc) from None
        if skipped:
            found = sum(self._live_rows(tdef, number) for number in pages)
            raise self._integrity_error(tdef, found, skipped)
        found = 0
        for number in pages:
            try:
                page = self._page(number)
                offsets = self._row_table(page)
            except Exception as exc:
                raise _scan_error(tdef, number, exc) from None
            for index, raw in enumerate(offsets):
                if raw & _ROW_DELETED:
                    continue
                found += 1
                if not resolve:
                    yield number, b""
                    continue
                try:
                    start, end = self._row_bounds(offsets, index)
                    if raw & _ROW_OVERFLOW:
                        if end - start < 4:
                            raise Acade2SqlError(
                                f"overflow row {index} holds {end - start} bytes, not a pointer"
                            )
                        record = self._overflow(page[start : start + 4])
                    else:
                        record = page[start:end]
                except Exception as exc:
                    raise _scan_error(tdef, number, exc) from None
                yield number, record
        if found != tdef.row_count:
            raise self._integrity_error(tdef, found, [])

    def _live_rows(self, tdef: _TableDef, number: int) -> int:
        try:
            offsets = self._row_table(self._page(number))
        except Exception as exc:
            raise _scan_error(tdef, number, exc) from None
        return sum(1 for raw in offsets if not raw & _ROW_DELETED)

    def _integrity_error(
        self, tdef: _TableDef, found: int, skipped: list[int]
    ) -> SourceIntegrityError:
        shown = ", ".join(str(n) for n in skipped[:20]) + (" ..." if len(skipped) > 20 else "")
        detail = (
            f"usage map lists {len(skipped)} page(s) not owned by the table: {shown}; "
            if skipped
            else ""
        )
        return SourceIntegrityError(
            f"Table {tdef.name!r} in {self.path}: {detail}the table definition records "
            f"{tdef.row_count} rows, the scanned pages hold {found}. The file is inconsistent "
            "(damaged, or being written: close AutoCAD Electrical/Access, use --copy-to-temp, or "
            "Compact & Repair a copy); nothing is read from this table. Try --reader odbc."
        )

    def _overflow(self, pointer_bytes: bytes) -> bytes:
        pointer = _U32.unpack(pointer_bytes)[0]
        for _ in range(_MAX_OVERFLOW_HOPS):
            page_no, index = pointer >> 8, pointer & 0xFF
            page = self._page(page_no)
            if page[:2] != _PAGE_DATA:
                raise Acade2SqlError(f"overflow row pointer to non-data page {page_no}")
            offsets = self._row_table(page)
            if index >= len(offsets):
                raise Acade2SqlError(f"overflow row pointer to missing row {index} of {page_no}")
            start, end = self._row_bounds(offsets, index)
            raw = offsets[index]
            if raw & _ROW_OVERFLOW and not raw & _ROW_DELETED:
                if end - start < 4:
                    raise Acade2SqlError(f"overflow row on page {page_no} too short")
                pointer = _U32.unpack_from(page, start)[0]
                continue
            return page[start:end]
        raise Acade2SqlError("overflow row chain too long")

    def long_value(self, data: bytes) -> bytes:
        """Resolve a MEMO/OLE field: inline, single LVAL row, or a chain of LVAL rows."""
        if len(data) < 12:
            raise Acade2SqlError(f"bad long-value header ({len(data)} bytes)")
        header = _U32.unpack_from(data, 0)[0]
        length = header & _LVAL_LENGTH_MASK
        if header & _LVAL_INLINE:
            value = data[12 : 12 + length]
        elif header & _LVAL_SINGLE:
            value = self.row_at(_U32.unpack_from(data, 4)[0])[:length]
        else:
            chunks: list[bytes] = []
            got = 0
            pointer = _U32.unpack_from(data, 4)[0]
            seen: set[int] = set()
            while pointer and got < length:
                if pointer in seen:
                    raise Acade2SqlError("long value chain loops")
                seen.add(pointer)
                row = self.row_at(pointer)
                pointer = _U32.unpack_from(row, 0)[0]
                chunks.append(row[4:])
                got += len(row) - 4
            value = b"".join(chunks)[:length]
        if len(value) != length:
            raise Acade2SqlError(f"long value truncated: {len(value)} of {length} bytes")
        return value

    # -- table definitions ---------------------------------------------------------------------

    def tdef_bytes(self, page_no: int) -> bytes:
        page = self._page(page_no)
        if page[:2] != _PAGE_TDEF:
            raise Acade2SqlError(f"Corrupt database {self.path}: page {page_no} is not a TDEF")
        data = page
        nxt = _U32.unpack_from(page, 4)[0]
        seen = {page_no}
        while nxt:
            if nxt in seen:
                raise Acade2SqlError(f"Corrupt database {self.path}: TDEF page loop")
            seen.add(nxt)
            cont = self._page(nxt)
            if cont[:2] != _PAGE_TDEF:
                raise Acade2SqlError(f"Corrupt database {self.path}: bad TDEF continuation")
            data += cont[8:]
            nxt = _U32.unpack_from(cont, 4)[0]
        return data

    def table_def(self, name: str, page_no: int) -> _TableDef:
        raw = self.tdef_bytes(page_no)
        version = 3 if self.jet3 else 4
        try:
            head = parse_table_head(raw, version=version)
            body = parse_table_data(
                raw[head.tdef_header_end :],
                head.index_count,
                head.real_index_count,
                head.column_count,
                version=version,
            )
        except (ConstructError, ValueError, UnicodeDecodeError) as exc:
            raise Acade2SqlError(f"Cannot parse the definition of table {name!r}: {exc}") from None
        columns = [
            self._column(col, body.column_names[i].col_name_str)
            for i, col in enumerate(body.column)
        ]
        columns.sort(key=lambda c: c.display)
        by_number = {c.number: c.name for c in columns}
        fmt = 0 if self.jet3 else 1
        real_start = (
            head.tdef_header_end
            + head.real_index_count * _REAL_INDEX_SIZE[fmt]
            + head.column_count * _COLUMN_SIZE[fmt]
            + sum(_NAME_PREFIX[fmt] + n.col_name_len for n in body.column_names)
        )

        def real_flags(k: int) -> int:
            pos = real_start + k * _REAL_INDEX2_SIZE[fmt] + _REAL_INDEX2_FLAGS[fmt]
            if pos >= len(raw):
                raise Acade2SqlError(f"Cannot parse the indexes of table {name!r}")
            return raw[pos]

        indexes = []
        for i, logical in enumerate(body.all_indexes):
            if logical.idx_type == _INDEX_FOREIGN_KEY:
                continue
            if not 0 <= logical.idx_col_num < len(body.real_index_2):
                raise Acade2SqlError(f"Cannot parse the indexes of table {name!r}")
            real = body.real_index_2[logical.idx_col_num]
            cols = tuple(
                by_number[c.col_id]
                for c in real.unk_struct
                if c.col_id != _NO_COLUMN and c.col_id in by_number
            )
            if cols:
                indexes.append(
                    IndexInfo(
                        name=body.index_names[i].idx_name_str,
                        columns=cols,
                        unique=bool(real_flags(logical.idx_col_num) & _INDEX_UNIQUE),
                    )
                )
        return _TableDef(
            name=name,
            page=page_no,
            columns=tuple(columns),
            indexes=tuple(indexes),
            row_count=int(head.number_of_rows),
            usage_map=int(head.row_page_map),
        )

    def _column(self, col: Any, name: str) -> _Column:
        flags = col.column_flags
        various = col.various if isinstance(col.various, dict) else {}
        calculated = bool(not self.jet3 and (flags.unk_3 or flags.unk_4))
        return _Column(
            name=name,
            number=int(col.column_id),
            var_number=int(col.variable_column_number),
            display=int(col.column_index),
            type_code=int(col.type),
            fixed=bool(flags.fixed_length),
            fixed_offset=int(col.fixed_offset),
            length=int(col.length),
            precision=int(various.get("prec", 0) or 0),
            scale=int(various.get("scale", 0) or 0),
            autonumber=bool(flags.autonumber),
            calculated=calculated,
        )


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------


def access_datetime(days: float) -> dt.datetime:
    """Access DATETIME (days since 1899-12-30; the time is the absolute fraction, also for
    negative dates) to ``datetime`` in whole seconds, exactly as the Access ODBC driver returns it
    (including its calendar before 1753, see :func:`_driver_date_before_1753`)."""
    if not math.isfinite(days):
        raise ValueError(f"invalid date value {days!r}")
    whole = math.trunc(days)
    seconds = math.floor(abs(days - whole) * 86400 + _SECONDS_EPSILON)
    try:
        if whole < _DRIVER_CALENDAR_START:
            return _driver_date_before_1753(whole) + dt.timedelta(seconds=seconds)
        return _EPOCH + dt.timedelta(days=whole, seconds=seconds)
    except (OverflowError, ValueError):
        raise ValueError(f"date value {days!r} is out of range") from None


_DRIVER_CALENDAR_START = (dt.datetime(1753, 1, 1) - _EPOCH).days  # -53688


def _driver_date_before_1753(whole: int) -> dt.datetime:
    """The Access ODBC driver's date for a day serial before 1753-01-01 (DECISIONS #39).

    It counts back from 1753-01-01 in 365-day years and maps the day of the year with that year's
    (Gregorian) month lengths, so 31 December of leap years never occurs and dates drift from the
    proleptic Gregorian calendar Access itself displays (serial -657434: Access 0100-01-01, driver
    0098-11-26). Matches the driver on 21,382 sampled serials between 0098 and 1753.
    """
    back = _DRIVER_CALENDAR_START - whole  # >= 1
    years = -(-back // 365)
    year = 1753 - years
    day_of_year = 365 * years - back  # 0-based, 0..364
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month = 1
    for length in (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31):
        if day_of_year < length:
            break
        day_of_year -= length
        month += 1
    return dt.datetime(year, month, day_of_year + 1)


def _decimal(negative: bool, magnitude: int, scale: int) -> Decimal:
    digits = tuple(int(d) for d in str(magnitude))
    return Decimal((1 if negative else 0, digits, -scale))


FixedDecoder = Callable[[bytes, int], Any]
VarDecoder = Callable[[bytes], Any]


def _fixed_decoder(col: _Column, jet: _JetFile) -> FixedDecoder:
    t = col.type_code
    if t == T_BYTE:
        return lambda r, p: r[p]
    if t == T_INT:
        return lambda r, p: _I16.unpack_from(r, p)[0]
    if t == T_LONG:
        return lambda r, p: _I32.unpack_from(r, p)[0]
    if t == T_BIGINT:
        return lambda r, p: _I64.unpack_from(r, p)[0]
    if t == T_MONEY:
        return lambda r, p: _money(_I64.unpack_from(r, p)[0])
    if t == T_FLOAT:
        return lambda r, p: _F32.unpack_from(r, p)[0]
    if t == T_DOUBLE:
        return lambda r, p: _F64.unpack_from(r, p)[0]
    if t == T_DATETIME:
        return lambda r, p: access_datetime(_F64.unpack_from(r, p)[0])
    if t == T_GUID:
        return lambda r, p: str(uuid.UUID(bytes_le=r[p : p + 16])).upper()
    if t == T_NUMERIC:
        scale = col.scale
        return lambda r, p: _numeric(r, p, scale)
    var = _var_decoder(col, jet)
    length = col.length
    return lambda r, p: var(r[p : p + length])


def _var_bigint(b: bytes) -> int:
    if len(b) != 8:
        raise ValueError(f"BIGINT value of {len(b)} bytes (expected 8)")
    return _I64.unpack(b)[0]


def _money(value: int) -> Decimal:
    return _decimal(value < 0, abs(value), 4)


def _numeric(record: bytes, pos: int, scale: int) -> Decimal:
    sign, a, b, c, d = _NUMERIC.unpack_from(record, pos)
    return _decimal(bool(sign & 0x80), (a << 96) | (b << 64) | (c << 32) | d, scale)


def _var_decoder(col: _Column, jet: _JetFile) -> VarDecoder:
    t = col.type_code
    version, codepage = jet.version, jet.codepage
    if t == T_TEXT:
        return lambda b: decode_jet_text(b, version, codepage)
    if t == T_MEMO:
        return lambda b: decode_jet_text(jet.long_value(b), version, codepage) if b else ""
    if t == T_OLE:
        return lambda b: jet.long_value(b) if b else b""
    if t == T_BINARY:
        return bytes
    if t == T_NUMERIC:
        scale = col.scale
        return lambda b: _numeric(b, 0, scale)
    if t == T_GUID:
        return lambda b: str(uuid.UUID(bytes_le=b[:16])).upper()
    if t == T_BIGINT:  # variable-length BIGINT (CREATE TABLE ... BIGINT via the ODBC driver)
        return _var_bigint
    raise UnsupportedAccessFeature(f"column {col.name!r}: unsupported variable-length type {t}")


@dataclass(frozen=True)
class _Plan:
    """Per-column decoding plan, in ordinal order."""

    names: tuple[str, ...]
    numbers: tuple[int, ...]
    kinds: tuple[int, ...]  # 0 bool, 1 fixed, 2 variable
    positions: tuple[int, ...]  # fixed offset (fixed) or variable slot (variable)
    fixed: tuple[FixedDecoder | None, ...]
    var: tuple[VarDecoder | None, ...]

    @cached_property
    def first_var(self) -> int:
        """Lowest column number of a variable-length column (a huge number when none).

        A row stores the variable-length offset table only if it was written when at least one
        variable-length column existed, i.e. when its column count exceeds this number (rows
        written before ``ALTER TABLE ... ADD COLUMN <text>`` have none)."""
        numbers = [n for n, k in zip(self.numbers, self.kinds, strict=True) if k == _KIND_VAR]
        return min(numbers, default=1 << 30)

    def column(self, k: int) -> _Plan:
        """The plan of column ``k`` alone (to name the column that fails to decode)."""
        return _Plan(
            names=self.names[k : k + 1],
            numbers=self.numbers[k : k + 1],
            kinds=self.kinds[k : k + 1],
            positions=self.positions[k : k + 1],
            fixed=self.fixed[k : k + 1],
            var=self.var[k : k + 1],
        )


_KIND_BOOL, _KIND_FIXED, _KIND_VAR = 0, 1, 2


class _RowFormatError(ValueError):
    """The row's own layout (null mask, variable-length offset table) is corrupt."""


def _plan(tdef: _TableDef, jet: _JetFile) -> _Plan:
    kinds, positions, fixed, var = [], [], [], []
    for col in tdef.columns:
        if col.type_code == T_BOOL:
            kinds.append(_KIND_BOOL)
            positions.append(0)
            fixed.append(None)
            var.append(None)
        elif col.fixed:
            kinds.append(_KIND_FIXED)
            positions.append(jet.row_count_size + col.fixed_offset)
            fixed.append(_fixed_decoder(col, jet))
            var.append(None)
        else:
            kinds.append(_KIND_VAR)
            positions.append(col.var_number)
            fixed.append(None)
            var.append(_var_decoder(col, jet))
    return _Plan(
        names=tuple(c.name for c in tdef.columns),
        numbers=tuple(c.number for c in tdef.columns),
        kinds=tuple(kinds),
        positions=tuple(positions),
        fixed=tuple(fixed),
        var=tuple(var),
    )


def _check_offsets(offsets: tuple[int, ...], low: int, high: int) -> tuple[int, ...]:
    """Variable-length offsets must be non-decreasing and inside ``[low, high]``."""
    previous = low
    for value in offsets:
        if value < previous:
            raise _RowFormatError(f"variable-length offsets are not monotonic: {list(offsets)}")
        previous = value
    if previous > high:
        raise _RowFormatError(f"variable-length offsets {list(offsets)} run past the row data")
    return offsets


def _var_offsets_jet4(r: bytes, mask_start: int) -> tuple[int, ...]:
    if mask_start < 4:
        raise _RowFormatError("row too short for a variable-length offset table")
    count = _U16.unpack_from(r, mask_start - 2)[0]
    first = mask_start - 4 - 2 * count  # offsets are stored backwards from the end
    if first < 2:
        raise _RowFormatError("variable-length offset table overruns the row")
    offsets = struct.unpack_from(f"<{count + 1}H", r, first)[::-1]
    return _check_offsets(offsets, 2, first)


def _var_offsets_jet3(r: bytes, mask_start: int, mask_len: int) -> tuple[int, ...]:
    """Jet 3: one-byte offsets plus a jump table for rows over 256 bytes (as mdbtools)."""
    row_end = len(r) - 1
    if mask_start < 2:
        raise _RowFormatError("row too short for a variable-length offset table")
    count = r[mask_start - 1]
    jumps = row_end // 256
    col_ptr = row_end - mask_len - jumps - 1
    if (col_ptr - count) // 256 < jumps:
        jumps -= 1
    if col_ptr < count or col_ptr >= len(r):
        raise _RowFormatError("bad Jet 3 variable-length offset table")
    used = 0
    out = []
    for i in range(count + 1):
        while used < jumps and i == r[row_end - mask_len - used - 1]:
            used += 1
        out.append(r[col_ptr - i] + used * 256)
    return _check_offsets(tuple(out), 1, col_ptr - count)


def _crack(r: bytes, plan: _Plan, jet3: bool) -> tuple:
    """One row's values (raises on undecodable data; the caller adds table/column context)."""
    n = len(r)
    if n < (1 if jet3 else 2):
        raise _RowFormatError(f"row of {n} bytes has no column count")
    ncols = r[0] if jet3 else _U16.unpack_from(r, 0)[0]
    mask_len = (ncols + 7) // 8
    mask_start = n - mask_len
    if mask_start < (1 if jet3 else 2):
        raise _RowFormatError(f"row of {n} bytes is shorter than its null mask ({ncols} columns)")
    offsets: tuple[int, ...] = ()
    if ncols > plan.first_var:  # the row was written with a variable-length column
        offsets = (
            _var_offsets_jet3(r, mask_start, mask_len) if jet3 else _var_offsets_jet4(r, mask_start)
        )
    nvar = len(offsets) - 1
    out: list[Any] = []
    for k, kind in enumerate(plan.kinds):
        number = plan.numbers[k]
        present = number < ncols and (r[mask_start + (number >> 3)] >> (number & 7)) & 1
        if kind == _KIND_BOOL:
            out.append(bool(present))
        elif not present:
            out.append(None)  # NULL, or a column added after this row was written
        elif kind == _KIND_FIXED:
            out.append(plan.fixed[k](r, plan.positions[k]))  # type: ignore[misc]
        else:
            slot = plan.positions[k]
            if slot >= nvar:
                raise ValueError(
                    f"value marked present but the row has no variable-length slot {slot} "
                    f"(row has {nvar})"
                )
            out.append(plan.var[k](r[offsets[slot] : offsets[slot + 1]]))  # type: ignore[misc]
    return tuple(out)


# ---------------------------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CatalogEntry:
    name: str
    page: int
    system: bool


class AccessParserReader(Reader):
    """Pure-Python Access reader: no ODBC driver, no lock file, the file is only read."""

    kind = "pure"

    def __init__(self, path: str | Path, *, copy_to_temp: bool = False) -> None:
        self.path = Path(path)
        self._temp: TempCopy | None = None
        self._jet: _JetFile | None = None
        self._catalog_cache: dict[str, _CatalogEntry] | None = None
        source = validate_source(self.path)
        _warn_if_open_elsewhere(source, copy_to_temp)
        try:
            if copy_to_temp:
                self._temp = TempCopy(source)
                source = self._temp.path
            self._jet = _JetFile(source)
        except BaseException:
            self.close()
            raise
        log.debug(
            "opened access db (pure reader)",
            extra={"kv": {"path": str(self.path), "version": self._jet.version}},
        )

    def close(self) -> None:
        if self._jet is not None:
            self._jet.close()
            self._jet = None
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None

    def _file(self) -> _JetFile:
        if self._jet is None:
            raise Acade2SqlError(f"Reader for {self.path} is closed")
        return self._jet

    # -- catalog -------------------------------------------------------------------------------

    def _catalog(self) -> dict[str, _CatalogEntry]:
        if self._catalog_cache is None:
            jet = self._file()
            tdef = jet.table_def("MSysObjects", _CATALOG_PAGE)
            wanted = ("Id", "Name", "Type", "Flags")
            missing = [c for c in wanted if c not in {col.name for col in tdef.columns}]
            if missing:
                raise Acade2SqlError(f"Corrupt catalog in {self.path}: missing {missing}")
            positions = [next(i for i, c in enumerate(tdef.columns) if c.name == w) for w in wanted]
            entries: dict[str, _CatalogEntry] = {}
            for row in self._rows(tdef):
                obj_id, name, obj_type, flags = (row[i] for i in positions)
                if obj_type != _OBJ_TABLE or not name or obj_id is None:
                    continue
                flags = (flags or 0) & 0xFFFFFFFF
                if flags & _FLAG_UNLISTED:
                    continue
                system = bool(flags & (_FLAG_SYSTEM | _FLAG_SYSTEM_ALT))
                entries[name] = _CatalogEntry(name, obj_id & 0x00FFFFFF, system)
            self._catalog_cache = entries
        return self._catalog_cache

    def list_tables(self, include_system: bool = False) -> list[str]:
        names = [
            e.name
            for e in self._catalog().values()
            if not e.system and not e.name.lower().startswith("msys")
        ]
        if include_system:
            names = list(self._catalog())
        return sorted(set(names), key=lambda n: (n.casefold(), n))

    def _resolve(self, name: str) -> _CatalogEntry:
        """User tables only: MSys* tables are not resolvable (DECISIONS #13), as with ODBC."""
        return self._catalog()[resolve_table(name, self.list_tables(), self.path)]

    def _table_def(self, name: str) -> _TableDef:
        entry = self._resolve(name)
        tdef = self._file().table_def(entry.name, entry.page)
        for col in tdef.columns:
            what = _UNSUPPORTED_TYPES.get(col.type_code)
            if what is None and col.type_code not in _TYPE_INFO:
                what = f"unknown column type {col.type_code}"
            if what is None and col.calculated:
                what = "calculated field"
            if what is None and col.type_code == T_BIGINT and not col.fixed and col.length != 8:
                what = f"variable-length BIGINT of length {col.length}"
            if what is not None:
                raise UnsupportedAccessFeature(
                    f"Table {entry.name!r} column {col.name!r}: {what} is not supported by the "
                    "pure reader; use the ODBC reader (--reader odbc) or exclude the table."
                )
        return tdef

    def get_table(self, name: str) -> TableInfo:
        tdef = self._table_def(name)
        jet3 = self._file().jet3
        columns = tuple(_column_info(c, i, jet3) for i, c in enumerate(tdef.columns, 1))
        return build_table_info(tdef.name, columns, tdef.indexes)

    # -- data ----------------------------------------------------------------------------------

    def count_rows(self, name: str) -> int:
        """Live rows on the table's pages; :class:`SourceIntegrityError` when that differs from
        the TDEF row count or a listed page is not the table's (never a partial count)."""
        tdef = self._table_def(name)
        return sum(1 for _ in self._file().records(tdef, resolve=False))

    def iter_rows(self, name: str, batch_size: int = 5000) -> Iterator[list[tuple]]:
        check_batch_size(batch_size)
        return self._batches(self._table_def(name), batch_size)

    def _batches(self, tdef: _TableDef, batch_size: int) -> Iterator[list[tuple]]:
        batch: list[tuple] = []
        total = 0
        for row in self._rows(tdef):
            batch.append(row)
            if len(batch) >= batch_size:
                total += len(batch)
                yield batch
                batch = []
        if batch:
            total += len(batch)
            yield batch
        log.debug("read table", extra={"kv": {"table": tdef.name, "rows": total}})

    def _rows(self, tdef: _TableDef) -> Iterator[tuple]:
        jet = self._file()
        plan = _plan(tdef, jet)
        jet3 = jet.jet3
        records = jet.page_records(tdef)
        page_no: int | None = None
        while True:
            try:
                page_no, record = next(records)
            except StopIteration:
                return
            except Exception as exc:  # backstop: page_records already names table and page
                raise _scan_error(tdef, page_no, exc) from None
            try:
                values = _crack(record, plan, jet3)
            except Exception as exc:
                raise _value_error(tdef, plan, record, page_no, jet3, exc) from None
            yield values

    @classmethod
    def _inspect_only(cls, source: Path) -> AccessParserReader:
        """A reader on ``source`` as-is: no temp copy, no lock-file warning (used by the ODBC
        reader's cross-check, which already holds the file open through the driver)."""
        self = cls.__new__(cls)
        self.path = source
        self._temp = None
        self._jet = None
        self._catalog_cache = None
        self._jet = _JetFile(source)
        return self


def column_type_codes(source: str | Path, table: str) -> dict[str, int]:
    """Access type code (TDEF) of every column of ``table`` in ``source``, read-only.

    Unlike the pure reader's ``get_table`` this does not reject unsupported column types: it only
    reports what the table definition says. Raises on an unparseable file or unknown table.
    """
    reader = AccessParserReader._inspect_only(Path(source))
    try:
        entry = reader._resolve(table)
        tdef = reader._file().table_def(entry.name, entry.page)
        return {col.name: col.type_code for col in tdef.columns}
    finally:
        reader.close()


_LOCK_SUFFIX = {".mdb": ".ldb", ".mde": ".ldb", ".accdb": ".laccdb", ".accde": ".laccdb"}
LIVE_WRITER_WARNING = (
    "the source database appears to be open in another program (lock file {lock} exists): the "
    "pure reader reads the file as it is on disk, so a write in progress can make tables "
    "inconsistent. Close AutoCAD Electrical/Access first, or use --copy-to-temp "
    "(copy_to_temp=True)."
)


def lock_file(source: Path) -> Path | None:
    """The Access lock file (``.ldb``/``.laccdb``) next to ``source``, if one exists."""
    known = _LOCK_SUFFIX.get(source.suffix.lower())
    for suffix in (known,) if known else (".ldb", ".laccdb"):
        candidate = source.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def _warn_if_open_elsewhere(source: Path, copy_to_temp: bool) -> None:
    """A lock file means someone (ACADE, Access) has the database open, possibly writing.

    Without ``copy_to_temp``: one WARNING recommending to close ACADE or use --copy-to-temp.
    With it the copy is taken first, as always (INFO: the copy is a point-in-time snapshot).
    """
    lock = lock_file(source)
    if lock is None:
        return
    if copy_to_temp:
        log.info(
            "source is open in another program; reading a temporary copy",
            extra={"kv": {"lock": str(lock)}},
        )
        return
    log.warning(LIVE_WRITER_WARNING.format(lock=lock))


def _scan_error(tdef: _TableDef, page: int | None, exc: Exception) -> Acade2SqlError:
    """Table/page context for a failure while locating rows (never a bare low-level error)."""
    if isinstance(exc, Acade2SqlError) and str(exc).startswith("Table "):
        return exc
    where = "its row usage map" if page is None else f"data page {page}"
    return Acade2SqlError(
        f"Table {tdef.name!r}: cannot read {where} ({type(exc).__name__}: {exc}); nothing was "
        "guessed. Use the ODBC reader (--reader odbc) for this table."
    )


def _value_error(
    tdef: _TableDef, plan: _Plan, record: bytes, page: int, jet3: bool, exc: Exception
) -> Acade2SqlError:
    """Name the column that failed (re-decoding the row column by column)."""
    if isinstance(exc, Acade2SqlError) and str(exc).startswith("Table "):
        return exc
    if isinstance(exc, _RowFormatError):
        return Acade2SqlError(
            f"Table {tdef.name!r}: corrupt row on data page {page} ({exc}); nothing was "
            "guessed. Use the ODBC reader (--reader odbc) for this table."
        )
    column = "?"
    for k in range(len(plan.names)):
        try:
            _crack(record, plan.column(k), jet3)
        except Exception:
            column = plan.names[k]
            break
    return Acade2SqlError(
        f"Table {tdef.name!r} column {column!r}: cannot decode a value on data page {page} "
        f"({exc}); nothing was guessed. Use the ODBC reader (--reader odbc) for this table."
    )


def _column_info(col: _Column, ordinal: int, jet3: bool) -> ColumnInfo:
    access_type, data_type, size, digits = _TYPE_INFO[col.type_code]
    if col.type_code == T_LONG and col.autonumber:
        access_type = "COUNTER"
    if col.type_code == T_TEXT:
        size = col.length if jet3 else col.length // 2
        if col.fixed:  # CHAR(n): fixed-length text, as the driver reports it
            access_type, data_type = "CHAR", -8
    elif col.type_code == T_BINARY:
        size = col.length
        if not col.fixed:  # BINARY(n) is fixed-length (zero-padded), VARBINARY(n) is not
            access_type, data_type = "VARBINARY", -3
    elif col.type_code == T_NUMERIC:
        size, digits = col.precision, col.scale
    return ColumnInfo(
        name=col.name,
        ordinal=ordinal,
        access_type=access_type,
        data_type=data_type,
        size=size,
        decimal_digits=digits,
        # As the driver reports it (DECISIONS #11): only COUNTER and BIT are NOT NULL.
        nullable=access_type not in ("COUNTER", "BIT"),
        autoincrement=access_type == "COUNTER",
    )
