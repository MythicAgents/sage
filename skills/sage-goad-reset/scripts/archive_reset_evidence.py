#!/usr/bin/env python3
"""Preserve a run's evidence before a reset, then archive the local runtime databases.

Evidence is copied and verified first; the local databases are moved only after every copy succeeds, so a
failed copy leaves everything where it was and blocks the reset. Copying never changes a source, which makes it
safe while the Sage container and Mythic are still running:

* SQLite databases in the installed service directory (``sage*.db`` and ``.phoenix/phoenix*.db``) are copied with
  SQLite's online backup API from read-only connections, so pages still in the WAL are included.
* The installed ``.sage_engagement`` tree (engagement state and sealed decision records) is copied as is.
* Mythic's database is dumped with ``pg_dump`` inside its Postgres container using that container's own
  credentials, so no secret is passed on a command line or leaves the container. Mythic's reset wipes this
  database, and it holds the task output that runtime proof depends on.

Data an earlier reset already preserved is not copied again. A database whose size and timestamps (with its
``-wal``) match a copy an earlier ``MANIFEST.json`` recorded is referenced without being read; otherwise it is backed
up and, if the copy's SHA-256 equals one an earlier reset still holds, the duplicate is removed and referenced. The
SQLite backup is deterministic, so the hash matches across resets. A reference is used only while the earlier copy
still exists.

``MANIFEST.json`` records every item with its size and SHA-256. Then the serving Sage's own state is moved aside:

* ``--target local`` (default): the local tmux databases, exactly as ``archive_runtime_dbs.py`` does.
* ``--target installed``: the container's databases and ``.sage_engagement`` inside the installed service
  directory. The repo's databases are left alone; they belong to the local Sage.

The installed service directory is found from ``MYTHIC_ENV_PATH``; when that is unset the container copy is skipped
and the manifest says so, and ``--target installed`` refuses.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / ".sage_history" / "runtime-db-archive"
SERVICE_NAME = "sage"
DEFAULT_POSTGRES_CONTAINER = "mythic_postgres"
SQLITE_PATTERNS = ("sage*.db", ".phoenix/phoenix*.db")
TREES = (".sage_engagement",)
# Runs inside the Postgres container so the password stays in that container's environment.
PG_DUMP = 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-password'

Runner = Callable[..., subprocess.CompletedProcess]


class EvidenceError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    """Copy one live SQLite database through the online backup API, then verify the copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        raise EvidenceError(f"integrity check failed for the copy of {source}: {integrity}")
    return {"kind": "sqlite-backup", "integrity": integrity}


def installed_service_dir(mythic_env_path: str | None) -> Path | None:
    value = (mythic_env_path or "").strip()
    if not value:
        return None
    installed = Path(value).expanduser().resolve().parent / "InstalledServices" / SERVICE_NAME
    return installed if installed.is_dir() else None


