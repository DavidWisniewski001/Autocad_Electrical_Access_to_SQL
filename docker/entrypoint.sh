#!/bin/sh
# Entrypoint of the acade2sql test-runner image (docs/M6_SPEC.md).
#
# 1. docker/db_setup.py waits (ACADE2SQL_WAIT_SECONDS, default 120 s) for SQL Server and
#    PostgreSQL, creates database acade2sql_test on SQL Server and schema "acade" on both, and
#    prints ACADE2SQL_TEST_MSSQL_URL / ACADE2SQL_TEST_PG_URL built from the raw passwords
#    (MSSQL_SA_PASSWORD / POSTGRES_PASSWORD, percent-encoded) as `export` lines.
# 2. exec the requested command (default: see CMD in docker/Dockerfile), e.g. `bash`.
#
# ACADE2SQL_SKIP_DB_SETUP=1 skips step 1 (no servers needed, live tests then skip).
set -eu

if [ "${ACADE2SQL_SKIP_DB_SETUP:-0}" != "1" ]; then
    # stdout: export lines only (eval'd below, never echoed); progress goes to stderr.
    acade2sql_env=$(python /usr/local/lib/acade2sql-docker/db_setup.py)
    eval "$acade2sql_env"
    unset acade2sql_env
fi

if [ "$#" -eq 0 ]; then
    set -- bash
fi
exec "$@"
