#!/usr/bin/env python3
"""Archive active Sage runtime and Phoenix SQLite databases, and the local engagement ledger, without deleting history.

By default each database is renamed beside itself (``sage_<stamp>.db``, ``.phoenix/phoenix_<stamp>.db``), which is
what the eval gauge's between-arm call expects. With ``--archive-dir`` (the reset) every file moves instead into that
directory under the name it is served by (``sage.db``, ``.phoenix/phoenix.db``, ``.sage_engagement/``), so nothing
is left beside the live databases. Both are renames on one filesystem; a directory on another filesystem is refused
before anything moves.

The Engagement State Ledger (ESL) lives in sage.db. Archiving moves sage.db aside and writes JSON exports of its
ESL from that archived snapshot (``esl_exports_<stamp>/`` beside it, or ``esl_exports/`` in the archive
directory, with a manifest). Without ``--include-ledger`` (the eval gauge's between-arm call) the ESL is kept
between arms: its complete tables, sequences, import records and objective settings are carried into a fresh
sage.db; checkpoints and findings are not. With ``--include-ledger`` (the reset) the next database starts without
the ESL.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import errno
import os
from pathlib import Path
import re

import sqlite3
import sys

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
#: The same databases inside Mythic's installed service directory, which the container bind-mounts.
INSTALLED_DB_SPECS = (
    ("sage.db", "sage"),
    ("sage_operation_memory.db", "sage_operation_memory"),
    (".phoenix/phoenix.db", "phoenix"),
)
#: Engagement state the container writes beside its databases. Mythic reuses channel and operation ids
#: after its own reset, so a ledger left in place can pre-seed a new run with an old run's facts.
INSTALLED_TREES = (".sage_engagement",)
#: The name each database is served by, which is also its name inside an archive directory.
SERVED_NAMES = {prefix: relative for relative, prefix in INSTALLED_DB_SPECS}


def _archive_dir(archive_dir: Path | None) -> Path | None:
    if archive_dir is None:
        return None
    path = Path(archive_dir)
    if not path.is_absolute():
        raise ValueError("archive directory must be an absolute path")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database_target(source: Path, prefix: str, stamp: str, archive: Path | None) -> Path:
    return archive / SERVED_NAMES[prefix] if archive is not None else source.with_name(f"{prefix}_{stamp}.db")


def _require_same_filesystem(sources: list[Path], archive: Path) -> None:
    """A rename cannot cross filesystems; refuse before anything moves rather than half-archive."""
    device = archive.stat().st_dev
    for source in sources:
        if source.stat().st_dev != device:
            raise OSError(errno.EXDEV, f"{source} is on a different filesystem from {archive}, so it cannot be "
                                       f"renamed into it; nothing was moved")


def archive_runtime_dbs(
    repo_root: Path,
    *,
    timestamp: str | None = None,
    sage_database_path: Path | None = None,
    include_ledger: bool = False,
    archive_dir: Path | None = None,
) -> list[tuple[Path, Path]]:
    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    if not STAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDD-HHMM")
    archive = _archive_dir(archive_dir)

    moves: list[tuple[Path, Path]] = []
    sage_source: Path | None = None
    for relative_source, prefix in DB_SPECS:
        source = (sage_database_path if prefix == "sage" and sage_database_path is not None
                  else repo_root / relative_source)
        if not source.is_absolute():
            raise ValueError("application database path must be absolute")
        if source.exists():
            moves.append((source, _database_target(source, prefix, stamp, archive)))
            if prefix == "sage":
                sage_source = source
    # The tmux Sage's engagement ledger, beside its cwd (engagement_ledger.py's default; a run that sets
    # SAGE_ENGAGEMENT_STATE_DIR keeps its own elsewhere). The repo-root `.sage_engagement` holds reset
    # checkpoints and is a different directory. Opt-in: the eval gauge calls this script between arms
    # without flags, and moving the ledger there would change those runs' protocol.
    ledger = repo_root / "Payload_Type" / "sage" / ".sage_engagement"
    moves_ledger = include_ledger and ledger.is_dir()
    has_esl = sage_source is not None and esl_present(sage_source)
    exports_dir = (archive / "esl_exports" if archive is not None
                   else sage_source.with_name(f"esl_exports_{stamp}")) if has_esl else None
    ledger_target = archive / ledger.name if archive is not None else ledger.with_name(f".sage_engagement_{stamp}")
    # Preflight every destination before anything moves.
    if exports_dir is not None and exports_dir.exists():
        raise FileExistsError(f"archive destination already exists: {exports_dir}")
    if moves_ledger and ledger_target.exists():
        raise FileExistsError(f"archive destination already exists: {ledger_target}")
    if archive is not None:
        _require_same_filesystem([source for source, _ in moves] + ([ledger] if moves_ledger else []), archive)
    moved = _move_databases(moves)
    if has_esl:
        archived = dict(moves)[sage_source]
        moved.extend(_export_and_carry(archived, exports_dir, sage_source, carry=not include_ledger,
                                       repo_root=repo_root))
    if moves_ledger:
        ledger.replace(ledger_target)
        moved.append((ledger, ledger_target))
    return moved


def _engagement_store(repo_root: Path):
    langgraph = repo_root / "Payload_Type" / "sage" / "ai" / "langgraph"
    if str(langgraph) not in sys.path:
        sys.path.insert(0, str(langgraph))
    import engagement_store  # noqa: PLC0415 - runtime storage module, loaded from the repo being archived

    return engagement_store


def esl_present(database: Path) -> bool:
    """Whether a file is a SQLite database holding ESL tables (a non-database file holds none).

    The header is checked first so a non-SQLite file (and its sidecars) is never opened by SQLite.
    """
    try:
        with open(database, "rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                return False
    except OSError:
        return False
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        return bool(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='esl_operations'"
        ).fetchone())
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def _export_and_carry(archived: Path, exports_dir: Path, fresh: Path, *, carry: bool,
                      repo_root: Path) -> list[tuple[Path, Path]]:
    """Export the archived ESL; in carry mode, copy its ESL tables into a fresh database at the old path.

    A failure is explicit and never touches the archived database.
    """
    store = _engagement_store(repo_root)
    try:
        store.export_database(archived, exports_dir)
    except Exception as exc:
        raise RuntimeError(f"ESL export from {archived} failed (the archived database is kept): {exc}") from exc
    results = [(archived, exports_dir)]
    if carry:
        if any(fresh.with_name(fresh.name + suffix).exists() for suffix in ("", "-wal", "-shm")):
            raise FileExistsError(f"carry-forward destination already exists: {fresh}")
        try:
            store.copy_ledger_tables(archived, fresh)
        except Exception as exc:
            raise RuntimeError(
                f"ESL carry-forward into {fresh} failed (the archived database {archived} is kept): {exc}"
            ) from exc
        results.append((archived, fresh))
    return results


def archive_installed_runtime_state(installed: Path, *, timestamp: str | None = None,
                                    archive_dir: Path | None = None) -> list[tuple[Path, Path]]:
    """Move a container Sage's databases and engagement ledger aside: beside themselves inside its installed
    directory, or into ``archive_dir``.

    The container must be stopped. Only that directory is touched: the repo's databases belong to the
    local tmux Sage, a different deployment (`--sage-db` once moved the local phoenix.db instead).
    """
    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    if not STAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDD-HHMM")
    root = Path(installed)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(f"installed service directory must be an existing absolute path: {root}")
    archive = _archive_dir(archive_dir)
    trees = [(root / name, archive / name if archive is not None else root / f"{name}_{stamp}")
             for name in INSTALLED_TREES if (root / name).is_dir()]
    clashing = [str(target) for _, target in trees if target.exists()]
    if clashing:
        raise FileExistsError(f"archive destination already exists: {', '.join(clashing)}")
    moves = [
        (root / relative, _database_target(root / relative, prefix, stamp, archive))
        for relative, prefix in INSTALLED_DB_SPECS
        if (root / relative).exists()
    ]
    installed_sage = root / "sage.db"
    has_esl = installed_sage.exists() and esl_present(installed_sage)
    exports_dir = archive / "esl_exports" if archive is not None else root / f"esl_exports_{stamp}"
    if has_esl and exports_dir.exists():
        raise FileExistsError(f"archive destination already exists: {exports_dir}")
    if archive is not None:
        _require_same_filesystem([source for source, _ in moves] + [source for source, _ in trees], archive)
    moved = _move_databases(moves)
    if has_esl:
        # The installed runtime archives its ledger: the next database starts without the ESL.
        moved.extend(_export_and_carry(dict(moves)[installed_sage], exports_dir, installed_sage, carry=False,
                                       repo_root=Path(__file__).resolve().parents[3]))
    for source, target in trees:
        source.replace(target)
    return [*moved, *trees]


def _move_databases(moves: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
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
        destination.parent.mkdir(parents=True, exist_ok=True)  # `.phoenix/` inside an archive directory
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
    parser.add_argument("--include-ledger", action="store_true",
                        help="also move Payload_Type/sage/.sage_engagement aside (the reset does this)")
    parser.add_argument("--archive-dir", type=Path,
                        help="move everything into this absolute directory under its served names instead of "
                             "renaming it beside itself (the reset passes its evidence folder)")
    args = parser.parse_args()

    root = args.repo_root.resolve()
    moves = archive_runtime_dbs(root, timestamp=args.timestamp,
                               sage_database_path=args.sage_db or configured_database_path(root),
                               include_ledger=args.include_ledger, archive_dir=args.archive_dir)
    if not moves:
        print("No active Sage or Phoenix runtime databases found.")
        return
    vacated: set[Path] = set()
    for source, destination in moves:
        # A carry-forward writes a fresh database at a path an earlier move vacated.
        verb = "Exported ESL from" if destination.name.startswith("esl_exports") else (
            "Carried ESL tables from" if destination in vacated else "Archived")
        vacated.add(source)
        print(f"{verb} {source} -> {destination}")


if __name__ == "__main__":
    main()
