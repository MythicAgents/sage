"""Read-only corpus inventory for retained Sage/Phoenix/Plans artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Iterable

from .schema import SourceArtifact


def artifact_kind(path: Path) -> str | None:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if path.suffix.lower() == ".db" and ("phoenix" in name or ".phoenix" in parts):
        return "phoenix_db"
    if path.suffix.lower() == ".db" and name.startswith("sage"):
        return "sage_db"
    if path.suffix.lower() == ".json" and (name.startswith("state_") or ".sage_engagement" in parts):
        return "engagement_ledger"
    if name.startswith("essos_da") and path.suffix.lower() in {".out", ".log", ".txt"}:
        return "solve_log"
    if path.suffix.lower() in {".out", ".log"}:
        return "run_log"
    return None


def iter_candidate_paths(roots: Iterable[str | Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root_value in roots:
        root = Path(root_value).expanduser()
        if not root.exists():
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = (
                path
                for path in root.rglob("*")
                if path.is_file() and artifact_kind(path) is not None
            )
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


def build_manifest(roots: Iterable[str | Path], include_hash: bool = False) -> list[SourceArtifact]:
    artifacts: list[SourceArtifact] = []
    for path in sorted(iter_candidate_paths(roots), key=lambda p: str(p)):
        kind = artifact_kind(path)
        if not kind:
            continue
        readable = True
        note = ""
        digest = None
        try:
            stat = path.stat()
            if include_hash:
                digest = _sha256(path)
        except OSError as exc:
            stat = None
            readable = False
            note = str(exc)
        size = int(stat.st_size) if stat else 0
        mtime = (
            datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            if stat
            else ""
        )
        artifacts.append(
            SourceArtifact(
                path=str(path),
                kind=kind,
                size=size,
                mtime=mtime,
                readable=readable,
                sensitive=kind in {"phoenix_db", "sage_db", "engagement_ledger", "solve_log", "run_log"},
                sha256=digest,
                note=note,
            )
        )
    return artifacts


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
