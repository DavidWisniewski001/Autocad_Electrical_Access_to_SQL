"""Build the synthetic Access fixtures used by the Docker live tests.

Usage (Windows with the Microsoft Access ODBC driver)::

    python scripts/build_test_fixtures.py [OUT_DIR]        # default: build/fixtures

Writes ``fixture.mdb``, ``fixture.accdb`` and ``MANIFEST.json`` (expected table names, row counts,
primary keys and the target-side tamper plan used by ``tests/test_live_db.py``). The files are
synthetic and are kept out of git; ``docker compose`` mounts the directory read-only at
``/fixtures`` (``ACADE2SQL_TEST_FIXTURE_DIR``), where the Linux runner reads them with
``--reader pure``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures import build_access  # noqa: E402

FORMATS = ("mdb", "accdb")


def build_fixtures(out_dir: Path) -> dict:
    """Build both fixtures and the manifest in ``out_dir``; return the manifest."""
    if not build_access.driver_available():
        raise SystemExit(
            "error: building the fixtures needs Windows and the Microsoft Access ODBC driver "
            "(install the 'Microsoft Access Database Engine' redistributable matching your "
            "Python's bitness, then re-run). On Linux, copy a prebuilt build/fixtures directory."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in FORMATS:
        target = out_dir / f"fixture.{ext}"
        if target.exists():
            target.unlink()
        build_access.build(target)
        print(f"built {target} ({target.stat().st_size:,} bytes)")
    manifest = build_access.manifest_json(FORMATS)
    path = out_dir / "MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(manifest['tables'])} tables)")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", nargs="?", default=str(ROOT / "build" / "fixtures"))
    args = ap.parse_args(argv)
    build_fixtures(Path(args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
