#!/usr/bin/env python3
"""Offline import and export of the Engagement State Ledger (ESL) in sage.db.

``status``   lists legacy ``state_*.json`` files that are not yet imported (or changed since import), and each
             operation's schema version with the number of rows the schema migration retracted or moved to
             ``pending``. Read-only: the migration itself runs when Sage next opens the database.
``import``   imports legacy files into sage.db. Sage must be stopped. Each file is verified and committed on its
             own; a malformed file, duplicate identity or number, changed source, or an operation already in the
             destination fails that file and leaves the destination unchanged. Sources are never modified, and an
             identical repeat import is a no-op. Files without a full ``<Name>_<id>_<uuid>`` engagement id need
             ``--map FILE=KEY`` or ``--exclude FILE``; nothing is attached to an operation silently.
``export``   writes one JSON export per operation (schema-versioned, with full mutation history) plus a manifest,
             from one read-only snapshot of the given database. It never changes the database.

The legacy directory identifies legacy files only; it is never a live store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SAGE_ROOT = Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE_ROOT))
sys.path.insert(0, str(SAGE_ROOT / "ai" / "langgraph"))

import engagement_store  # noqa: E402
from runtime_db import database_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive_runtime_dbs import configured_database_path  # noqa: E402


def _database(value: Path | None) -> Path:
    target = value or configured_database_path(SAGE_ROOT.parents[1]) or database_path()
    if not target.is_absolute():
        raise SystemExit("ERR: --database must be an absolute path")
    return target


def _legacy_dir(value: Path | None) -> Path:
    if value is None:
        return Path(engagement_store.legacy_state_dir())
    if not value.is_absolute():
        raise SystemExit("ERR: --legacy-dir must be an absolute path")
    return value


def _mapping(items: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        name, sep, key = item.partition("=")
        if not sep or not name.strip() or not key.strip():
            raise SystemExit(f"ERR: --map expects FILE=ENGAGEMENT_KEY, got {item!r}")
        mapping[name.strip()] = key.strip()
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="list legacy files still needing import, and each operation's schema version (read-only)"
    )
    status.add_argument("--database", type=Path)
    status.add_argument("--legacy-dir", type=Path)

    imp = sub.add_parser("import", help="import legacy state_*.json files (Sage stopped)")
    imp.add_argument("--sage-stopped", action="store_true", help="confirm Sage is stopped for the offline import")
    imp.add_argument("--database", type=Path)
    imp.add_argument("--legacy-dir", type=Path)
    imp.add_argument("--map", action="append", default=[], metavar="FILE=KEY",
                     help="explicit engagement key for a file whose own id does not identify an operation")
    imp.add_argument("--exclude", action="append", default=[], metavar="FILE",
                     help="record a legacy file as deliberately not imported")

    exp = sub.add_parser("export", help="export every operation in a database (read-only)")
    exp.add_argument("--database", type=Path)
    exp.add_argument("--out", type=Path, required=True, help="destination directory for exports and manifest")

    args = parser.parse_args(argv)
    database = _database(args.database)

    if args.command == "status":
        store = engagement_store.EngagementStore(database, legacy_dir=str(_legacy_dir(args.legacy_dir)))
        pending = store.pending_legacy_files() if database.exists() else [
            str(path) for path in engagement_store._legacy_files(_legacy_dir(args.legacy_dir))
        ]
        operations = engagement_store.schema_status(database)
        print(json.dumps({
            "database": str(database),
            "pending": pending,
            "current_schema_version": engagement_store.SCHEMA_VERSION,
            "operations": operations,
        }, indent=2))
        return 1 if pending else 0

    if args.command == "import":
        if not args.sage_stopped:
            parser.error("Stop Sage first, then pass --sage-stopped")
        report = engagement_store.import_legacy_directory(
            _legacy_dir(args.legacy_dir), database=database, mapping=_mapping(args.map), exclude=args.exclude,
        )
        print(json.dumps({"database": str(database), "files": report}, indent=2))
        return 1 if any(value.startswith("refused") for value in report.values()) else 0

    if not database.exists():
        raise SystemExit(f"ERR: database not found: {database}")
    written = engagement_store.export_database(database, args.out)
    print(json.dumps({"database": str(database), "exports": written}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
