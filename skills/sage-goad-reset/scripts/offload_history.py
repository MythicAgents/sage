#!/usr/bin/env python3
"""Move old Sage runtime archives off this host: copy, verify every file's SHA-256, then delete the local copy.

Two kinds of item qualify once they are older than ``--older-than-days`` (default 7):

* folders in ``.sage_history/runtime-db-archive/`` (each reset's ``reset-<stamp>/`` and any hand-made folder),
  copied to ``<to>/runtime-db-archive/<name>/``;
* stamped files that older resets and the eval gauge's between-arm archive left beside the live databases:
  ``sage_<stamp>.db``, ``sage_operation_memory_<stamp>.db`` and ``.phoenix/phoenix_<stamp>.db`` (each with its
  ``-wal``, ``-shm`` or ``.zst``), plus ``.sage_engagement_<stamp>/`` and ``esl_exports_<stamp>/``, in
  ``Payload_Type/sage`` and in the installed service directory, copied to ``<to>/rotated-dbs/{local,installed}/``.
  Names must match the whole pattern, so a source file such as ``sage_entrypoint.py`` never qualifies and the live
  ``sage.db`` and ``phoenix.db`` never do.

An item's age comes from the date in its name, else from its newest file. Without ``--yes`` the script only prints
its plan. A database and its ``-wal``/``-shm`` move together, and nothing local is deleted unless every file of that
item verified. A destination file that already exists is reused only when its hash matches, so an interrupted run
can be repeated.

The destination must already exist and be on a different filesystem from the archive: an unmounted drive's empty
mount point would otherwise fill this disk. A folder that a folder staying here still references (an
``already_preserved`` entry in its ``MANIFEST.json``, which resets wrote before each folder held its own copies) is
kept, because moving it alone would leave that reference pointing off the host; offload both together.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / ".sage_history" / "runtime-db-archive"
OFFLOAD_ENV = "SAGE_HISTORY_OFFLOAD_DIR"

ROTATED_DATABASE = re.compile(r"^(?:sage|sage_operation_memory)_\d{8}[-0-9a-z]*\.db(?:-wal|-shm|\.zst)?$")
ROTATED_PHOENIX = re.compile(r"^phoenix_(?:pre_upgrade_)?\d{8}[-0-9a-z]*\.db(?:-wal|-shm|\.zst)?$")
ROTATED_TREE = re.compile(r"^(?:\.sage_engagement|esl_exports)_\d{8}-\d{4}$")
_SIDECAR = re.compile(r"-(?:wal|shm)$")
_COMPACT_STAMP = re.compile(r"(?<!\d)(\d{8})(?:-(\d{4}))?(?!\d)")
_DASHED_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

# Indirection so a test can corrupt one copy and show that nothing local is deleted.
_copy_file = shutil.copyfile


@dataclass
class Item:
    """One unit that moves or stays as a whole."""

    label: str
    source_root: Path
    files: list[Path]
    dest_root: Path
    folder: Path | None = None  # removed as a whole after its files verify; otherwise ``files`` are removed
    when: datetime | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(path.stat().st_size for path in self.files)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        try:  # read the destination back from the device, not from the page cache this run just filled
            os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp_from_name(name: str) -> datetime | None:
    for match in _COMPACT_STAMP.finditer(name):
        try:
            return datetime.strptime(match.group(1) + (match.group(2) or "0000"), "%Y%m%d%H%M")
        except ValueError:
            continue
    match = _DASHED_DATE.search(name)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _newest(files: Iterable[Path]) -> datetime | None:
    times = [path.stat().st_mtime for path in files]
    return datetime.fromtimestamp(max(times)) if times else None


def _tree_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def installed_service_dir(mythic_env_path: str | None) -> Path | None:
    spec = importlib.util.spec_from_file_location(
        "archive_reset_evidence", Path(__file__).resolve().with_name("archive_reset_evidence.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.installed_service_dir(mythic_env_path)


def evidence_items(evidence_root: Path, dest: Path) -> list[Item]:
    items = []
    for folder in sorted(path for path in evidence_root.iterdir() if path.is_dir()) if evidence_root.is_dir() else []:
        files = _tree_files(folder)
        items.append(Item(label=f"runtime-db-archive/{folder.name}", source_root=folder, files=files,
                          dest_root=dest / "runtime-db-archive" / folder.name, folder=folder,
                          when=stamp_from_name(folder.name) or _newest(files)))
    return items


def rotated_items(directory: Path, area: str, dest: Path) -> list[Item]:
    """Stamped history left beside a Sage's live databases; the live files never match."""
    if not directory.is_dir():
        return []
    dest_root = dest / "rotated-dbs" / area
    groups: dict[Path, list[Path]] = {}
    for parent, pattern in ((directory, ROTATED_DATABASE), (directory / ".phoenix", ROTATED_PHOENIX)):
        if parent.is_dir():
            for path in parent.iterdir():
                if path.is_file() and not path.is_symlink() and pattern.fullmatch(path.name):
                    groups.setdefault(path.with_name(_SIDECAR.sub("", path.name)), []).append(path)
    items = [Item(label=f"{area}/{key.relative_to(directory)}", source_root=directory, files=sorted(files),
                  dest_root=dest_root, when=stamp_from_name(key.name))
             for key, files in sorted(groups.items())]
    for path in sorted(directory.iterdir()):
        if path.is_dir() and not path.is_symlink() and ROTATED_TREE.fullmatch(path.name):
            items.append(Item(label=f"{area}/{path.name}", source_root=directory, files=_tree_files(path),
                              dest_root=dest_root, folder=path, when=stamp_from_name(path.name)))
    return items


