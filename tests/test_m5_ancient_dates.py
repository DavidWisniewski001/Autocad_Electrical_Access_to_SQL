"""DATETIME values before the Gregorian reform: pure vs ODBC (docs/M5_SPEC.md section 1).

Kept apart from the shared fixture: the ODBC driver reads these back with a shifted calendar
(1000-01-01 -> 0999-07-02), so the two readers can disagree; the spec demands equality.
"""

from __future__ import annotations

import pytest
from tests.fixtures import build_access

from acade2sql.readers import open_reader

pytestmark = pytest.mark.requires_driver


@pytest.fixture(params=["mdb", "accdb"])
def ancient_db(request, tmp_path):
    return build_access.build_ancient(tmp_path / f"ancient.{request.param}")


def _dates(path, kind):
    with open_reader(path, reader=kind) as r:
        return [row[1] for batch in r.iter_rows("DATES") for row in batch]


def test_ancient_dates_pure_equals_odbc(ancient_db):
    assert _dates(ancient_db, "pure") == _dates(ancient_db, "odbc")
