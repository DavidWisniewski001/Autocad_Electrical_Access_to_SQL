"""``python -m acade2sql``: the same as the ``acade2sql`` console script."""

import sys

from acade2sql.cli import main

if __name__ == "__main__":
    sys.exit(main())
