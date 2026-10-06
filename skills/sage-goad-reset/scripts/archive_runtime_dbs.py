#!/usr/bin/env python3
"""Archive active Sage runtime and Phoenix SQLite databases without deleting history."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import re

from dotenv import dotenv_values


DB_SPECS = (
    ("Payload_Type/sage/sage.db", "sage"),
    (
        "Payload_Type/sage/sage_operation_memory.db",
        "sage_operation_memory",
    ),
    ("Payload_Type/sage/.phoenix/phoenix.db", "phoenix"),
)
STAMP_RE = re.compile(r"^\d{8}-\d{4}$")


def archive_runtime_dbs(
    repo_root: Path,
    *,
    timestamp: str | None = None,
    sage_database_path: Path | None = None,
) -> list[tuple[Path, Path]]:
    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    if not STAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDD-HHMM")

    moves: list[tuple[Path, Path]] = []
    for relative_source, prefix in DB_SPECS:
        source = (sage_database_path if prefix == "sage" and sage_database_path is not None
                  else repo_root / relative_source)
        if not source.is_absolute():
            raise ValueError("application database path must be absolute")
        if source.exists():
            moves.append((source, source.with_name(f"{prefix}_{stamp}.db")))

    collisions: list[Path] = []
    for source, destination in moves:
        for suffix in ("", "-wal", "-shm"):
            source_member = source.with_name(source.name + suffix)
            destination_member = destination.with_name(destination.name + suffix)
            if source_member.exists() and destination_member.exists():
                collisions.append(destination_member)
    if collisions:
        rendered = ", ".join(str(path) for path in collisions)
        raise FileExistsError(f"archive destination already exists: {rendered}")

    for source, destination in moves:
        source.replace(destination)
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                sidecar.replace(destination.with_name(destination.name + suffix))
    return moves


def configured_database_path(repo_root: Path) -> Path | None:
    """Follow runtime environment precedence without exposing other config values."""
    value = os.environ.get("SAGE_DATABASE_PATH")
    if value is None:
        runtime = repo_root / "Payload_Type" / "sage"
        for name in (".env.local", ".env"):
            value = dotenv_values(runtime / name).get("SAGE_DATABASE_PATH")
            if value:
                break
    if not value or not value.strip():
        return None
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("SAGE_DATABASE_PATH must be an absolute path")
    return path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    parser.add_argument(
        "--timestamp",
        help="Override the local timestamp for testing; format YYYYMMDD-HHMM.",
    )
    parser.add_argument("--sage-db", type=Path, help="Explicit absolute application database path.")
    args = parser.parse_args()

    root = args.repo_root.resolve()
    moves = archive_runtime_dbs(root, timestamp=args.timestamp,
                               sage_database_path=args.sage_db or configured_database_path(root))
    if not moves:
        print("No active Sage or Phoenix runtime databases found.")
        return
    for source, destination in moves:
        print(f"Archived {source} -> {destination}")


if __name__ == "__main__":
    main()
