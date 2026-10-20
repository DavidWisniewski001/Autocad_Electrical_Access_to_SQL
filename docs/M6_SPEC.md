# M6 spec: Docker test environment for live SQL Server and PostgreSQL

Goal: `docker compose` brings up SQL Server and PostgreSQL plus a Linux test-runner image, and runs the
live tests and an end-to-end conversion of the real sample databases into both servers. Nothing is
installed on the Windows host except Docker Desktop. The pure reader (M5) makes this possible, because
the Linux container has no Access ODBC driver.

**Environment fact:** Docker is NOT installed on the development machine yet (no Docker Desktop, no
WSL). Everything must be written so that it is checked as far as possible without Docker (YAML/TOML
parse, hadolint-style review, `pytest` collection on Windows), and the README must say exactly how to
run it once Docker Desktop is installed. The lead runs it when Docker is available.

## 1. Files (owner: python-converter-dev)

```
docker/
  Dockerfile            # test-runner image
  entrypoint.sh         # waits for the servers, creates test DB/schema, runs the requested command
compose.yaml            # repo root
.env.example            # placeholders only
scripts/docker_env.py   # writes .env with strong random passwords (refuses to overwrite unless --force)
scripts/e2e_live.py     # end-to-end real-data check (below)
.dockerignore
```

* **Runner image**: `python:3.12-slim-bookworm` (pin the digest-free tag), install `unixodbc` and
  Microsoft **ODBC Driver 18 for SQL Server** from packages.microsoft.com (Debian 12 repo; signed key;
  `ACCEPT_EULA=Y` set only for that apt step), then `pip install .[dev,postgresql]` from the build
  context. Non-root user. `PYTHONDONTWRITEBYTECODE=1`. No secrets in the image.
* **compose.yaml** services:
  - `mssql`: `mcr.microsoft.com/mssql/server:2022-latest`, `ACCEPT_EULA=Y`, `MSSQL_PID=Developer`,
    `MSSQL_SA_PASSWORD=${MSSQL_SA_PASSWORD:?run scripts/docker_env.py}`, a healthcheck with
    `/opt/mssql-tools18/bin/sqlcmd -C ... -Q "SELECT 1"`, and a named volume. Port 1433 is published only on
    127.0.0.1 (`127.0.0.1:${MSSQL_PORT:-14330}:1433`) so the host can also be used.
  - `postgres`: `postgres:16-bookworm`, `POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?...}`, db `acade2sql_test`,
    a healthcheck (`pg_isready`), `127.0.0.1:${PG_PORT:-54330}:5432`, and a named volume.
  - `tests`: builds `docker/Dockerfile`, `depends_on` both with `condition: service_healthy`. Env:
    `ACADE2SQL_TEST_MSSQL_URL=mssql+pyodbc://sa:${MSSQL_SA_PASSWORD}@mssql:1433/acade2sql_test?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes`,
    `ACADE2SQL_TEST_PG_URL=postgresql+psycopg://postgres:${POSTGRES_PASSWORD}@postgres:5432/acade2sql_test`,
    `ACADE2SQL_TEST_FIXTURE_DIR=/fixtures`, `ACADE2SQL_DATA_DIR=/data`. Volumes: `./data_files:/data:ro`
    and `./build/fixtures:/fixtures:ro`. Default command: `pytest -m "live_db" -q` then
    `python scripts/e2e_live.py`. The entrypoint also allows `bash` / an arbitrary command.
  - Passwords contain special characters, so URL-encode them in the URLs. The entrypoint builds the URLs
    from the raw env vars with `urllib.parse.quote_plus`. compose passes only the raw passwords.
* `entrypoint.sh`: waits up to 120 s for both servers, creates database `acade2sql_test` on mssql if it
  is missing (sqlcmd from the mssql-tools18 package, or a tiny Python pyodbc snippet), and creates schema
  `acade` on both, so `--schema` can be tested.
* `.gitignore`: `.env`, `build/`. `.dockerignore`: `.git`, `data_files/`, `build/`, `.env`, `*.sqlite`,
  `__pycache__`, `.claude/`.
* `scripts/docker_env.py`: generates 24-char passwords meeting SQL Server complexity rules, writes
  `.env` with MSSQL_SA_PASSWORD and POSTGRES_PASSWORD, and prints nothing secret.

## 2. Linux compatibility (owner: python-converter-dev for src/, test-engineer for tests/)

* The package and the test suite must import and collect on Linux without the Access driver:
  `ctypes.windll` and the registry (`winreg`) must only be touched inside functions guarded by
  `sys.platform == "win32"`. `doctor` must work on Linux (no Access driver → pure only).
* `requires_driver` tests skip cleanly on Linux.

## 3. Prebuilt fixtures (owner: test-engineer)

* `scripts/build_test_fixtures.py OUT_DIR` (runs on Windows, where the Access driver exists) writes
  `fixture.mdb`, `fixture.accdb` and `MANIFEST.json` (the expected table names, row counts and PKs)
  into OUT_DIR (default `build/fixtures`). These are synthetic, so it is fine to keep them out of git
  and rebuild them.
* `tests/test_live_db.py` is reworked so that on any OS, with `ACADE2SQL_TEST_*_URL` set, it uses
  (a) the session-built fixture when the Access driver is available, else (b) the prebuilt files from
  `ACADE2SQL_TEST_FIXTURE_DIR`, read with `--reader pure`; skip with a clear reason when neither is
  available. It covers, for each server: full + checksum, sync no-op, sync after target tampering
  (exact counters), `--delete-missing`, append duplicate-key rollback, `--schema acade`, the collation
  guard on mssql (a case-only key change → error, target unchanged), the PG NUL error, and REAL float4
  round-trips without spurious updates (the M4 review's PLAUSIBLE item). Each test uses unique table
  names or a throwaway schema and cleans up.

## 4. `scripts/e2e_live.py` (owner: python-converter-dev)

For each URL in the env (mssql, pg) and each source in `ACADE2SQL_DATA_DIR` (`*.mdb`, `*.accdb`):
full `--reader pure --checksum` into schema `e2e`, then tamper (3 updates, 2 deletes, 1 extra row,
through SQLAlchemy), sync `--checksum` (expect exactly those counters), `--delete-missing` (deleted=1),
and a final sync (all zero). Print a summary table and exit non-zero on any deviation. Never write to
`/data`. Clean up the schema at the end (keep it with `--keep`).

## 5. Docs (owner: lead / docs-writer after the run)

README "Testing against SQL Server and PostgreSQL with Docker": install Docker Desktop (WSL 2 backend),
`python scripts/build_test_fixtures.py`, `python scripts/docker_env.py`,
`docker compose run --rm tests`, how to connect from the host (ports 14330/54330 on 127.0.0.1), and
`docker compose down -v` to clean up. Note the EULA (`ACCEPT_EULA=Y`, Developer edition: free for
testing, not production).

## 6. Acceptance

Without Docker (now): YAML parses; `scripts/docker_env.py` and `scripts/build_test_fixtures.py` run on
Windows; full pytest still green on Windows (live tests skip without URLs); the package imports with
`sys.platform` faked as linux, and `ctypes.windll` is absent.
With Docker (when installed): `docker compose run --rm tests` → live tests pass on both servers, and
e2e_live passes for both sample DBs on both servers.
