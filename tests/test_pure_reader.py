# ruff: noqa: RUF001
"""The pure-Python reader (docs/M5_SPEC.md sections 0-1): text decoding, equivalence with the ODBC
reader on the synthetic fixture (.mdb and .accdb), and the no-lock / read-only guarantees.

The reader contract tests themselves (rows, types, NULLs, batching, ...) run for both backends in
``test_pyodbc_reader.py`` through the parametrised ``reader`` fixture.
"""

from __future__ import annotations

import builtins
import datetime as dt
import decimal
import io
import itertools
import os
import shutil
from pathlib import Path

import pytest
from tests.fixtures.build_access import MANIFEST

from acade2sql.errors import Acade2SqlError
from acade2sql.readers import open_reader
from acade2sql.readers.access_parser_reader import AccessParserReader, decode_jet_text
from acade2sql.validate import table_checksum

# ----------------------------------------------------------------------------------------------
# decode_jet_text: hand-built bytes, no database needed
# ----------------------------------------------------------------------------------------------

FF_FE = b"\xff\xfe"


def u16(text: str) -> bytes:
    return text.encode("utf-16-le")


def comp(*parts: bytes | str) -> bytes:
    """Compressed value: FF FE prefix then the parts (str = compressed bytes, bytes = raw)."""
    return FF_FE + b"".join(p.encode("latin-1") if isinstance(p, str) else p for p in parts)


def uncomp(text: str) -> bytes:
    """An uncompressed run inside a compressed value: 00, UTF-16LE data, then a single 00.

    This is what Access writes (verified against real files: ``41 00 13 20 00 42`` is 'A', an
    en dash as ``13 20``, 'B'). A lone ``00`` toggles in both directions.
    """
    return b"\x00" + u16(text) + b"\x00"


