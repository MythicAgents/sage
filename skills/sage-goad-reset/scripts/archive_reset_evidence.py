#!/usr/bin/env python3
"""Preserve a run's evidence before a reset, then move the serving Sage's state into the same folder.

Each reset writes one self-contained folder, ``.sage_history/runtime-db-archive/reset-<stamp>/``. It holds its own
bytes and never refers to another folder, so any folder can be moved off the host or deleted on its own
(``offload_history.py``), and nothing is left beside the live databases.

First, copies. They never change a source, so they are safe while the Sage container and Mythic still run:

* Mythic's database is dumped with ``pg_dump`` inside its Postgres container using that container's own
  credentials, so no secret is passed on a command line or leaves the container. Mythic's reset wipes this
  database, and it holds the task output that runtime proof depends on.
* With ``--target local``, the container's serving databases (``sage.db``, ``sage_operation_memory.db`` and
  ``.phoenix/phoenix.db`` in the installed service directory) are copied too, because a container may have run
  since its own last reset. They are copied with SQLite's online backup API from read-only connections, so pages
  still in the WAL are included, together with the installed ``.sage_engagement`` tree. Each copy that holds the
  Engagement State Ledger also gets JSON exports generated from that backup (``installed/esl_exports/<database>/``).
  Stamped files that older resets left beside the databases are not copied; ``offload_history.py`` moves them.

Only after every copy succeeds is the serving Sage's own state moved (renamed on one filesystem, not copied) into
the folder. That Sage must be stopped, which the reset's ``stop-sage`` phase guarantees:

* ``--target local`` (default): the local tmux databases and ``Payload_Type/sage/.sage_engagement``, into ``local/``.
* ``--target installed``: the container's databases and ``.sage_engagement`` in the installed service directory,
  into ``installed/``. The repo's databases are left alone; they belong to the local Sage.

A failed copy stops before anything moves, so the reset does not continue. ``MANIFEST.json`` records every item with
its size and SHA-256. The installed service directory is found from ``MYTHIC_ENV_PATH``; when that is unset the
container copy is skipped and the manifest says so, and ``--target installed`` refuses.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import functools
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
    """The installed Sage's serving databases only; stamped files beside them are history, not this run."""
    specs = _load_local_archiver().INSTALLED_DB_SPECS
    return [installed / relative for relative, _ in specs if (installed / relative).is_file()]


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


def _export_esl(copy: Path, destination_dir: Path) -> list[Path]:
    """Export the ESL held by a database backup (none when the copy has no ESL tables)."""
    archiver = _load_local_archiver()
    if not archiver.esl_present(copy):
        return []
    store = archiver._engagement_store(REPO_ROOT)
    written = store.export_database(copy, destination_dir)
    return [*(Path(path) for path in written.values()), destination_dir / store.MANIFEST_NAME]


def preserve_evidence(
    destination: Path,
    *,
    installed: Path | None,
    postgres_container: str | None,
    run: Runner = subprocess.run,
    copy_installed: bool = True,
) -> dict[str, Any]:
    """Copy every evidence source into a new directory and write its manifest. Sources are never changed.

    ``copy_installed=False`` leaves the installed service's state to be moved in afterwards (``--target installed``).
    """
    destination.mkdir(parents=True, exist_ok=False)
    items: list[dict[str, Any]] = []
    skipped: list[str] = []

    def record(path: Path, info: dict[str, Any]) -> None:
        items.append({"path": str(path.relative_to(destination)), "bytes": path.stat().st_size,
                      "sha256": _sha256(path), **info})

    if installed is None:
        skipped.append("installed service directory: MYTHIC_ENV_PATH unset or no installed Sage service")
    elif copy_installed:
        for source in _sqlite_sources(installed):
            relative = source.relative_to(installed)
            target = destination / "installed" / relative
            record(target, backup_sqlite(source, target))
            exports_dir = destination / "installed" / "esl_exports" / str(relative).replace("/", "_")
            for path in _export_esl(target, exports_dir):
                record(path, {"kind": "esl-export", "from_backup": str(target.relative_to(destination))})
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
    _write_manifest(destination, manifest)
    return manifest


def _write_manifest(destination: Path, manifest: dict[str, Any]) -> None:
    (destination / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def record_moved(destination: Path, manifest: dict[str, Any], area: Path,
                 moves: list[tuple[Path, Path]]) -> None:
    """Hash every file the move put under ``area`` into the manifest, and rewrite it."""
    origin = {Path(target): Path(source) for source, target in moves}
    for path in sorted(p for p in area.rglob("*") if p.is_file()) if area.is_dir() else []:
        kind = "esl-export" if "esl_exports" in path.relative_to(area).parts else "moved"
        item = {"path": str(path.relative_to(destination)), "bytes": path.stat().st_size,
                "sha256": _sha256(path), "kind": kind}
        if path in origin:
            item["source"] = str(origin[path])
        manifest["items"].append(item)
    _write_manifest(destination, manifest)


@functools.cache
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
            copy_installed=args.target != "installed",
        )
    except (EvidenceError, OSError, sqlite3.Error) as exc:
        raise SystemExit(f"ERR: evidence was not preserved, so nothing was archived or reset: {exc}. "
                         f"Partial copy left at {destination} for inspection.") from exc

    area = destination / args.target
    try:
        if args.target == "installed":
            moves = local.archive_installed_runtime_state(installed, timestamp=stamp, archive_dir=area)
        else:
            root = args.repo_root.resolve()
            moves = local.archive_runtime_dbs(root, timestamp=stamp,
                                              sage_database_path=args.sage_db or local.configured_database_path(root),
                                              include_ledger=True, archive_dir=area)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"ERR: evidence was preserved at {destination}, but moving the {args.target} Sage's "
                         f"state into it failed: {exc}") from exc
    record_moved(destination, manifest, area, moves)
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
