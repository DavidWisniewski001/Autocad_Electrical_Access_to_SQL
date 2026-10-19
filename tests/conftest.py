"""Shared fixtures and markers for the acade2sql test-suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `from tests.fixtures import ...`
    sys.path.insert(0, str(ROOT))

from tests.fixtures import build_access  # noqa: E402

DATA_DIR = ROOT / "data_files"
DEFAULT_CAT = DATA_DIR / "default_cat.mdb"
FOOTPRINT = DATA_DIR / "footprint_lookup.accdb"


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_driver: needs the Microsoft Access ODBC driver")
    config.addinivalue_line("markers", "access_driver: alias of requires_driver")
    config.addinivalue_line("markers", "real_data: runs against data_files/*.mdb|accdb")
    config.addinivalue_line("markers", "slow: long-running test")
    config.addinivalue_line(
        "markers",
        "live_db: needs a live server (ACADE2SQL_TEST_PG_URL / ACADE2SQL_TEST_MSSQL_URL)",
    )


def pytest_collection_modifyitems(config, items):
    have_driver = build_access.driver_available()
    for item in items:
        if (
            "requires_driver" in item.keywords or "access_driver" in item.keywords
        ) and not have_driver:
            item.add_marker(pytest.mark.skip(reason="Access ODBC driver not available"))
        if "real_data" in item.keywords:
            if not have_driver:
                item.add_marker(pytest.mark.skip(reason="Access ODBC driver not available"))
            elif not (DEFAULT_CAT.exists() and FOOTPRINT.exists()):
                item.add_marker(pytest.mark.skip(reason="data_files/ sources missing"))


@pytest.fixture(scope="session")
def manifest():
    return build_access.MANIFEST


@pytest.fixture(scope="session", params=["mdb", "accdb"])
def access_db(request, tmp_path_factory) -> Path:
    """Session-scoped synthetic database (built once per format). NEVER modified by tests."""
    if not build_access.driver_available():
        pytest.skip("Access ODBC driver not available")
    d = tmp_path_factory.mktemp(f"fx_{request.param}")
    return build_access.build(d / f"fixture.{request.param}")


@pytest.fixture(scope="session")
def mdb_db(tmp_path_factory) -> Path:
    if not build_access.driver_available():
        pytest.skip("Access ODBC driver not available")
    return build_access.build(tmp_path_factory.mktemp("fx_cli") / "cli.mdb")


READER_KINDS = ("odbc", "pure")


@pytest.fixture(params=READER_KINDS)
def reader_kind(request) -> str:
    """The reader backend under test ('odbc' | 'pure')."""
    return request.param


@pytest.fixture
def reader(access_db, reader_kind):
    """A reader over the synthetic database, for both backends x both formats."""
    from acade2sql.readers import open_reader

    with open_reader(access_db, reader=reader_kind) as r:
        yield r


@pytest.fixture
def odbc_reader(access_db):
    from acade2sql.readers import open_reader

    with open_reader(access_db, reader="odbc") as r:
        yield r


@pytest.fixture
def pure_reader(access_db):
    from acade2sql.readers import open_reader

    with open_reader(access_db, reader="pure") as r:
        yield r


@pytest.fixture
def no_odbc_driver(monkeypatch):
    """Simulate a machine without the Access ODBC driver (``diagnose_driver`` reports not-ok).

    Patched wherever the name may have been imported, so it works however the factory looks it up.
    """
    from acade2sql import diagnostics

    def fake(drivers=None):
        return diagnostics.DriverStatus(
            False, diagnostics.python_bits(), [], "simulated: no Access ODBC driver"
        )

    monkeypatch.setattr(diagnostics, "diagnose_driver", fake)
    for modname in ("acade2sql.readers", "acade2sql.cli", "acade2sql.converter"):
        mod = __import__(modname, fromlist=["x"])
        if hasattr(mod, "diagnose_driver"):
            monkeypatch.setattr(mod, "diagnose_driver", fake)
    return fake


# ---- M4 fixtures ---------------------------------------------------------------------------------


def _env_url(var: str) -> str:
    url = os.environ.get(var, "").strip()
    if not url:
        pytest.skip(f"{var} not set")
    return url


@pytest.fixture
def pg_url() -> str:
    return _env_url("ACADE2SQL_TEST_PG_URL")


@pytest.fixture
def mssql_url() -> str:
    return _env_url("ACADE2SQL_TEST_MSSQL_URL")


@pytest.fixture
def install_reader(monkeypatch):
    """Make ``converter.open_reader`` return the given in-memory reader."""
    from acade2sql import converter

    def install(reader):
        monkeypatch.setattr(converter, "open_reader", lambda *a, **k: reader)
        return reader

    return install


def patch_writer(mp, generic: bool, dialect: str | None = None):
    """Wrap ``converter.SqlAlchemyWriter`` so every writer gets ``_force_generic_sync``.

    With ``dialect`` the writer (and its engine's dialect instance) is made to *report* that
    dialect name while still talking to SQLite; used to exercise dialect-name-gated checks.
    """
    from acade2sql import converter

    real = converter.SqlAlchemyWriter

    def factory(*a, **k):
        w = real(*a, **k)
        w._force_generic_sync = generic
        if dialect:
            w.dialect = dialect
            w.engine.dialect.name = dialect
        return w

    mp.setattr(converter, "SqlAlchemyWriter", factory)


@pytest.fixture(params=["native", "generic"])
def impl(request, monkeypatch):
    """Sync implementation under test: 'generic' sets writer._force_generic_sync=True."""
    patch_writer(monkeypatch, request.param == "generic")
    return request.param


@pytest.fixture(scope="session")
def accdb_original(tmp_path_factory) -> Path:
    """Session .accdb fixture for the M4 sync tests. NEVER modified (tests copy it)."""
    if not build_access.driver_available():
        pytest.skip("Access ODBC driver not available")
    d = tmp_path_factory.mktemp("fx_m4")
    return build_access.build(d / "orig.accdb")