@pytest.mark.parametrize("version", [4, 5])
class TestDecodeJet4:
    def test_compressed_ascii(self, version):
        assert decode_jet_text(comp("ABC def"), version) == "ABC def"

    def test_prefix_only_is_empty(self, version):
        assert decode_jet_text(FF_FE, version) == ""

    def test_empty_bytes(self, version):
        assert decode_jet_text(b"", version) == ""

    def test_compressed_latin1_byte_is_one_char(self, version):
        assert decode_jet_text(comp(b"\xb5\xb0\xe9\xd8"), version) == "µ°éØ"

    def test_toggle_in_the_middle(self, version):
        raw = comp("A", uncomp("–"), "B")
        assert decode_jet_text(raw, version) == "A–B"

    def test_regression_en_dash_is_not_x13(self, version):
        # the access-parser 0.0.6 bug: U+2013 came back as '\x13'
        got = decode_jet_text(comp(b"\x00\x13\x20\x00"), version)
        assert got == "–" and got != "\x13"

    def test_toggle_at_start(self, version):
        raw = comp(uncomp("日本語"), "abc")
        assert decode_jet_text(raw, version) == "日本語abc"

    def test_toggle_at_end_with_terminator(self, version):
        raw = comp("abc", uncomp("日本"))
        assert decode_jet_text(raw, version) == "abc日本"

    def test_uncompressed_run_ends_with_the_data(self, version):
        # no closing 00: the value simply ends while in uncompressed mode
        raw = comp("abc", b"\x00" + u16("日本"))
        assert decode_jet_text(raw, version) == "abc日本"

    def test_several_toggles(self, version):
        raw = comp("x", uncomp("–"), "y", uncomp("–"), "z")
        assert decode_jet_text(raw, version) == "x–y–z"

    def test_consecutive_non_latin1_characters_in_one_run(self, version):
        raw = comp("a", uncomp("日本語テキスト"), "b")
        assert decode_jet_text(raw, version) == "a日本語テキストb"

    def test_alternating(self, version):
        parts: list[bytes | str] = []
        expected = ""
        for _ in range(10):
            parts += ["a", uncomp("日")]
            expected += "a日"
        assert decode_jet_text(comp(*parts), version) == expected

    def test_long_uncompressed_run(self, version):
        raw = comp(uncomp("–" * 50))
        assert decode_jet_text(raw, version) == "–" * 50

    def test_surrogate_pair_in_uncompressed_run(self, version):
        # U+1F601 = D83D DE01: no 0x00 byte inside the pair
        raw = comp("x", uncomp("\U0001f601"), "y")
        assert decode_jet_text(raw, version) == "x\U0001f601y"

    def test_latin1_next_to_uncompressed(self, version):
        raw = comp(b"\xe9", uncomp("–"), b"\xe9")
        assert decode_jet_text(raw, version) == "é–é"

    def test_uncompressed_without_prefix(self, version):
        for text in ("日本語テキスト", "µ · – Ω °", "A", "emoji \U0001f600 ok", "\r\n"):
            assert decode_jet_text(u16(text), version) == text

    def test_uncompressed_value_with_embedded_zero_bytes_is_not_a_toggle(self, version):
        # 'A' = 41 00, U+0100 = 00 01: a 00 byte inside a UTF-16 pair is not a mode switch
        text = "AĀB"
        assert decode_jet_text(u16(text), version) == text

    def test_odd_trailing_byte_is_an_error(self, version):
        with pytest.raises(Acade2SqlError):
            decode_jet_text(u16("AB") + b"\x41", version)

    def test_odd_length_uncompressed_is_an_error(self, version):
        with pytest.raises(Acade2SqlError):
            decode_jet_text(b"\x41", version)

    def test_odd_byte_inside_uncompressed_run_is_an_error(self, version):
        with pytest.raises(Acade2SqlError):
            decode_jet_text(comp("a", b"\x00\x13"), version)  # half a UTF-16 unit, then EOF

    def test_lone_surrogate_is_an_error_not_a_replacement(self, version):
        with pytest.raises(Acade2SqlError):
            decode_jet_text(b"\x00\xd8", version)
        with pytest.raises(Acade2SqlError):
            decode_jet_text(comp("a", b"\x00\x3d\xd8", b"\x00"), version)

    def test_never_silently_lossy(self, version):
        # whatever is returned must round-trip: no '�', and nothing dropped
        for raw in (comp("ab", uncomp("日"), "cd"), u16("日本abc")):
            out = decode_jet_text(raw, version)
            assert "�" not in out and len(out) >= 4

    @pytest.mark.parametrize(
        ("hexstr", "expected"),
        [  # captured from the synthetic Access fixture (.accdb), see tests/fixtures/build_access.py
            ("ff fe 41 00 13 20 00 42 74 61 69 6c", "A–Btail"),
            ("ff fe 00 13 20 00 61 62 63 64 65 66", "–abcdef"),
            ("ff fe 61 62 63 64 65 66 00 13 20", "abcdef–"),
            ("ff fe 00 e5 65 2c 67 9e 8a 00 61 62 63 74 61 69 6c", "日本語abctail"),
            ("ff fe 61 62 63 00 e5 65 2c 67", "abc日本"),
            ("ff fe e9 00 13 20 00 e9 74 61 69 6c", "é–étail"),
            ("ff fe 78 00 13 20 00 79 00 13 20 00 7a", "x–y–z"),
            ("ff fe b5 20 b7 20 00 13 20 00 20 00 a9 03 00 20 b0", "µ · – Ω °"),
            (
                "ff fe 00 ac 20 00 35 20 00 13 20 00 20 00 1c 20 00 71 75 6f 74 65 64 00 1d 20 "
                "00 20 00 26 20",
                "€5 – “quoted” …",
            ),
            ("13 20 74 00 61 00 69 00 6c 00", "–tail"),  # not compressed at all
        ],
    )
    def test_bytes_captured_from_real_access_files(self, version, hexstr, expected):
        assert decode_jet_text(bytes.fromhex(hexstr), version) == expected

    def test_returns_str(self, version):
        assert type(decode_jet_text(comp("a"), version)) is str


