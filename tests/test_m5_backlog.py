"""M5 backlog from 0.2.0 (docs/M5_SPEC.md section 4): redact() short secrets, keyless dry-run
actions, top-level error exports. Driver-free."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from tests.fakes import FakeReader, std_info, std_rows

import acade2sql
from acade2sql import converter, mask_url, redact
from acade2sql.errors import Acade2SqlError

# ------------------------------------------------------------------------------ redact


def pg(pw: str) -> str:
    return f"postgresql+psycopg://u:{quote(pw, safe='')}@h:5432/db"


@pytest.mark.parametrize("pw", ["p", "ab", "abc"])
def test_short_secret_masked_in_url_position(pw):
    url = pg(pw)
    text = f"could not connect to {url}: connection refused"
    out = redact(text, url)
    assert f"u:{pw}@" not in out
    assert "u:***@h" in out
    assert "connection refused" in out


@pytest.mark.parametrize("pw", ["p", "ab", "abc"])
def test_short_secret_masked_in_pwd_position(pw):
    text = f"Driver={{ODBC Driver 18}};Server=h;UID=u;PWD={pw};Database=db"
    out = redact(text, pg(pw))
    assert f"PWD={pw}" not in out
    assert "PWD=***" in out
    assert "Server=h" in out and "UID=u" in out and "Database=db" in out


@pytest.mark.parametrize("pw", ["p", "ab", "abc"])
def test_short_secret_masked_in_odbc_connect_query(pw):
    url = f"mssql+pyodbc:///?odbc_connect=Driver%3D%7BSQL+Server%7D%3BUID%3Du%3BPWD%3D{pw}%3BDatabase%3Ddb"
    out = redact(f"failed: {url}", url)
    assert f"PWD%3D{pw}%3B" not in out
    assert "***" in out
    assert "Database%3Ddb" in out


def test_one_char_secret_does_not_mangle_unrelated_words():
    url = pg("p")
    text = "plain text: the pool of postgresql sessions is empty; password authentication failed"
    out = redact(text, url)
    assert out == text  # nothing structured in it, secret too short for free text
    for word in ("plain", "pool", "postgresql", "password", "empty", "sessions"):
        assert word in out


def test_three_char_secret_does_not_mangle_unrelated_words():
    url = pg("abc")
    text = "table 'abcdef' has a cabbage column; abc is not masked in free text"
    assert redact(text, url) == text


def test_short_secret_still_masked_when_text_also_has_words():
    url = pg("p")
    text = f"plain error while connecting to {url} (pool exhausted)"
    out = redact(text, url)
    assert "plain" in out and "pool exhausted" in out
    assert "u:p@" not in out and "u:***@h" in out


def test_mask_url_short_password():
    assert "u:p@" not in mask_url(pg("p"))
    assert mask_url(pg("p")) == "postgresql+psycopg://u:***@h:5432/db"


def test_short_secret_query_value_masked():
    url = "postgresql+psycopg://u@h/db?password=ab&sslmode=require"
    out = redact(f"x {url}", url)
    assert "password=ab" not in out


@pytest.mark.parametrize("pw", ["wxyz", "s3cr3tP4", "correct horse battery"])
def test_long_secret_masked_in_free_text(pw):
    url = pg(pw)
    text = f"server rejected value {pw!r} for login; also {pw} and {quote(pw, safe='')}"
    out = redact(text, url)
    assert pw not in out and quote(pw, safe="") not in out
    assert "server rejected value" in out and "for login" in out


def test_four_char_boundary_is_masked_three_is_not():
    assert "wxyz" not in redact("the value wxyz leaked", pg("wxyz"))
    assert "xyz" in redact("the value xyz stays", pg("xyz"))


def test_eight_char_secret_masked_everywhere():
    pw = "Tr0ub4dr"
    url = pg(pw)
    out = redact(f"{url} | PWD={pw} | bare {pw} | enc {quote(pw)}", url)
    assert pw not in out


def test_redact_without_url_masks_structured_positions_only():
    out = redact("postgresql://u:hunter2@h/db PWD=hunter2; plain hunter2 stays")
    assert "u:***@h" in out and "PWD=***" in out
    assert "plain hunter2 stays" in out


# ------------------------------------------------------------- keyless dry-run actions


def keyless_reader():
    return FakeReader(
        {
            "KEYED": (std_info("KEYED"), std_rows(range(1, 3))),
            "NOKEY": (std_info("NOKEY", pk=()), std_rows(range(1, 3))),
        }
    )


def test_dry_run_no_key_error_has_no_replace_rows(tmp_path, install_reader):
    install_reader(keyless_reader())
    rep = converter.convert(
        "fake.mdb", str(tmp_path / "n.sqlite"), mode="sync", no_key="error", dry_run=True
    )
    by = {t.name: t for t in rep.tables}
    assert by["NOKEY"].status == "error"
    assert "replace-rows" not in by["NOKEY"].actions
    assert "upsert" not in by["NOKEY"].actions
    assert by["KEYED"].actions == ["create-if-missing", "upsert"]
    line = next(ln for ln in rep.render_text().splitlines() if "NOKEY" in ln)
    assert "replace-rows" not in line
    assert "replace-rows" not in str(rep.to_dict())


def test_dry_run_no_key_error_via_cli(capsys, tmp_path, install_reader):
    from acade2sql import cli

    install_reader(keyless_reader())
    rc = cli.main(
        ["convert", "fake.mdb", "--target", str(tmp_path / "n.sqlite"), "--mode", "sync",
         "--dry-run"]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 3
    assert "replace-rows" not in out


def test_dry_run_no_key_replace_still_lists_replace_rows(tmp_path, install_reader):
    install_reader(keyless_reader())
    rep = converter.convert(
        "fake.mdb", str(tmp_path / "n.sqlite"), mode="sync", no_key="replace", dry_run=True
    )
    by = {t.name: t for t in rep.tables}
    assert by["NOKEY"].status == "planned"
    assert by["NOKEY"].actions == ["create-if-missing", "replace-rows"]


def test_dry_run_explicit_key_for_keyless_table_upserts(tmp_path, install_reader):
    install_reader(keyless_reader())
    rep = converter.convert(
        "fake.mdb", str(tmp_path / "n.sqlite"), mode="sync", key=["NOKEY=id"], dry_run=True
    )
    by = {t.name: t for t in rep.tables}
    assert by["NOKEY"].actions == ["create-if-missing", "upsert"]
    assert by["NOKEY"].status == "planned"


# ------------------------------------------------------------------------------ exports


@pytest.mark.parametrize("name", ["UsageError", "SchemaError", "SyncKeyError", "TargetDataError"])
def test_error_classes_exported(name):
    cls = getattr(acade2sql, name)
    assert name in acade2sql.__all__
    from acade2sql import errors

    assert cls is getattr(errors, name)
    assert issubclass(cls, Exception)


def test_export_hierarchy():
    assert issubclass(acade2sql.UsageError, ValueError)
    for name in ("SchemaError", "SyncKeyError", "TargetDataError"):
        assert issubclass(getattr(acade2sql, name), Acade2SqlError)


def test_star_import_has_all_names():
    ns: dict = {}
    exec("from acade2sql import *", ns)
    for name in acade2sql.__all__:
        assert name in ns


def test_version_is_0_3_0():
    assert acade2sql.__version__ == "0.3.0"
