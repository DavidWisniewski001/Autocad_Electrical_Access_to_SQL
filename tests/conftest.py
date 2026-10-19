"""Shared fixtures and markers for the acade2sql test-suite."""

from __future__ import annotations

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


@pytest.fixture
def reader(access_db):
    from acade2sql.readers import open_reader

    with open_reader(access_db) as r:
        yield r