class TestDecodeJet3:
    def test_cp1252(self):
        assert decode_jet_text(b"\xb5 \x96 \xe9 \x80", 3) == "µ – é €"

    def test_ascii(self):
        assert decode_jet_text(b"plain", 3) == "plain"

    def test_empty(self):
        assert decode_jet_text(b"", 3) == ""

    def test_ff_fe_is_not_a_compression_marker_in_jet3(self):
        assert decode_jet_text(b"\xff\xfeA", 3) == "ÿþA"

    def test_undefined_cp1252_byte_is_an_error_not_dropped(self):
        with pytest.raises(Acade2SqlError):
            decode_jet_text(b"a\x81b", 3)  # 0x81 is undefined in cp1252


# ----------------------------------------------------------------------------------------------
# equivalence pure == odbc on the synthetic fixture (.mdb and .accdb)
# ----------------------------------------------------------------------------------------------


def _rows(reader, name, batch_size=5000):
    return list(itertools.chain.from_iterable(reader.iter_rows(name, batch_size=batch_size)))


def _same_value(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, decimal.Decimal):
        return a.as_tuple() == b.as_tuple()  # same digits and scale, not just numerically equal
    if isinstance(a, float):
        return repr(a) == repr(b)
    return a == b


@pytest.fixture
def both(access_db):
    with (
        open_reader(access_db, reader="odbc") as o,
        open_reader(access_db, reader="pure") as p,
    ):
        yield o, p


@pytest.mark.requires_driver
def test_pure_reader_class(access_db):
    with open_reader(access_db, reader="pure") as r:
        assert isinstance(r, AccessParserReader)


@pytest.mark.requires_driver
def test_same_table_list(both):
    o, p = both
    assert p.list_tables() == o.list_tables()
    assert set(p.list_tables()) == set(MANIFEST)


@pytest.mark.requires_driver
@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_same_schema(both, name):
    o, p = both
    io_, ip = o.get_table(name), p.get_table(name)
    assert ip.name == io_.name
    assert ip.column_names == io_.column_names
    assert [c.ordinal for c in ip.columns] == [c.ordinal for c in io_.columns]
    assert ip.column_types == io_.column_types
    for co, cp in zip(io_.columns, ip.columns, strict=True):
        assert cp.access_type == co.access_type, co.name
        assert cp.data_type == co.data_type, co.name
        assert cp.autoincrement == co.autoincrement, co.name
        assert (cp.size, cp.decimal_digits) == (co.size, co.decimal_digits), co.name


@pytest.mark.requires_driver
@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_same_count(both, name):
    o, p = both
    assert p.count_rows(name) == o.count_rows(name) == MANIFEST[name]["count"]


@pytest.mark.requires_driver
@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_same_values_and_python_types(both, name):
    o, p = both
    cols = o.get_table(name).column_names
    ro, rp = _rows(o, name), _rows(p, name)
    assert len(rp) == len(ro)
    for i, (a, b) in enumerate(zip(rp, ro, strict=True)):
        assert type(a) is tuple and len(a) == len(b) == len(cols)
        for j, (x, y) in enumerate(zip(a, b, strict=True)):
            assert _same_value(x, y), f"{name} row {i} col {cols[j]}: pure={x!r} odbc={y!r}"


@pytest.mark.requires_driver
@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_same_column_aware_checksum(both, name):
    o, p = both
    types = o.get_table(name).column_types
    co = table_checksum(o.iter_rows(name, batch_size=777), types)
    cp = table_checksum(p.iter_rows(name, batch_size=777), p.get_table(name).column_types)
    assert cp == co


@pytest.mark.requires_driver
@pytest.mark.parametrize("name", [n for n in sorted(MANIFEST) if "rows" in MANIFEST[n]])
def test_pure_matches_manifest_exactly(pure_reader, name):
    assert _rows(pure_reader, name) == MANIFEST[name]["rows"]


