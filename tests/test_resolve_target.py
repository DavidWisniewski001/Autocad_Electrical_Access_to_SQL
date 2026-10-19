"""resolve_target precedence (spec M3 section 4). No Access driver needed."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from acade2sql.converter import resolve_target


def _db_of(url: str) -> Path:
    u = make_url(url)
    assert u.drivername.startswith("sqlite"), url
    return Path(u.database).resolve()


def test_explicit_url_as_is(monkeypatch):
    monkeypatch.setenv("ACADE2SQL_TARGET", "sqlite:///env.sqlite")
    url = "postgresql+psycopg2://u:p@h:5432/db"
    assert resolve_target(url, Path("x.mdb")) == url
    assert resolve_target("sqlite:///some/where.db", Path("x.mdb")) == "sqlite:///some/where.db"


def test_env_used_when_no_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("ACADE2SQL_TARGET", "mssql+pyodbc://u:p@dsn")
    assert resolve_target(None, Path("x.mdb")) == "mssql+pyodbc://u:p@dsn"


def test_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("ACADE2SQL_TARGET", "sqlite:///env.sqlite")
    assert resolve_target("sqlite:///mine.sqlite", Path("x.mdb")) == "sqlite:///mine.sqlite"


def test_default_in_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("ACADE2SQL_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    url = resolve_target(None, Path("somewhere/else/default_cat.mdb"))
    assert url.startswith("sqlite:///")
    assert _db_of(url) == (tmp_path / "default_cat.sqlite").resolve()


def test_default_for_accdb_stem(monkeypatch, tmp_path):
    monkeypatch.delenv("ACADE2SQL_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    url = resolve_target(None, Path("footprint_lookup.accdb"))
    assert _db_of(url) == (tmp_path / "footprint_lookup.sqlite").resolve()


@pytest.mark.parametrize("ext", [".sqlite", ".db", ".sqlite3"])
def test_plain_path_becomes_url(monkeypatch, tmp_path, ext):
    monkeypatch.delenv("ACADE2SQL_TARGET", raising=False)
    p = tmp_path / f"out{ext}"
    url = resolve_target(str(p), Path("x.mdb"))
    assert url.startswith("sqlite:///")
    assert _db_of(url) == p.resolve()


def test_relative_plain_path_made_absolute(monkeypatch, tmp_path):
    monkeypatch.delenv("ACADE2SQL_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    url = resolve_target("rel.sqlite", Path("x.mdb"))
    assert _db_of(url) == (tmp_path / "rel.sqlite").resolve()
    assert Path(make_url(url).database).is_absolute()


def test_env_plain_path_becomes_url(monkeypatch, tmp_path):
    monkeypatch.setenv("ACADE2SQL_TARGET", str(tmp_path / "envout.db"))
    url = resolve_target(None, Path("x.mdb"))
    assert _db_of(url) == (tmp_path / "envout.db").resolve()
