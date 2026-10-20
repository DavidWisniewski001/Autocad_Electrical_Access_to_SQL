# acade2sql test runner (docs/M6_SPEC.md): Linux, pure-Python Access reader, SQL Server via
# pyodbc + Microsoft ODBC Driver 18, PostgreSQL via psycopg. Built by compose.yaml (service
# "tests"); the build context is the repository root, filtered by .dockerignore.
# No secrets are baked in: passwords arrive as environment variables at run time.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Microsoft ODBC Driver 18 for SQL Server, following Microsoft's documented Debian 12 steps
# (learn.microsoft.com "Install the Microsoft ODBC driver for SQL Server (Linux)"): the
# packages-microsoft-prod.deb installs the signing key (/usr/share/keyrings/microsoft-prod.gpg)
# and the signed-by apt source; apt then verifies the packages. ACCEPT_EULA=Y is scoped to the
# one apt-get call that installs msodbcsql18. curl is only needed for the download.
SHELL ["/bin/sh", "-eux", "-c"]
RUN apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL -o /tmp/packages-microsoft-prod.deb \
        https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb; \
    dpkg -i /tmp/packages-microsoft-prod.deb; \
    rm /tmp/packages-microsoft-prod.deb; \
    apt-get update; \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    odbcinst -q -d -n "ODBC Driver 18 for SQL Server"

# Non-root runtime user
RUN groupadd --gid 10001 acade && \
    useradd --uid 10001 --gid acade --create-home --shell /bin/bash acade

WORKDIR /app

# Install the package first, then add tests/scripts (editing a test does not reinstall).
COPY --chown=acade:acade pyproject.toml ./
COPY --chown=acade:acade src ./src
RUN pip install ".[dev,postgresql]"; \
    rm -rf build src/*.egg-info
COPY --chown=acade:acade tests ./tests
COPY --chown=acade:acade scripts ./scripts

COPY docker/entrypoint.sh /usr/local/bin/acade2sql-entrypoint
COPY docker/db_setup.py /usr/local/lib/acade2sql-docker/db_setup.py
# Strip CR in case the checkout converted line endings (.gitattributes asks for LF).
RUN sed -i 's/\r$//' /usr/local/bin/acade2sql-entrypoint; \
    chmod 0755 /usr/local/bin/acade2sql-entrypoint; \
    chmod 0644 /usr/local/lib/acade2sql-docker/db_setup.py

USER acade
ENTRYPOINT ["/usr/local/bin/acade2sql-entrypoint"]
# `python -m pytest` (not `pytest`) so /app is on sys.path for `from tests.conftest import ...`.
CMD ["sh", "-c", "python -m pytest -p no:cacheprovider -m live_db -q && python scripts/e2e_live.py"]
