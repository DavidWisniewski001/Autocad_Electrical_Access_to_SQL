"""Target writers (SQLAlchemy Core)."""

from __future__ import annotations

from acade2sql.writers.sqlalchemy_writer import SqlAlchemyWriter, build_table

__all__ = ["SqlAlchemyWriter", "build_table"]
