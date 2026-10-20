"""Container start-up helper for the acade2sql test runner (called by docker/entrypoint.sh).

1. Builds ``ACADE2SQL_TEST_MSSQL_URL`` / ``ACADE2SQL_TEST_PG_URL`` from the raw passwords in
   ``MSSQL_SA_PASSWORD`` / ``POSTGRES_PASSWORD`` (percent-encoded, so any special character is
   safe). A URL that is already set in the environment is kept as is.
2. Waits (up to ``ACADE2SQL_WAIT_SECONDS``, default 120) until each server accepts connections.
3. Creates database ``acade2sql_test`` on SQL Server if missing, and schema ``acade`` on both.
4. Prints ``export NAME='value'`` lines on stdout for the entrypoint to ``eval``.

Everything human-readable goes to stderr, and passwords are never printed there.
``--urls-only`` skips steps 2-3 (used to check the URL building without servers).
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from collections.abc import Callable
from urllib.parse import quote

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

# Fixed names (the SQL below spells them out literally; no identifiers are interpolated).
TEST_DB = "acade2sql_test"
TEST_SCHEMA = "acade"
MSSQL_DRIVER = "ODBC Driver 18 for SQL Server"
MSSQL_VAR = "ACADE2SQL_TEST_MSSQL_URL"
PG_VAR = "ACADE2SQL_TEST_PG_URL"


def say(msg: str) -> None:
    print(f"[db_setup] {msg}", file=sys.stderr, flush=True)


def encode_password(raw: str) -> str:
    """Percent-encode for the userinfo part of a URL.

    ``quote(safe="")`` rather than ``quote_plus``: SQLAlchemy decodes the password with
    ``unquote``, which turns ``%20`` back into a space but leaves a ``+`` from ``quote_plus``
    as a literal plus (verified with SQLAlchemy 2.x).
    """
    return quote(raw, safe="")


def mssql_url(password: str, host: str, port: int, database: str = TEST_DB) -> str:
    query = f"driver={MSSQL_DRIVER.replace(' ', '+')}&TrustServerCertificate=yes"
    return f"mssql+pyodbc://sa:{encode_password(password)}@{host}:{port}/{database}?{query}"


def pg_url(password: str, host: str, port: int, database: str = TEST_DB) -> str:
    return f"postgresql+psycopg://postgres:{encode_password(password)}@{host}:{port}/{database}"


def check_roundtrip(url: str, raw: str) -> None:
    if make_url(url).password != raw:
        raise SystemExit("db_setup: password does not survive URL encoding (please report)")


def masked(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def wait_for(name: str, connect: Callable[[], None], deadline: float) -> None:
    attempt = 0
    while True:
        attempt += 1
        try:
            connect()
        except Exception as exc:  # the server is still starting: any driver error
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"db_setup: {name} not reachable after the wait timeout "
                    f"({type(exc).__name__}: {str(exc).splitlines()[0][:300]})"
                ) from None
            if attempt == 1 or attempt % 5 == 0:
                say(f"waiting for {name} ({type(exc).__name__})")
            time.sleep(2)
        else:
            say(f"{name} is up")
            return


def _engine(url: str | URL, **kwargs: object) -> Engine:
    return create_engine(url, pool_pre_ping=False, **kwargs)


def setup_mssql(url: str, deadline: float) -> None:
    master = make_url(url).set(database="master")
    admin = _engine(master, isolation_level="AUTOCOMMIT", connect_args={"timeout": 5})

    def ping() -> None:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        wait_for("mssql", ping, deadline)
        with admin.connect() as conn:
            # CREATE DATABASE cannot run inside a transaction: AUTOCOMMIT engine.
            conn.execute(
                text("IF DB_ID(N'acade2sql_test') IS NULL CREATE DATABASE [acade2sql_test]")
            )
    finally:
        admin.dispose()
    db = _engine(url, isolation_level="AUTOCOMMIT", connect_args={"timeout": 5})
    try:
        with db.connect() as conn:
            # CREATE SCHEMA must be the only statement of its batch: wrap it in EXEC.
            conn.execute(text("IF SCHEMA_ID(N'acade') IS NULL EXEC(N'CREATE SCHEMA [acade]')"))
    finally:
        db.dispose()
    say(f"mssql: database {TEST_DB} and schema {TEST_SCHEMA} ready")


def setup_pg(url: str, deadline: float) -> None:
    # The database itself is created by the postgres image (POSTGRES_DB).
    engine = _engine(url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})

    def ping() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        wait_for("postgres", ping, deadline)
        with engine.connect() as conn:
            conn.execute(text('CREATE SCHEMA IF NOT EXISTS "acade"'))
    finally:
        engine.dispose()
    say(f"postgres: schema {TEST_SCHEMA} ready")


def _port(env: dict[str, str], var: str, default: int) -> int:
    # Not MSSQL_PORT / PG_PORT: those are the *host* ports in compose.yaml.
    return int(env.get(var) or default)


def build_urls(env: dict[str, str]) -> dict[str, str]:
    """``{VAR: url}`` for every server whose URL or password is configured."""
    urls: dict[str, str] = {}
    if env.get(MSSQL_VAR, "").strip():
        urls[MSSQL_VAR] = env[MSSQL_VAR].strip()
    elif env.get("MSSQL_SA_PASSWORD"):
        raw = env["MSSQL_SA_PASSWORD"]
        url = mssql_url(
            raw, env.get("ACADE2SQL_MSSQL_HOST", "mssql"), _port(env, "ACADE2SQL_MSSQL_PORT", 1433)
        )
        check_roundtrip(url, raw)
        urls[MSSQL_VAR] = url
    if env.get(PG_VAR, "").strip():
        urls[PG_VAR] = env[PG_VAR].strip()
    elif env.get("POSTGRES_PASSWORD"):
        raw = env["POSTGRES_PASSWORD"]
        url = pg_url(
            raw, env.get("ACADE2SQL_PG_HOST", "postgres"), _port(env, "ACADE2SQL_PG_PORT", 5432)
        )
        check_roundtrip(url, raw)
        urls[PG_VAR] = url
    return urls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--urls-only", action="store_true", help="build the URLs, no connect")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    urls = build_urls(env)
    if not urls:
        say("no MSSQL_SA_PASSWORD / POSTGRES_PASSWORD set: live tests will be skipped")
    if not args.urls_only:
        deadline = time.monotonic() + float(env.get("ACADE2SQL_WAIT_SECONDS", "120"))
        if MSSQL_VAR in urls:
            setup_mssql(urls[MSSQL_VAR], deadline)
        if PG_VAR in urls:
            setup_pg(urls[PG_VAR], deadline)
    for var, url in urls.items():
        say(f"{var}={masked(url)}")
        print(f"export {var}={shlex.quote(url)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