@pytest.mark.requires_driver
def test_compressed_unicode_runs_exact(pure_reader):
    # the access-parser 0.0.6 failure: '\x13' for U+2013, 'å'/'©' garbage, silent loss
    rows = _rows(pure_reader, "UNIRUNS")
    expected = MANIFEST["UNIRUNS"]["rows"]
    assert rows == expected
    flat = "".join(v for r in rows for v in r[1:] if isinstance(v, str))
    assert "\x13" not in flat and "�" not in flat
    assert "–" in flat


@pytest.mark.requires_driver
def test_long_memo_with_periodic_non_latin1(pure_reader):
    from tests.fixtures.build_access import UNI_MEMO_LONG, UNI_MEMO_LONG2

    by_s = {r[1]: r[2] for r in _rows(pure_reader, "UNIRUNS")}
    assert len(by_s["long1"]) == len(UNI_MEMO_LONG) > 4000
    assert by_s["long1"] == UNI_MEMO_LONG
    assert by_s["long2"] == UNI_MEMO_LONG2


@pytest.mark.requires_driver
def test_dates_before_1899_epoch_and_extremes(both):
    o, p = both
    rp, ro = _rows(p, "DATES"), _rows(o, "DATES")
    assert rp == ro == MANIFEST["DATES"]["rows"]
    assert all(type(r[1]) is dt.datetime for r in rp if r[1] is not None)
    assert rp[-1][1] is None
    # whole seconds: no float noise like 23:59:59.000005
    assert all(r[1].microsecond == 0 for r in rp if r[1] is not None)
    assert dt.datetime(1899, 12, 30) in [r[1] for r in rp]  # the epoch itself, not "(Empty Date)"


@pytest.mark.requires_driver
def test_typed_every_type_equal_and_exact(both):
    o, p = both
    rp, ro = _rows(p, "TYPED"), _rows(o, "TYPED")
    assert rp == ro == MANIFEST["TYPED"]["rows"]
    cols = p.get_table("TYPED").column_names
    first = dict(zip(cols, rp[0], strict=True))
    assert type(first["YN"]) is bool and type(first["BIN"]) is bytes
    assert type(first["CUR"]) is decimal.Decimal and type(first["DEC"]) is decimal.Decimal
    assert first["CUR"].as_tuple().exponent == -4  # CURRENCY keeps 4 decimals
    assert first["DEC"].as_tuple().exponent == -4  # DECIMAL(18,4) keeps its scale
    assert first["G"] == "12345678-1234-1234-1234-123456789ABC"  # unbraced, upper-case
    assert rp[0][cols.index("BIN")] == b"\x00\x01\xff"  # bytes with \x00
    assert [r[cols.index("YN")] for r in rp] == [True, False, False, True]  # BIT is never None
    assert rp[2][cols.index("DT")] == dt.datetime(1899, 12, 30, 13, 45, 30)  # time only


@pytest.mark.requires_driver
def test_null_vs_empty_string_and_padding(pure_reader):
    rows = {r[0]: r for r in _rows(pure_reader, "XX")}
    assert rows["P-005"][2:5] == ("", "", "")
    assert rows["P-006"][2:5] == (None, None, None)
    assert rows["P-000"][2] == "  ABC  "
    assert rows["P-004"][2] == "line1\r\nline2"
    uni = {r[1]: r for r in _rows(pure_reader, "UNIRUNS")}
    assert uni[""][2] == "tail"
    assert any(r[1] is None and r[2] is None for r in uni.values())


@pytest.mark.requires_driver
def test_batches_equal_across_readers(both):
    o, p = both
    for size in (1, 7, 5000):
        assert [len(b) for b in p.iter_rows("BIG", batch_size=size)] == [
            len(b) for b in o.iter_rows("BIG", batch_size=size)
        ]


# ----------------------------------------------------------------------------------------------
# documented differences (indexes / PK / nullable / sizes): the documented value is tested
# ----------------------------------------------------------------------------------------------


