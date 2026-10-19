"""Tests against the real (read-only) sources in data_files/. Never writes to them."""

from __future__ import annotations

import pytest
from tests.conftest import DEFAULT_CAT, FOOTPRINT

from acade2sql.readers import open_reader

pytestmark = [pytest.mark.real_data, pytest.mark.requires_driver]

EXPECT = {
    DEFAULT_CAT: (168, 1_594_493),
    FOOTPRINT: (117, 261_858),
}


def _sig(p):
    s = p.stat()
    return s.st_size, s.st_mtime_ns


@pytest.fixture(scope="module", autouse=True)
def sources_unchanged():
    before = {p: _sig(p) for p in EXPECT}
    yield
    assert {p: _sig(p) for p in EXPECT} == before, "a source file changed during the tests!"
    for p in EXPECT:
        assert not [q for q in p.parent.iterdir() if q.suffix.lower() in (".ldb", ".laccdb")]


@pytest.fixture(scope="module", params=list(EXPECT), ids=lambda p: p.name)
def real_reader(request):
    with open_reader(request.param) as r:
        yield request.param, r


@pytest.mark.slow
def test_table_and_row_counts(real_reader):
    path, r = real_reader
    n_tables, n_rows = EXPECT[path]
    names = r.list_tables()
    assert len(names) == n_tables
    assert names == sorted(names, key=str.casefold)
    assert not [n for n in names if n.upper().startswith("MSYS")]
    assert sum(r.count_rows(n) for n in names) == n_rows


@pytest.mark.slow
def test_every_table_pk_is_recnum_inferred(real_reader):
    _path, r = real_reader
    bad = []
    for n in r.list_tables():
        info = r.get_table(n)
        if (info.primary_key, info.primary_key_source) != (("RECNUM",), "inferred"):
            bad.append((n, info.primary_key, info.primary_key_source))
    assert bad == []


def test_only_known_access_types(real_reader):
    _, r = real_reader
    seen = set()
    for n in r.list_tables():
        seen.update(c.access_type for c in r.get_table(n).columns)
    assert seen <= {"VARCHAR", "LONGCHAR", "COUNTER"}


@pytest.mark.slow
def test_ms_iter_rows_total():
    with open_reader(DEFAULT_CAT) as r:
        assert r.count_rows("MS") == 269_772
        total = sum(len(b) for b in r.iter_rows("MS"))
        assert total == 269_772


def test_ms_first_batch_shape():
    with open_reader(DEFAULT_CAT) as r:
        info = r.get_table("MS")
        batch = next(iter(r.iter_rows("MS", batch_size=100)))
        assert len(batch) == 100
        assert all(type(t) is tuple and len(t) == len(info.columns) for t in batch)
        i = info.column_names.index("RECNUM")
        assert all(isinstance(t[i], int) for t in batch)


def test_footprint_small_table_roundtrip_counts():
    with open_reader(FOOTPRINT) as r:
        for n in r.list_tables()[:5]:
            assert sum(len(b) for b in r.iter_rows(n, batch_size=333)) == r.count_rows(n)
