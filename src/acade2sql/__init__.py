"""acade2sql: convert AutoCAD Electrical Access databases (.mdb/.accdb) to SQL."""

from acade2sql.errors import (
    Acade2SqlError,
    BitnessMismatchError,
    DriverNotFoundError,
    SourceNotFoundError,
    TableNotFoundError,
)
from acade2sql.readers import Reader, open_reader
from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo

__version__ = "0.1.0"

__all__ = [
    "Acade2SqlError",
    "BitnessMismatchError",
    "ColumnInfo",
    "DriverNotFoundError",
    "IndexInfo",
    "Reader",
    "SourceNotFoundError",
    "TableInfo",
    "TableNotFoundError",
    "__version__",
    "open_reader",
]