@pytest.mark.requires_driver
def test_pure_indexes_documented(pure_reader):
    """Documented in access_parser_reader.py: see the module docstring of the reader."""
    for name in MANIFEST:
        info = pure_reader.get_table(name)
        if info.indexes:  # the reader discovers indexes: they must be the real ones
            for ix in info.indexes:
                assert set(ix.columns) <= set(info.column_names)
        else:
            counters = [c.name for c in info.columns if c.autoincrement]
            if len(counters) == 1:
                assert (info.primary_key, info.primary_key_source) == ((counters[0],), "inferred")
            else:
                assert (info.primary_key, info.primary_key_source) == ((), "none"), name


# ----------------------------------------------------------------------------------------------
# no lock file, nothing written, read-only open, errors
# ----------------------------------------------------------------------------------------------


def _listing(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


@pytest.mark.requires_driver
def test_no_lock_file_while_open_or_iterating(access_db, tmp_path):
    copy = tmp_path / access_db.name
    shutil.copy2(access_db, copy)
    before = _listing(tmp_path)
    stat = copy.stat()
    r = open_reader(copy, reader="pure")
    try:
        assert _listing(tmp_path) == before
        r.list_tables()
        r.get_table("BIG")
        assert r.count_rows("BIG") == MANIFEST["BIG"]["count"]
        assert _listing(tmp_path) == before
        it = r.iter_rows("BIG", batch_size=100)
        next(it)
        assert _listing(tmp_path) == before, "lock file appeared while iterating"
        next(it)
        assert _listing(tmp_path) == before
        for _ in it:
            pass
        assert _listing(tmp_path) == before
        assert not [p for p in tmp_path.iterdir() if p.suffix.lower() in (".ldb", ".laccdb")]
    finally:
        r.close()
    assert _listing(tmp_path) == before
    after = copy.stat()
    assert (after.st_size, after.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)


@pytest.mark.requires_driver
def test_source_opened_read_only_binary(access_db, monkeypatch):
    """Every open of the source file is 'rb'-like: no write/append/create flags."""
    target = os.path.normcase(str(access_db.resolve()))
    seen_modes: list[str] = []
    seen_flags: list[int] = []
    real_open, real_os_open = builtins.open, os.open

    def same(p) -> bool:
        try:
            return os.path.normcase(str(Path(os.fspath(p)).resolve())) == target
        except (TypeError, ValueError, OSError):
            return False

    def spy_open(file, mode="r", *a, **k):
        if same(file):
            seen_modes.append(mode)
        return real_open(file, mode, *a, **k)

    def spy_os_open(path, flags, *a, **k):
        if same(path):
            seen_flags.append(flags)
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    before = access_db.stat()
    with open_reader(access_db, reader="pure") as r:
        r.get_table("COMP")
        _rows(r, "COMP")
    monkeypatch.undo()
    assert seen_modes or seen_flags, "the spy saw no open of the source (test is blind)"
    for mode in seen_modes:
        assert "b" in mode and "r" in mode and not set(mode) & set("wax+"), mode
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    for flags in seen_flags:
        assert not flags & write_flags, flags
    after = access_db.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_garbage_file_is_a_clear_error(tmp_path):
    p = tmp_path / "garbage.mdb"
    p.write_bytes(b"this is not an access database\x00" * 200)
    with pytest.raises(Acade2SqlError), open_reader(p, reader="pure") as r:
        r.list_tables()


def test_empty_file_is_a_clear_error(tmp_path):
    p = tmp_path / "empty.accdb"
    p.write_bytes(b"")
    with pytest.raises(Acade2SqlError), open_reader(p, reader="pure") as r:
        r.list_tables()


@pytest.mark.requires_driver
def test_streaming_releases_parsed_table(pure_reader):
    """Only one parsed table is held at a time: after the last batch nothing big is retained."""
    import gc
    import tracemalloc

    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        for _ in pure_reader.iter_rows("BIG", batch_size=1000):
            pass
        pure_reader.count_rows("BIG")
        gc.collect()
        kept = tracemalloc.get_traced_memory()[0] - base
    finally:
        tracemalloc.stop()
    # BIG is 12,345 rows x 4 columns: a retained parse would be several MB
    assert kept < 1_000_000, f"{kept} bytes still referenced after iteration"
