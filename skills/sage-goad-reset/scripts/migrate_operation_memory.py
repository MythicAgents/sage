#!/usr/bin/env python3
"""Explicit offline import of canonical legacy operation memory into sage.db."""

import argparse
from pathlib import Path
import sys


SAGE_ROOT = Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE_ROOT))

from runtime_db import database_path, migrate_legacy_operation_memory  # noqa: E402
from archive_runtime_dbs import configured_database_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sage-stopped", action="store_true", help="Confirm Sage is stopped for offline import")
    parser.add_argument("--database", type=Path, default=None, help="Absolute destination; default SAGE_DATABASE_PATH or Sage sage.db")
    parser.add_argument("--legacy", type=Path, default=None, help="Absolute legacy source; default original Sage-root sage_operation_memory.db")
    args = parser.parse_args()
    if not args.sage_stopped:
        parser.error("Stop Sage first, then pass --sage-stopped")
    target = args.database or configured_database_path(SAGE_ROOT.parents[1]) or database_path()
    if not target.is_absolute() or (args.legacy is not None and not args.legacy.is_absolute()):
        parser.error("Database paths must be absolute")
    changed = migrate_legacy_operation_memory(target, legacy_path=args.legacy)
    print("Migrated legacy operation memory." if changed else "No migration needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
