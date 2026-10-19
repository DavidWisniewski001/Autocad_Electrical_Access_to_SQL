"""acade2sql: convert AutoCAD Electrical Access databases (.mdb/.accdb) to SQL."""

# The implementation lives in ``acade2sql.converter``; ``acade2sql.convert`` is the plain
# function (there is no module of that name). See DECISIONS #18.
from acade2sql.converter import convert, resolve_target
from acade2sql.errors import (
    Acade2SqlError,
    BitnessMismatchError,
    DriverNotFoundError,
    SourceNotFoundError,
    TableNotFoundError,
    TargetError,
    WriteError,
)
from acade2sql.readers import Reader, open_reader
from acade2sql.redact import mask_url, redact
from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo
from acade2sql.validate import Report, TableResult

__version__ = "0.2.0"

__all__ = [
    "Acade2SqlError",
    "BitnessMismatchError",
    "ColumnInfo",
    "DriverNotFoundError",
    "IndexInfo",
    "Reader",
    "Report",
    "SourceNotFoundError",
    "TableInfo",
    "TableNotFoundError",
    "TableResult",
    "TargetError",
    "WriteError",
    "__version__",
    "convert",
    "mask_url",
    "open_reader",
    "redact",
    "resolve_target",
]
