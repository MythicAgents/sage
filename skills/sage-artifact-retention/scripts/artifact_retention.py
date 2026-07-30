#!/usr/bin/env python3
"""Allocate, record, and explicitly promote private Sage artifacts."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable


MANIFEST_SCHEMA = "sage-artifact-manifest-v1"
DEFAULT_HISTORY_DIR = ".sage_history"
MAX_PROMOTE_BYTES = 100 * 1024 * 1024
SENSITIVE_NAME = re.compile(
    r"(^|[._-])(?:auth|cookie|credential|env|id_rsa|password|passwd|payload|"
    r"private[_-]?key|secret|token)(?:[._-]|$)",
    re.IGNORECASE,
)
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}


class RetentionError(ValueError):
    """Raised when an artifact cannot be retained safely."""


def repository_root(start: str | Path | None = None) -> Path:
    candidate = Path(start or __file__).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return Path(__file__).resolve().parents[3]


def history_root(root: str | Path | None = None) -> Path:
    repo = repository_root(root)
    configured = os.environ.get("SAGE_HISTORY_ROOT")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = repo / candidate
        return candidate.resolve()
    return (repo / DEFAULT_HISTORY_DIR).resolve()


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def initialize(root: str | Path | None = None) -> Path:
    durable = _private_directory(history_root(root))
    manifest = durable / "manifest.jsonl"
    if not manifest.exists():
        descriptor = {
            "schema": MANIFEST_SCHEMA,
            "record_kind": "archive-initialized",
            "recorded_at": datetime.now(UTC).isoformat(),
            "retention_class": "durable-private",
        }
        _append_manifest(durable, descriptor)
    return durable


def _category_parts(category: str) -> tuple[str, ...]:
    raw = str(category or "").strip().replace("\\", "/")
    parts = tuple(part for part in raw.split("/") if part)
    if not parts or any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
        for part in parts
    ):
        raise RetentionError(f"invalid artifact category: {category!r}")
    return parts


def _safe_name(name: str) -> str:
    candidate = Path(str(name or "")).name
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip(".-")
    if not candidate:
        raise RetentionError("artifact name must contain a safe filename")
    return candidate[:180]


def allocate_artifact_path(
    category: str,
    name: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    durable = initialize(root)
    directory = durable / moment.strftime("%Y") / moment.strftime("%m")
    for part in _category_parts(category):
        directory /= part
    _private_directory(directory)
    timestamp = moment.strftime("%Y%m%dT%H%M%S.%fZ")
    filename = f"{timestamp}-{_safe_name(name)}"
    candidate = directory / filename
    suffix = 1
    while candidate.exists():
        candidate = directory / f"{timestamp}-{suffix}-{_safe_name(name)}"
        suffix += 1
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_state(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    total_bytes = 0
    file_count = 0
    for child in sorted(path.rglob("*")):
        if not child.is_file() or child.is_symlink():
            continue
        relative = child.relative_to(path).as_posix()
        child_sha = _sha256_file(child)
        size = child.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(child_sha.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
        total_bytes += size
        file_count += 1
    return digest.hexdigest(), total_bytes, file_count


def _append_manifest(durable: Path, record: dict[str, Any]) -> None:
    _private_directory(durable)
    manifest = durable / "manifest.jsonl"
    payload = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        manifest,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    manifest.chmod(0o600)


def _relative_to_history(path: Path, durable: Path) -> str:
    try:
        return path.resolve().relative_to(durable.resolve()).as_posix()
    except ValueError as exc:
        raise RetentionError(f"artifact is outside the durable root: {path}") from exc


def record_artifact(
    path: str | Path,
    *,
    category: str,
    artifact_type: str,
    context: str = "",
    source_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    durable = initialize(root)
    artifact = Path(path).expanduser().resolve()
    if not artifact.is_file() or artifact.is_symlink():
        raise RetentionError(f"artifact must be a regular file: {artifact}")
    relative = _relative_to_history(artifact, durable)
    artifact.chmod(0o600)
    digest = _sha256_file(artifact)
    size = artifact.stat().st_size
    record = {
        "schema": MANIFEST_SCHEMA,
        "record_kind": "artifact",
        "artifact_id": hashlib.sha256(
            f"{relative}\0{digest}".encode("utf-8")
        ).hexdigest()[:24],
        "artifact_path": relative,
        "artifact_type": str(artifact_type),
        "category": "/".join(_category_parts(category)),
        "context": str(context),
        "recorded_at": datetime.now(UTC).isoformat(),
        "retention_class": "durable-private",
        "sha256": digest,
        "size_bytes": size,
    }
    if source_path is not None:
        source = Path(source_path).expanduser().resolve()
        record["source_path"] = str(source)
        record["source_sha256"] = digest
    _append_manifest(durable, record)
    return record


def _write_private(path: Path, payload: bytes) -> None:
    _private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json_artifact(
    category: str,
    name: str,
    payload: Any,
    *,
    artifact_type: str,
    context: str = "",
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    destination = allocate_artifact_path(category, name, root=root)
    _write_private(
        destination,
        (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            + "\n"
        ).encode("utf-8"),
    )
    record = record_artifact(
        destination,
        category=category,
        artifact_type=artifact_type,
        context=context,
        root=root,
    )
    return destination, record


def record_existing(
    paths: Iterable[str | Path],
    *,
    category: str,
    artifact_type: str,
    context: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    durable = initialize(root)
    records: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    for value in paths:
        candidate = Path(value).expanduser().resolve()
        _relative_to_history(candidate, durable)
        if candidate.is_file() and not candidate.is_symlink():
            records.append(
                record_artifact(
                    candidate,
                    category=category,
                    artifact_type=artifact_type,
                    context=context,
                    root=root,
                )
            )
            continue
        if not candidate.is_dir() or candidate.is_symlink():
            raise RetentionError(
                f"recorded path must be a regular file or directory: {candidate}"
            )
        files = [
            child
            for child in sorted(candidate.rglob("*"))
            if child.is_file() and not child.is_symlink()
        ]
        if not files:
            raise RetentionError(f"recorded directory contains no files: {candidate}")
        for child in files:
            records.append(
                record_artifact(
                    child,
                    category=category,
                    artifact_type=artifact_type,
                    context=context,
                    root=root,
                )
            )
        digest, size, file_count = _tree_state(candidate)
        directory_record = {
            "schema": MANIFEST_SCHEMA,
            "record_kind": "directory",
            "artifact_id": hashlib.sha256(
                f"{candidate.relative_to(durable).as_posix()}\0{digest}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24],
            "artifact_path": candidate.relative_to(durable).as_posix(),
            "artifact_type": str(artifact_type),
            "category": "/".join(_category_parts(category)),
            "context": str(context),
            "file_count": file_count,
            "recorded_at": datetime.now(UTC).isoformat(),
            "retention_class": "durable-private",
            "sha256": digest,
            "size_bytes": size,
        }
        _append_manifest(durable, directory_record)
        directories.append(directory_record)
    return {
        "ok": True,
        "artifact_count": len(records),
        "directory_count": len(directories),
        "size_bytes": sum(record["size_bytes"] for record in records),
    }


def _unsafe_reason(path: Path) -> str | None:
    if path.is_symlink():
        return "symlinks are not promoted"
    for part in path.parts:
        lowered = part.casefold()
        if lowered in {".env", ".mcp.json", "auth.json"}:
            return f"sensitive filename {part!r}"
        if SENSITIVE_NAME.search(lowered):
            return f"sensitive or payload-shaped filename {part!r}"
        if Path(lowered).suffix in SENSITIVE_SUFFIXES:
            return f"sensitive file suffix in {part!r}"
    if path.is_file() and path.stat().st_size > MAX_PROMOTE_BYTES:
        return f"file exceeds {MAX_PROMOTE_BYTES} bytes"
    return None


def _promotion_files(source: Path) -> list[Path]:
    reason = _unsafe_reason(source)
    if reason:
        raise RetentionError(f"refusing {source}: {reason}")
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise RetentionError(f"source does not exist or is not a regular path: {source}")
    files: list[Path] = []
    for child in sorted(source.rglob("*")):
        reason = _unsafe_reason(child)
        if reason:
            raise RetentionError(f"refusing {child}: {reason}")
        if child.is_file():
            files.append(child)
    if not files:
        raise RetentionError(f"source directory contains no regular files: {source}")
    return files


def promote(
    sources: Iterable[str | Path],
    *,
    category: str,
    artifact_type: str,
    context: str,
    root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    durable = initialize(root)
    planned: list[tuple[Path, list[Path]]] = []
    for value in sources:
        source = Path(value).expanduser().resolve()
        planned.append((source, _promotion_files(source)))
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "sources": [str(source) for source, _files in planned],
            "file_count": sum(len(files) for _source, files in planned),
            "size_bytes": sum(
                child.stat().st_size for _source, files in planned for child in files
            ),
        }

    records: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    for source, files in planned:
        destination = allocate_artifact_path(category, source.name, root=root)
        if source.is_file():
            _write_private(destination, source.read_bytes())
            records.append(
                record_artifact(
                    destination,
                    category=category,
                    artifact_type=artifact_type,
                    context=context,
                    source_path=source,
                    root=root,
                )
            )
            continue

        _private_directory(destination)
        for child in files:
            relative = child.relative_to(source)
            copied = destination / relative
            _write_private(copied, child.read_bytes())
            records.append(
                record_artifact(
                    copied,
                    category=category,
                    artifact_type=artifact_type,
                    context=context,
                    source_path=child,
                    root=root,
                )
            )
        source_sha, source_bytes, source_files = _tree_state(source)
        archive_sha, archive_bytes, archive_files = _tree_state(destination)
        if (source_sha, source_bytes, source_files) != (
            archive_sha,
            archive_bytes,
            archive_files,
        ):
            raise RetentionError(f"copied directory failed verification: {source}")
        container = {
            "schema": MANIFEST_SCHEMA,
            "record_kind": "directory",
            "artifact_id": hashlib.sha256(
                f"{destination.relative_to(durable).as_posix()}\0{archive_sha}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24],
            "artifact_path": destination.relative_to(durable).as_posix(),
            "artifact_type": str(artifact_type),
            "category": "/".join(_category_parts(category)),
            "context": str(context),
            "file_count": archive_files,
            "recorded_at": datetime.now(UTC).isoformat(),
            "retention_class": "durable-private",
            "sha256": archive_sha,
            "size_bytes": archive_bytes,
            "source_path": str(source),
            "source_sha256": source_sha,
        }
        _append_manifest(durable, container)
        containers.append(container)
    return {
        "ok": True,
        "dry_run": False,
        "history_root": str(durable),
        "sources": [str(source) for source, _files in planned],
        "artifact_count": len(records),
        "directory_count": len(containers),
        "size_bytes": sum(record["size_bytes"] for record in records),
    }


def _manifest_records(root: str | Path | None = None) -> list[dict[str, Any]]:
    manifest = history_root(root) / "manifest.jsonl"
    if not manifest.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def source_is_recorded(
    source_path: str | Path,
    *,
    root: str | Path | None = None,
) -> bool:
    source = Path(source_path).expanduser().resolve()
    if not source.exists() or source.is_symlink():
        return False
    if source.is_file():
        digest = _sha256_file(source)
    elif source.is_dir():
        digest, _size, _count = _tree_state(source)
    else:
        return False
    return any(
        record.get("source_path") == str(source)
        and record.get("source_sha256") == digest
        for record in reversed(_manifest_records(root))
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("manifest")
    path_parser = subparsers.add_parser("path")
    path_parser.add_argument("--category", required=True)
    path_parser.add_argument("--name", required=True)
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--category", required=True)
    record_parser.add_argument("--artifact-type", required=True)
    record_parser.add_argument("--context", default="")
    record_parser.add_argument("paths", nargs="+")
    write_parser = subparsers.add_parser("write-json")
    write_parser.add_argument("--category", required=True)
    write_parser.add_argument("--name", required=True)
    write_parser.add_argument("--artifact-type", required=True)
    write_parser.add_argument("--context", default="")
    write_parser.add_argument(
        "--input",
        default="-",
        help="JSON file to retain, or - for stdin.",
    )
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--category", required=True)
    promote_parser.add_argument("--artifact-type", required=True)
    promote_parser.add_argument("--context", default="")
    promote_parser.add_argument("--dry-run", action="store_true")
    promote_parser.add_argument("sources", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            print(json.dumps({"ok": True, "history_root": str(initialize())}, indent=2))
            return 0
        if args.command == "manifest":
            records = _manifest_records()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "history_root": str(history_root()),
                        "manifest": str(history_root() / "manifest.jsonl"),
                        "record_count": len(records),
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "path":
            print(str(allocate_artifact_path(args.category, args.name)))
            return 0
        if args.command == "record":
            print(
                json.dumps(
                    record_existing(
                        args.paths,
                        category=args.category,
                        artifact_type=args.artifact_type,
                        context=args.context,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "write-json":
            if args.input == "-":
                payload = json.load(sys.stdin)
            else:
                payload = json.loads(
                    Path(args.input).expanduser().read_text(encoding="utf-8")
                )
            path, record = write_json_artifact(
                args.category,
                args.name,
                payload,
                artifact_type=args.artifact_type,
                context=args.context,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "path": str(path),
                        "artifact_id": record["artifact_id"],
                        "sha256": record["sha256"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        print(
            json.dumps(
                promote(
                    args.sources,
                    category=args.category,
                    artifact_type=args.artifact_type,
                    context=args.context,
                    dry_run=args.dry_run,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RetentionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
