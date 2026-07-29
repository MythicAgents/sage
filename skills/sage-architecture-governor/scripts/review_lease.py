#!/usr/bin/env python3
"""Freeze and verify exact staged bytes during a Sage review."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from typing import Any

from arch_governor_common import (
    REVIEW_LEASE_VERSION,
    normalize_repo_path,
    repo_hash,
    repo_root,
    review_lease_path,
    write_json,
)


REVIEW_STAGES = (
    "design_preflight",
    "source_candidate",
    "artifact_candidate",
    "live_readiness",
    "promotion_boundary",
)
REVIEW_DOMAINS = (
    "evaluation_lifecycle",
    "conversation_behavior",
    "runtime_authority",
    "provenance",
)
INDEPENDENCE_CLASSES = (
    "internal_subagent",
    "independent_top_level_session",
    "human_external",
)
CLOSE_DISPOSITIONS = ("accepted", "rejected", "invalidated", "abandoned")
ARTIFACT_RETENTION_PATH = (
    Path(__file__).resolve().parents[2]
    / "sage-artifact-retention"
    / "scripts"
    / "artifact_retention.py"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retention_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "sage_artifact_retention_for_review_lease",
        ARTIFACT_RETENTION_PATH,
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load Sage artifact-retention helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive_closed_review(
    root: Path,
    lease_path: Path,
    payload: dict[str, Any],
) -> Path:
    candidate = str(payload.get("candidate_id", "unknown")).split(":")[-1][:16]
    filename = f"{lease_path.stem}-{candidate}.closed.json"
    retention = _retention_module()
    archive, _record = retention.write_json_artifact(
        "governance/architecture-reviews",
        filename,
        payload,
        artifact_type="closed-architecture-review",
        context=(
            f"review lease {payload.get('lease_id')} closed "
            f"{payload.get('close_disposition')}"
        ),
        root=root,
    )
    return archive


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(error or f"git {' '.join(args)} failed")
    return result.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(
        json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )


def _path_state(root: Path, path: str) -> dict[str, Any]:
    try:
        index_bytes = _git(root, "show", f":{path}")
    except ValueError as exc:
        raise ValueError(f"{path}: path is not present in the git index") from exc
    worktree_path = root / path
    if not worktree_path.is_file():
        raise ValueError(f"{path}: worktree file is missing or not a regular file")
    status = _git(
        root, "status", "--porcelain=v1", "--untracked-files=all", "--", path
    ).decode("utf-8", errors="replace").strip()
    return {
        "path": path,
        "index_sha256": _sha256(index_bytes),
        "worktree_sha256": _sha256(worktree_path.read_bytes()),
        "status": status,
    }


def _normalize_paths(root: Path, paths: list[str]) -> list[str]:
    normalized = sorted(
        {
            normalize_repo_path(path, root)
            for path in paths
            if normalize_repo_path(path, root)
        }
    )
    if not normalized:
        raise ValueError("at least one candidate path is required")
    return normalized


def _staged_paths(root: Path) -> set[str]:
    output = _git(root, "diff", "--cached", "--name-only", "-z")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    }


def _index_sha256(root: Path) -> str:
    """Fingerprint every path and blob currently represented by the index."""

    return _sha256(_git(root, "ls-files", "--stage", "-z"))


def _snapshot(
    root: Path, candidate_paths: list[str], protected_paths: list[str]
) -> dict[str, Any]:
    candidates = [_path_state(root, path) for path in candidate_paths]
    protected = [_path_state(root, path) for path in protected_paths]
    staged = _staged_paths(root)
    missing_staged = [path for path in candidate_paths if path not in staged]
    if missing_staged:
        raise ValueError(
            "candidate paths must be staged before freeze: "
            + ", ".join(missing_staged)
        )
    split_bytes = [
        row["path"]
        for row in candidates
        if row["index_sha256"] != row["worktree_sha256"]
    ]
    if split_bytes:
        raise ValueError(
            "candidate paths have unstaged bytes after the staged candidate: "
            + ", ".join(split_bytes)
        )
    status_rows = [
        {"path": row["path"], "status": row["status"]}
        for row in [*candidates, *protected]
    ]
    return {
        "head": _git(root, "rev-parse", "HEAD").decode().strip(),
        "index_sha256": _index_sha256(root),
        "candidate_files": candidates,
        "protected_files": protected,
        "scoped_status_digest": _canonical_sha256(status_rows),
    }


def freeze(args: argparse.Namespace) -> int:
    root = repo_root(args.repo)
    lease_path = review_lease_path(root)
    if args.review_round < 1:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "freeze_refused",
                    "reason": "review_round must be at least 1",
                },
                indent=2,
            )
        )
        return 2
    if lease_path.exists():
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "active_review_lease_exists",
                    "lease": str(lease_path),
                },
                indent=2,
            )
        )
        return 2
    try:
        candidates = _normalize_paths(root, args.paths)
        protected = sorted(
            {
                normalize_repo_path(path, root)
                for path in args.protected
                if normalize_repo_path(path, root)
            }
        )
        overlap = sorted(set(candidates) & set(protected))
        if overlap:
            raise ValueError(
                "candidate and protected paths overlap: " + ", ".join(overlap)
            )
        snapshot = _snapshot(root, candidates, protected)
    except ValueError as exc:
        print(
            json.dumps(
                {"ok": False, "status": "freeze_refused", "reason": str(exc)},
                indent=2,
            )
        )
        return 2
    identity = {
        **snapshot,
        "candidate_paths": candidates,
        "protected_paths": protected,
        "review_stage": args.review_stage,
        "review_domain": args.review_domain,
        "independence_class": args.independence_class,
        "mechanism_id": args.mechanism_id,
        "review_round": args.review_round,
        "governing_gate": args.governing_gate,
    }
    payload = {
        "version": REVIEW_LEASE_VERSION,
        "status": "active",
        "repo_root": str(root),
        "repo_hash": repo_hash(root),
        "lease_id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "candidate_id": f"sha256:{_canonical_sha256(identity)}",
        **identity,
    }
    write_json(lease_path, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "status": "frozen",
                "lease": str(lease_path),
                "lease_id": payload["lease_id"],
                "candidate_id": payload["candidate_id"],
                "candidate_paths": candidates,
                "protected_paths": protected,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _verify_payload(root: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    drift: list[dict[str, Any]] = []
    if payload.get("version") != REVIEW_LEASE_VERSION:
        drift.append({"field": "version", "expected": REVIEW_LEASE_VERSION})
    if payload.get("repo_hash") != repo_hash(root):
        drift.append(
            {
                "field": "repo_hash",
                "expected": payload.get("repo_hash"),
                "actual": repo_hash(root),
            }
        )
    try:
        current = _snapshot(
            root,
            list(payload.get("candidate_paths") or []),
            list(payload.get("protected_paths") or []),
        )
    except ValueError as exc:
        return [{"field": "snapshot", "reason": str(exc)}]
    for field in (
        "head",
        "index_sha256",
        "candidate_files",
        "protected_files",
        "scoped_status_digest",
    ):
        if current.get(field) != payload.get(field):
            drift.append(
                {
                    "field": field,
                    "expected": payload.get(field),
                    "actual": current.get(field),
                }
            )
    return drift


def verify(args: argparse.Namespace) -> int:
    root = repo_root(args.repo)
    lease_path = review_lease_path(root)
    try:
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
    except Exception:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "missing_review_lease",
                    "lease": str(lease_path),
                },
                indent=2,
            )
        )
        return 2
    drift = _verify_payload(root, payload)
    status = "verified_frozen_candidate" if not drift else "invalidated_candidate_drift"
    print(
        json.dumps(
            {
                "ok": not drift,
                "status": status,
                "lease": str(lease_path),
                "lease_id": payload.get("lease_id"),
                "candidate_id": payload.get("candidate_id"),
                "drift": drift,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not drift else 3


def close(args: argparse.Namespace) -> int:
    root = repo_root(args.repo)
    lease_path = review_lease_path(root)
    try:
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
    except Exception:
        print(
            json.dumps(
                {"ok": False, "status": "missing_review_lease"}, indent=2
            )
        )
        return 2
    if args.lease_id != payload.get("lease_id"):
        print(
            json.dumps(
                {"ok": False, "status": "lease_id_mismatch"}, indent=2
            )
        )
        return 2
    payload.update(
        {
            "status": "closed",
            "closed_at": _now_iso(),
            "close_disposition": args.disposition,
        }
    )
    try:
        archive = _archive_closed_review(root, lease_path, payload)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "closed_review_archive_failed",
                    "error": str(exc),
                },
                indent=2,
            )
        )
        return 2
    lease_path.unlink()
    print(
        json.dumps(
            {
                "ok": True,
                "status": "closed",
                "archive": str(archive),
                "candidate_id": payload.get("candidate_id"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_cwd = Path.cwd()
        old_review_dir = os.environ.get("SAGE_ARCH_REVIEW_DIR")
        try:
            os.environ["SAGE_ARCH_REVIEW_DIR"] = str(root / "leases")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "self-test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Self Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=root,
                check=True,
            )
            source = root / "source.py"
            source.write_text("old = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "base"], cwd=root, check=True
            )
            source.write_text("old = False\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
            os.chdir(root)
            freeze_args = argparse.Namespace(
                repo=str(root),
                paths=["source.py"],
                protected=[],
                review_stage="source_candidate",
                review_domain="runtime_authority",
                independence_class="internal_subagent",
                mechanism_id="self-test",
                review_round=1,
                governing_gate="self-test",
            )
            if freeze(freeze_args) != 0 or verify(
                argparse.Namespace(repo=str(root))
            ) != 0:
                return 1
            lease = json.loads(
                review_lease_path(root).read_text(encoding="utf-8")
            )
            source.write_text("drift = True\n", encoding="utf-8")
            if verify(argparse.Namespace(repo=str(root))) != 3:
                return 1
            if (
                close(
                    argparse.Namespace(
                        repo=str(root),
                        lease_id=lease["lease_id"],
                        disposition="invalidated",
                    )
                )
                != 0
            ):
                return 1
        finally:
            os.chdir(old_cwd)
            if old_review_dir is None:
                os.environ.pop("SAGE_ARCH_REVIEW_DIR", None)
            else:
                os.environ["SAGE_ARCH_REVIEW_DIR"] = old_review_dir
    print("review_lease self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--repo")
    freeze_parser.add_argument("--paths", action="append", required=True)
    freeze_parser.add_argument("--protected", action="append", default=[])
    freeze_parser.add_argument(
        "--review-stage", choices=REVIEW_STAGES, required=True
    )
    freeze_parser.add_argument(
        "--review-domain", choices=REVIEW_DOMAINS, required=True
    )
    freeze_parser.add_argument(
        "--independence-class",
        choices=INDEPENDENCE_CLASSES,
        required=True,
    )
    freeze_parser.add_argument("--mechanism-id", required=True)
    freeze_parser.add_argument("--review-round", type=int, required=True)
    freeze_parser.add_argument("--governing-gate", required=True)
    freeze_parser.set_defaults(func=freeze)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--repo")
    verify_parser.set_defaults(func=verify)

    close_parser = subparsers.add_parser("close")
    close_parser.add_argument("--repo")
    close_parser.add_argument("--lease-id", required=True)
    close_parser.add_argument(
        "--disposition", choices=CLOSE_DISPOSITIONS, required=True
    )
    close_parser.set_defaults(func=close)

    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