def _sqlite_sources(installed: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in SQLITE_PATTERNS:
        found.update(path for path in installed.glob(pattern) if path.is_file())
    return sorted(found)


def dump_mythic(container: str, destination: Path, *, run: Runner = subprocess.run) -> dict[str, Any]:
    """Dump Mythic's database read-only and confirm the archive lists table data."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        result = run(["docker", "exec", container, "sh", "-c", PG_DUMP],
                     stdout=handle, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise EvidenceError(f"pg_dump in {container} exited {result.returncode}: "
                            f"{(result.stderr or b'').decode(errors='replace').strip()[-500:]}")
    with destination.open("rb") as handle:
        listing = run(["docker", "exec", "-i", container, "pg_restore", "--list"],
                      stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    tables = (listing.stdout or b"").decode(errors="replace").count("TABLE DATA")
    if listing.returncode or tables == 0:
        raise EvidenceError(f"the Mythic dump from {container} could not be listed or holds no table data")
    return {"kind": "pg_dump -Fc", "table_data_entries": tables}


def _source_stat(source: Path, relative: Path) -> dict[str, Any]:
    wal = source.with_name(source.name + "-wal")
    stat, wal_stat = source.stat(), (wal.stat() if wal.exists() else None)
    return {"name": str(relative), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "wal_bytes": wal_stat.st_size if wal_stat else 0, "wal_mtime_ns": wal_stat.st_mtime_ns if wal_stat else 0}


def _source_key(stat: dict[str, Any]) -> tuple:
    return tuple(stat.get(key) for key in ("name", "bytes", "mtime_ns", "wal_bytes", "wal_mtime_ns"))


def _earlier_copies(destination: Path) -> tuple[dict[str, str], dict[tuple, dict[str, Any]]]:
    """Database copies held by earlier reset folders beside ``destination``: by copy hash and by source stat.

    A copy counts only if it still exists at the size its manifest recorded; the fresh backup is deleted on
    that copy's word. This stops NEW references to a missing copy. It cannot protect references already
    written: deleting an evidence folder that later folders reference loses that data (see SKILL.md).
    """
    root = destination.parent
    by_sha: dict[str, str] = {}
    by_source: dict[tuple, dict[str, Any]] = {}
    for manifest_path in sorted(root.glob("reset-*/MANIFEST.json")):
        folder = manifest_path.parent
        if folder == destination:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in manifest.get("items") or []:
            if not isinstance(item, dict) or item.get("kind") != "sqlite-backup" or not item.get("sha256"):
                continue
            held = item.get("already_preserved") or f"{folder.name}/{item.get('path')}"
            copy = root / held
            if not copy.is_file() or (item.get("bytes") is not None and copy.stat().st_size != item["bytes"]):
                continue
            by_sha.setdefault(item["sha256"], held)
            if isinstance(item.get("source"), dict):
                by_source[_source_key(item["source"])] = {"held": held, "sha256": item["sha256"],
                                                          "bytes": item.get("bytes")}
    return by_sha, by_source


def preserve_evidence(
    destination: Path,
    *,
    installed: Path | None,
    postgres_container: str | None,
    run: Runner = subprocess.run,
) -> dict[str, Any]:
    """Copy every evidence source into a new directory and write its manifest. Sources are never changed."""
    destination.mkdir(parents=True, exist_ok=False)
    items: list[dict[str, Any]] = []
    skipped: list[str] = []

    def record(path: Path, info: dict[str, Any], sha256: str | None = None) -> None:
        items.append({"path": str(path.relative_to(destination)), "bytes": path.stat().st_size,
                      "sha256": sha256 or _sha256(path), **info})

    if installed is None:
        skipped.append("installed service directory: MYTHIC_ENV_PATH unset or no installed Sage service")
    else:
        by_sha, by_source = _earlier_copies(destination)
        for source in _sqlite_sources(installed):
            relative = source.relative_to(installed)
            target = destination / "installed" / relative
            stat = _source_stat(source, relative)
            known = by_source.get(_source_key(stat))
            if known:
                items.append({"path": str(target.relative_to(destination)), "kind": "sqlite-backup",
                              "bytes": known["bytes"], "sha256": known["sha256"],
                              "already_preserved": known["held"], "source": stat})
                continue
            info = backup_sqlite(source, target)
            # Record the source identity only if the file did not change while it was backed up; a write
            # after the snapshot that kept the size would otherwise let a later reset skip that write.
            if _source_stat(source, relative) != stat:
                stat = None
            digest = _sha256(target)
            held = by_sha.get(digest)
            if held:
                size = target.stat().st_size
                for path in (target, target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
                    path.unlink(missing_ok=True)  # the duplicate this run just made, never a source
                items.append({"path": str(target.relative_to(destination)), "bytes": size, "sha256": digest,
                              **info, "already_preserved": held, **({"source": stat} if stat else {})})
            else:
                record(target, {**info, **({"source": stat} if stat else {})}, digest)
        for name in TREES:
            if not (installed / name).is_dir():
                continue
            shutil.copytree(installed / name, destination / "installed" / name)
            for path in sorted((destination / "installed" / name).rglob("*")):
                if path.is_file():
                    record(path, {"kind": "copy"})

    if postgres_container is None:
        skipped.append("Mythic database dump: skipped by the operator")
    else:
        target = destination / "mythic_db.pgdump"
        record(target, dump_mythic(postgres_container, target, run=run))

    manifest = {
        "purpose": "evidence preserved before a reset",
        "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "installed_service_dir": str(installed) if installed else None,
        "postgres_container": postgres_container,
        "items": items,
        "skipped": skipped,
    }
    (destination / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _load_local_archiver():
    spec = importlib.util.spec_from_file_location(
        "archive_runtime_dbs", Path(__file__).resolve().with_name("archive_runtime_dbs.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None, *, run: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--timestamp", help="Override the local timestamp; format YYYYMMDD-HHMM.")
    parser.add_argument("--sage-db", type=Path, help="Explicit absolute local application database path.")
    parser.add_argument("--mythic-env-path", default=os.environ.get("MYTHIC_ENV_PATH", ""))
    parser.add_argument("--postgres-container", default=DEFAULT_POSTGRES_CONTAINER)
    parser.add_argument("--skip-mythic-dump", action="store_true",
                        help="Do not dump Mythic's database. Its task records are then lost on reset.")
    parser.add_argument("--target", choices=("local", "installed"), default="local",
                        help="whose state to move aside after the evidence copy: the local tmux Sage's "
                             "(default) or the container's, in Mythic's installed service directory")
    args = parser.parse_args(argv)
    installed = installed_service_dir(args.mythic_env_path)
    if args.target == "installed" and installed is None:
        raise SystemExit("ERR: --target installed needs MYTHIC_ENV_PATH (or --mythic-env-path) to name a Mythic "
                         "install with Sage in InstalledServices/sage")

    local = _load_local_archiver()
    stamp = args.timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    if not local.STAMP_RE.fullmatch(stamp):
        raise SystemExit("ERR: --timestamp must use YYYYMMDD-HHMM")
    destination = args.evidence_root.expanduser().resolve() / f"reset-{stamp}"
    if destination.exists():
        raise SystemExit(f"ERR: evidence destination already exists: {destination}")

    try:
        manifest = preserve_evidence(
            destination,
            installed=installed,
            postgres_container=None if args.skip_mythic_dump else args.postgres_container,
            run=run,
        )
    except (EvidenceError, OSError, sqlite3.Error) as exc:
        raise SystemExit(f"ERR: evidence was not preserved, so nothing was archived or reset: {exc}. "
                         f"Partial copy left at {destination} for inspection.") from exc

    if args.target == "installed":
        moves = local.archive_installed_runtime_state(installed, timestamp=stamp)
    else:
        root = args.repo_root.resolve()
        moves = local.archive_runtime_dbs(root, timestamp=stamp,
                                          sage_database_path=args.sage_db or local.configured_database_path(root),
                                          include_ledger=True)
    print(json.dumps({
        "target": args.target,
        "evidence_dir": str(destination),
        "evidence_items": len(manifest["items"]),
        "evidence_skipped": manifest["skipped"],
        "archived": [[str(source), str(target)] for source, target in moves],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
