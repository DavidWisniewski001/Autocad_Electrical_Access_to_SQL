"""Live PostgreSQL / SQL Server round trips. Skipped unless the env URLs are set.

ACADE2SQL_TEST_PG_URL / ACADE2SQL_TEST_MSSQL_URL must point at a throwaway database where the
user may CREATE SCHEMA. Each test uses a unique schema (``a2s_<random>``), dropped afterwards.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import MetaData, create_engine, text
from tests.fakes import FakeReader, by_name, std_info, std_rows
from tests.fixtures.build_access import MANIFEST

from acade2sql import converter
from acade2sql.writers import SqlAlchemyWriter

pytestmark = pytest.mark.live_db


@pytest.fixture
def live_schema(request):
    """Yield (url, schema) for the requested live DB; drop the schema afterwards."""
    which = request.param
    url = request.getfixturevalue("pg_url" if which == "pg" else "mssql_url")
    schema = f"a2s_{uuid.uuid4().hex[:10]}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(f'CREATE SCHEMA "{schema}"' if which == "pg" else f"CREATE SCHEMA {schema}")
        )
    try:
        yield url, schema
    finally:
        try:
            if which == "pg":
                with eng.begin() as conn:
                    conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            else:
                md = MetaData(schema=schema)
                md.reflect(bind=eng)
                md.drop_all(bind=eng)
                with eng.begin() as conn:
                    conn.execute(text(f"DROP SCHEMA {schema}"))
        finally:
            eng.dispose()


LIVE = pytest.mark.parametrize("live_schema", ["pg", "mssql"], indirect=True)


def _rows(url, schema, name):
    with SqlAlchemyWriter(url, schema=schema) as w:
        return [r for b in w.read_rows(name) for r in b]


@LIVE
def test_live_fake_source_full_sync_append(live_schema, install_reader):
    url, schema = live_schema
    install_reader(FakeReader({"T": (std_info("T"), std_rows(range(1, 6)))}))
    rep = converter.convert("fake.mdb", url, schema=schema, checksum=True)
    assert rep.ok, rep.render_text()
    assert len(_rows(url, schema, "T")) == 5
    changed = std_rows(range(1, 6))
    changed[1] = (2, "label2 µ", "c2", "")
    changed.append((6, "label6", "c6", None))
    install_reader(FakeReader({"T": (std_info("T"), changed)}))
    rep = converter.convert(
        "fake.mdb", url, schema=schema, mode="sync", checksum=True, delete_missing=True
    )
    t = by_name(rep)["T"]
    assert rep.ok, rep.render_text()
    assert (t.inserted, t.updated, t.deleted, t.unchanged) == (1, 1, 0, 4)
    rep = converter.convert("fake.mdb", url, schema=schema, mode="append")
    assert by_name(rep)["T"].status == "error"  # duplicate keys
    assert len(_rows(url, schema, "T")) == 6


@pytest.mark.requires_driver
@LIVE
def test_live_synthetic_fixture_full_then_sync(live_schema, accdb_original):
    url, schema = live_schema
    rep = converter.convert(accdb_original, url, schema=schema, checksum=True)
    assert rep.ok, rep.render_text()
    for t in rep.tables:
        assert t.source_rows == t.target_rows == MANIFEST[t.name]["count"], t.name
        assert t.checksum_match is True, t.name
    rep = converter.convert(
        accdb_original,
        url,
        schema=schema,
        mode="sync",
        checksum=True,
        delete_missing=True,
        exclude=["PNLCOMP"],
    )
    assert rep.ok, rep.render_text()
    for t in rep.tables:
        assert (t.inserted, t.updated, t.deleted) == (0, 0, 0), t.name
        assert t.unchanged == t.source_rows, t.name