def _references(folder: Path) -> set[str]:
    """Names of other evidence folders this folder's manifest points into."""
    try:
        manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names = set()
    for entry in manifest.get("items") or []:
        held = entry.get("already_preserved") if isinstance(entry, dict) else None
        if isinstance(held, str) and held:
            names.add(held.split("/", 1)[0])
    return names - {folder.name}


def keep_referenced(all_folders: list[Item], chosen: list[Item]) -> list[str]:
    """Drop from ``chosen`` every folder that a folder staying here references, repeating until stable."""
    notes = []
    chosen_names = {item.folder.name for item in chosen}
    references = {item.folder.name: _references(item.folder) for item in all_folders}
    changed = True
    while changed:
        changed = False
        for name, referenced in references.items():
            if name in chosen_names:
                continue
            for target in sorted(referenced & chosen_names):
                chosen_names.discard(target)
                notes.append(f"kept runtime-db-archive/{target}: {name} still references it; "
                             f"offload both together (a larger --older-than-days)")
                changed = True
    chosen[:] = [item for item in chosen if item.folder.name in chosen_names]
    return notes


def plan(*, evidence_root: Path, runtime_dir: Path, installed: Path | None, dest: Path,
         older_than_days: float, now: datetime | None = None) -> tuple[list[Item], list[str]]:
    cutoff = (now or datetime.now()) - timedelta(days=older_than_days)
    folders = evidence_items(evidence_root, dest)
    chosen = [item for item in folders if item.when is not None and item.when < cutoff]
    notes = keep_referenced(folders, chosen)
    rotated = rotated_items(runtime_dir, "local", dest)
    if installed is not None:
        rotated += rotated_items(installed, "installed", dest)
    chosen += [item for item in rotated if item.when is not None and item.when < cutoff]
    return chosen, notes


def _copy_verified(source: Path, target: Path) -> str | None:
    """Copy one file unless an identical one is there, and confirm the copy's hash. Returns a problem or None."""
    digest = _sha256(source)
    if target.exists():
        if target.is_file() and _sha256(target) == digest:
            return None
        return f"{target} already exists with different content"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".offload-partial")
    _copy_file(source, partial)
    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    stat = source.stat()
    try:
        os.utime(partial, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass  # some external filesystems keep coarse times; the hash is what counts
    os.replace(partial, target)
    if _sha256(target) != digest:
        return f"the copy at {target} does not match {source}"
    return None


def offload_item(item: Item) -> bool:
    """Copy and verify every file; delete the local copy only if all of them verified."""
    for path in item.files:
        problem = _copy_verified(path, item.dest_root / path.relative_to(item.source_root))
        if problem:
            item.problems.append(problem)
    if item.problems:
        return False
    if item.folder is not None:
        shutil.rmtree(item.folder)
    else:
        for path in item.files:
            path.unlink()
    return True


def _same_filesystem(dest: Path, roots: Iterable[Path]) -> list[Path]:
    device = dest.stat().st_dev
    return [root for root in roots if root.exists() and root.stat().st_dev == device]


def _gb(size: int) -> str:
    return f"{size / 1e9:.2f} GB"


def main(argv: list[str] | None = None, *, require_other_filesystem: bool = True) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", type=Path, default=os.environ.get(OFFLOAD_ENV) or None,
                        help=f"existing destination directory on another filesystem (default: ${OFFLOAD_ENV})")
    parser.add_argument("--older-than-days", type=float, default=7.0)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--mythic-env-path", default=os.environ.get("MYTHIC_ENV_PATH", ""),
                        help="finds the installed service directory; without it only the repo's files qualify")
    parser.add_argument("--yes", action="store_true", help="copy, verify and delete; without it, only print the plan")
    args = parser.parse_args(argv)

    if args.to is None:
        raise SystemExit(f"ERR: name the destination with --to or {OFFLOAD_ENV}")
    dest = args.to.expanduser()
    if not dest.is_absolute() or not dest.is_dir():
        raise SystemExit(f"ERR: destination must be an existing absolute directory (is the drive mounted?): {dest}")
    evidence_root = args.evidence_root.expanduser().resolve()
    runtime_dir = args.repo_root.expanduser().resolve() / "Payload_Type" / "sage"
    installed = installed_service_dir(args.mythic_env_path)
    if require_other_filesystem:
        shared = _same_filesystem(dest, [evidence_root, runtime_dir, *([installed] if installed else [])])
        if shared:
            raise SystemExit(f"ERR: {dest} is on the same filesystem as {shared[0]}; offloading there frees nothing "
                             f"(is the drive mounted?)")

    items, notes = plan(evidence_root=evidence_root, runtime_dir=runtime_dir, installed=installed, dest=dest,
                        older_than_days=args.older_than_days)
    for note in notes:
        print(note)
    if not items:
        print(f"Nothing older than {args.older_than_days:g} days to offload.")
        return 0
    total = sum(item.size for item in items)
    if not args.yes:
        for item in items:
            print(f"would move {item.label} ({_gb(item.size)}, {len(item.files)} files)")
        print(f"{len(items)} items, {_gb(total)}, to {dest}. Run again with --yes to copy, verify and delete.")
        return 0
    moved = freed = 0
    failed = []
    for item in items:
        size = item.size
        if offload_item(item):
            moved += 1
            freed += size
            print(f"moved {item.label} ({_gb(size)})")
        else:
            failed.append(item)
            print(f"kept {item.label}: " + "; ".join(item.problems))
    print(f"moved {moved} of {len(items)} items, freed {_gb(freed)}; copies in {dest}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
