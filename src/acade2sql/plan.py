"""``--dry-run``: the plan and DDL of a conversion, without connecting to the target.

The target dialect comes from the URL through :func:`sqlalchemy.create_mock_engine`, which needs
neither the DBAPI module (psycopg, pyodbc) nor a server; DDL is compiled by that dialect.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import MetaData, Table, create_mock_engine
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import URL, make_url
from sqlalchemy.schema import DropTable

from acade2sql.errors import Acade2SqlError, TargetError
from acade2sql.keys import NO_KEY_MESSAGE, key_for
from acade2sql.modes import ReadFailed, RunOptions, error_text, read, unmapped_type_warnings
from acade2sql.readers import Reader
from acade2sql.redact import mask_url, redact
from acade2sql.schema import TableInfo
from acade2sql.typemap import DIALECTS
from acade2sql.validate import TableResult
from acade2sql.writers import build_table
from acade2sql.writers.dialects import check_pg_identifiers

log = logging.getLogger(__name__)


class DdlCollector:
    """A mock engine for ``url``'s dialect that records the DDL it is asked to execute."""

    def __init__(self, url: str | URL) -> None:
        try:
            parsed = make_url(url)
            self.engine = create_mock_engine(parsed, self._execute)
        except (sa_exc.ArgumentError, sa_exc.NoSuchModuleError, ValueError, TypeError) as exc:
            text = redact(str(exc), url)
            raise TargetError(f"Invalid target URL {mask_url(url)}: {text}") from None
        self.dialect: str = self.engine.dialect.name
        if self.dialect not in DIALECTS:
            raise TargetError(
                f"Unsupported target dialect {self.dialect!r} ({mask_url(parsed)}); "
                f"supported: {', '.join(DIALECTS)}"
            )
        self.statements: list[str] = []

    def _execute(self, sql: Any, *_multiparams: Any, **_params: Any) -> None:
        self.statements.append(str(sql.compile(dialect=self.engine.dialect)).strip())

    def drop(self, table: Table) -> None:
        # DROP TABLE IF EXISTS on every dialect: the real run drops only if the table exists.
        self._execute(DropTable(table, if_exists=True))

    def create(self, table: Table) -> None:
        table.create(self.engine, checkfirst=False)  # CREATE TABLE + CREATE INDEX


def _actions(opts: RunOptions, key: tuple[str, ...] | None) -> list[str]:
    if opts.mode == "full":
        return ["drop", "create", "insert"]
    if opts.mode == "append":
        return ["create-if-missing", "append"]
    actions = ["create-if-missing", "upsert" if key else "replace-rows"]
    if opts.delete_missing and key:
        actions.append("delete-missing")
    return actions


def _plan_table(
    reader: Reader,
    ddl: DdlCollector,
    metadata: MetaData,
    used: set[str],
    info: TableInfo,
    opts: RunOptions,
    counts: bool,
) -> TableResult:
    unmapped = unmapped_type_warnings(info)
    res = TableResult(info.name, None, None, "planned", warnings=list(unmapped))
    try:
        table = build_table(metadata, info, ddl.dialect, used_index_names=used)
        res.warnings.extend(table.info.get("warnings", ()))
        key: tuple[str, ...] | None = None
        if opts.mode == "sync":
            key = key_for(info, opts.keys)
            res.key = list(key) if key else None
            if key is None and opts.no_key == "error":
                res.status, res.error = "error", NO_KEY_MESSAGE
        res.actions = _actions(opts, key)
        if table.info.get("error"):
            res.status, res.error = "error", str(table.info["error"])
        if opts.mode == "full":
            ddl.drop(table)
        ddl.create(table)
        if counts:
            res.source_rows = read(lambda: reader.count_rows(info.name))
        if unmapped and res.status == "planned":  # DECISIONS #21, as in the real run
            res.status = "fail"
    except ReadFailed as exc:
        res.status, res.error = "error", error_text(exc, None)
    except Acade2SqlError as exc:  # e.g. a --key column the table lacks
        res.status, res.error = "error", str(exc)
    return res


def dry_run(
    reader: Reader,
    infos: Sequence[TableInfo],
    url: str,
    opts: RunOptions,
    *,
    schema: str | None,
    counts: bool,
) -> tuple[list[TableResult], list[str], str]:
    """Plan every table; returns ``(results, ddl_statements, masked_target)``. No connection."""
    ddl = DdlCollector(url)
    if ddl.dialect == "postgresql":
        check_pg_identifiers(infos, schema)
    metadata = MetaData(schema=schema)
    used: set[str] = set()
    results = [_plan_table(reader, ddl, metadata, used, info, opts, counts) for info in infos]
    log.info(
        "dry run planned",
        extra={"kv": {"tables": len(results), "statements": len(ddl.statements)}},
    )
    return results, ddl.statements, mask_url(url)
