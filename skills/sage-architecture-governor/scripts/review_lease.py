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
import stat
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
REVIEW_LANES = ("runtime_bugfix", "sealed_evaluation")
GATE_CONTRACT_FIELDS = (
    "schema",
    "review_lane",
    "isa_path",
    "isa_gate",
    "isa_criteria",
    "review_domain",
    "mechanism_id",
    "design_epoch",
    "exact_production_path",
    "governed_paths",
    "operator_approval_scope",
)
MANIFEST_SCHEMA = "sage-artifact-manifest-v1"
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
    history_override = os.environ.get("SAGE_HISTORY_ROOT")
    use_canonical_history = (
        payload.get("review_stage") == "source_candidate"
        and payload.get("review_lane") == "sealed_evaluation"
    )
    if use_canonical_history:
        _canonical_history_root(root)
        os.environ.pop("SAGE_HISTORY_ROOT", None)
    try:
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
    finally:
        if use_canonical_history and history_override is not None:
            os.environ["SAGE_HISTORY_ROOT"] = history_override
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


def _canonical_history_root(root: Path) -> Path:
    """Return the literal repository history root without following symlinks."""

    history = root / ".sage_history"
    _require_real_path(history, root, expect_directory=True)
    return history


def _require_real_path(
    path: Path,
    base: Path,
    *,
    expect_directory: bool = False,
) -> None:
    """Require every component below base to exist without a symlink."""

    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path is outside {base}: {path}") from exc
    current = base
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError(f"required history path is missing: {current}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlinked history path is not allowed: {current}")
        if current != path and not stat.S_ISDIR(mode):
            raise ValueError(f"history path component is not a directory: {current}")
    final_mode = path.lstat().st_mode
    expected = (
        stat.S_ISDIR(final_mode)
        if expect_directory
        else stat.S_ISREG(final_mode)
    )
    if not expected:
        kind = "directory" if expect_directory else "regular file"
        raise ValueError(f"history path must be a {kind}: {path}")


def _history_artifact_path(root: Path, value: str | Path) -> tuple[Path, Path]:
    history = _canonical_history_root(root)
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        supplied = root / supplied
    artifact = Path(os.path.abspath(supplied))
    _require_real_path(artifact, history)
    return history, artifact


def _manifest_records(history: Path) -> list[dict[str, Any]]:
    manifest = history / "manifest.jsonl"
    _require_real_path(manifest, history)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), start=1
    ):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"malformed canonical manifest line {line_number}"
            ) from exc
        if not isinstance(record, dict) or record.get("schema") != MANIFEST_SCHEMA:
            raise ValueError(f"malformed canonical manifest line {line_number}")
        records.append(record)
    return records


def _load_manifest_artifact(root: Path, value: str | Path) -> dict[str, Any]:
    history, artifact = _history_artifact_path(root, value)
    relative = artifact.relative_to(history).as_posix()
    records = [
        row
        for row in _manifest_records(history)
        if row.get("record_kind") == "artifact"
        and row.get("artifact_path") == relative
    ]
    if not records:
        raise ValueError(f"history artifact is orphaned from manifest: {relative}")
    digest = _sha256(artifact.read_bytes())
    recorded_hashes = {str(row.get("sha256", "")) for row in records}
    if recorded_hashes != {digest}:
        raise ValueError(f"history artifact hash is stale or contradictory: {relative}")
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"history artifact is malformed JSON: {relative}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"history artifact must contain a JSON object: {relative}")
    return {
        "path": artifact,
        "repo_path": artifact.relative_to(root).as_posix(),
        "history_path": relative,
        "sha256": digest,
        "manifest_record": records[0],
        "payload": payload,
    }


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _process_guard_key(
    canonical_isa_path: str,
    isa_gate: str,
    isa_criteria: list[str],
    review_domain: str,
) -> str:
    identity = {
        "canonical_isa_path": canonical_isa_path,
        "isa_gate": isa_gate,
        "isa_criteria": sorted(isa_criteria),
        "review_domain": review_domain,
    }
    return f"sha256:{_canonical_sha256(identity)}"


def _load_gate_contract(root: Path, value: str | Path) -> dict[str, Any]:
    artifact = _load_manifest_artifact(root, value)
    payload = artifact["payload"]
    missing = [field for field in GATE_CONTRACT_FIELDS if field not in payload]
    if missing:
        raise ValueError("gate contract is missing fields: " + ", ".join(missing))
    _required_text(payload, "schema")
    lane = _required_text(payload, "review_lane")
    if lane not in REVIEW_LANES:
        raise ValueError(f"unsupported review_lane: {lane}")
    review_domain = _required_text(payload, "review_domain")
    if review_domain not in REVIEW_DOMAINS:
        raise ValueError(f"unsupported review_domain: {review_domain}")
    epoch = payload.get("design_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ValueError("design_epoch must be a positive integer")
    raw_criteria = payload.get("isa_criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise ValueError("isa_criteria must be a nonempty list")
    if any(not isinstance(item, str) or not item.strip() for item in raw_criteria):
        raise ValueError("isa_criteria entries must be nonempty strings")
    criteria = sorted({item.strip() for item in raw_criteria})
    if len(criteria) != len(raw_criteria):
        raise ValueError("isa_criteria must not contain duplicates")
    isa_path = Path(_required_text(payload, "isa_path")).expanduser()
    try:
        canonical_isa = isa_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"isa_path does not resolve to an existing file: {isa_path}"
        ) from exc
    if not canonical_isa.is_file():
        raise ValueError(f"isa_path is not a regular file: {canonical_isa}")
    governed_paths = _normalize_paths(root, list(payload.get("governed_paths") or []))
    isa_gate = _required_text(payload, "isa_gate")
    result = {
        **artifact,
        "schema": payload["schema"],
        "review_lane": lane,
        "canonical_isa_path": str(canonical_isa),
        "isa_gate": isa_gate,
        "isa_criteria": criteria,
        "review_domain": review_domain,
        "mechanism_id": _required_text(payload, "mechanism_id"),
        "design_epoch": epoch,
        "exact_production_path": _required_text(payload, "exact_production_path"),
        "governed_paths": governed_paths,
        "operator_approval_scope": _required_text(
            payload, "operator_approval_scope"
        ),
    }
    result["process_guard_key"] = _process_guard_key(
        result["canonical_isa_path"],
        result["isa_gate"],
        result["isa_criteria"],
        result["review_domain"],
    )
    return result


def _validate_reviewer_route(payload: dict[str, Any]) -> None:
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, dict):
        raise ValueError("reviewer route attestation is missing")
    if reviewer.get("profile_name") != "sage_eval_reviewer":
        raise ValueError("reviewer profile must be sage_eval_reviewer")
    for field in ("model", "effort", "profile_config_sha256"):
        _required_text(reviewer, field)
    if reviewer.get("route_verified") is not True:
        raise ValueError("reviewer route must be verified")
    if reviewer.get("authority") != "read_only":
        raise ValueError("reviewer authority must be read_only")


def _validate_common_receipt(
    receipt: dict[str, Any],
    gate: dict[str, Any],
    disposition: str,
) -> None:
    payload = receipt["payload"]
    if payload.get("disposition") != disposition:
        raise ValueError(f"reviewer result must contain literal {disposition}")
    _validate_reviewer_route(payload)
    exact = {
        "gate_contract_path": gate["repo_path"],
        "gate_contract_sha256": gate["sha256"],
        "operator_approval_scope": gate["operator_approval_scope"],
        "process_guard_key": gate["process_guard_key"],
        "design_epoch": gate["design_epoch"],
        "exact_production_path": gate["exact_production_path"],
        "governed_paths": gate["governed_paths"],
    }
    for field, expected in exact.items():
        if payload.get(field) != expected:
            raise ValueError(f"reviewer result has mismatched {field}")
    gate_time = str(gate["manifest_record"].get("recorded_at", ""))
    receipt_time = str(receipt["manifest_record"].get("recorded_at", ""))
    try:
        gate_moment = datetime.fromisoformat(gate_time.replace("Z", "+00:00"))
        receipt_moment = datetime.fromisoformat(
            receipt_time.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("review artifacts have malformed recorded_at values") from exc
    if receipt_moment <= gate_moment:
        raise ValueError("reviewer result is not fresh relative to gate contract")


def _load_design_receipt(
    root: Path,
    value: str | Path,
    gate: dict[str, Any],
) -> dict[str, Any]:
    receipt = _load_manifest_artifact(root, value)
    _validate_common_receipt(receipt, gate, "DESIGN_ACCEPT")
    return receipt


def _load_recovery_receipt(
    root: Path,
    value: str | Path,
    gate: dict[str, Any],
    rejected_candidate_id: str,
) -> dict[str, Any]:
    receipt = _load_manifest_artifact(root, value)
    _validate_common_receipt(receipt, gate, "RECOVERY_ACCEPT")
    payload = receipt["payload"]
    if payload.get("rejected_candidate_id") != rejected_candidate_id:
        raise ValueError("recovery result binds the wrong rejected candidate")
    classes = payload.get("root_cause_classes")
    if not isinstance(classes, list) or not classes or any(
        not isinstance(item, str) or not item.strip() for item in classes
    ):
        raise ValueError("recovery result requires nonempty root_cause_classes")
    _required_text(payload, "simplification")
    _required_text(payload, "named_falsifier")
    probes = payload.get("property_probe_evidence")
    if not isinstance(probes, list) or not probes:
        raise ValueError("recovery result requires property_probe_evidence")
    for probe in probes:
        if not isinstance(probe, dict):
            raise ValueError("property probe evidence must contain objects")
        _required_text(probe, "command")
        _required_text(probe, "output")
        if probe.get("exit_code") != 0 or probe.get("reviewer_reproduced") is not True:
            raise ValueError("property probe was not reproduced successfully")
    return receipt


def _receipt_identity(payload: dict[str, Any]) -> str:
    criteria = payload.get("isa_criteria")
    if not isinstance(criteria, list) or not criteria or any(
        not isinstance(item, str) or not item.strip() for item in criteria
    ):
        raise ValueError("counted receipt has malformed isa_criteria")
    return _process_guard_key(
        _required_text(payload, "canonical_isa_path"),
        _required_text(payload, "isa_gate"),
        criteria,
        _required_text(payload, "review_domain"),
    )


def _closed_process_receipts(
    root: Path, gate: dict[str, Any]
) -> list[dict[str, Any]]:
    history = _canonical_history_root(root)
    symlink = next((path for path in history.rglob("*") if path.is_symlink()), None)
    if symlink is not None:
        raise ValueError(f"symlinked canonical history entry is not allowed: {symlink}")
    relevant_paths: set[Path] = set()
    for path in history.rglob("*.closed.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed closed review receipt: {path}") from exc
        if (
            isinstance(raw, dict)
            and raw.get("process_guard_key") == gate["process_guard_key"]
        ):
            relevant_paths.add(path)
    receipts: list[dict[str, Any]] = []
    candidates: dict[str, dict[str, Any]] = {}
    leases: dict[str, str] = {}
    for path in sorted(relevant_paths):
        artifact = _load_manifest_artifact(root, path)
        payload = artifact["payload"]
        if payload.get("review_stage") != "source_candidate":
            raise ValueError("counted receipt has contradictory review_stage")
        if payload.get("review_lane") != "sealed_evaluation":
            raise ValueError("counted receipt has contradictory review_lane")
        if _receipt_identity(payload) != gate["process_guard_key"]:
            raise ValueError("counted receipt has contradictory process identity")
        epoch = payload.get("design_epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("counted receipt has malformed design_epoch")
        disposition = payload.get("close_disposition")
        if disposition not in CLOSE_DISPOSITIONS:
            raise ValueError("counted receipt has malformed disposition")
        review_round = payload.get("review_round")
        if (
            isinstance(review_round, bool)
            or not isinstance(review_round, int)
            or review_round not in (1, 2)
        ):
            raise ValueError("counted receipt has malformed review_round")
        for prefix in ("gate_contract", "design_receipt"):
            referenced = _load_manifest_artifact(
                root, _required_text(payload, f"{prefix}_path")
            )
            if referenced["sha256"] != payload.get(f"{prefix}_sha256"):
                raise ValueError(f"counted receipt has mismatched {prefix}_sha256")
        if review_round == 2:
            recovery = _load_manifest_artifact(
                root, _required_text(payload, "recovery_receipt_path")
            )
            if recovery["sha256"] != payload.get("recovery_receipt_sha256"):
                raise ValueError(
                    "counted receipt has mismatched recovery_receipt_sha256"
                )
        candidate_id = _required_text(payload, "candidate_id")
        if candidate_id in candidates:
            raise ValueError(
                f"duplicate or contradictory candidate receipt: {candidate_id}"
            )
        lease_id = _required_text(payload, "lease_id")
        if lease_id in leases and leases[lease_id] != candidate_id:
            raise ValueError(f"contradictory lease receipts: {lease_id}")
        candidates[candidate_id] = payload
        leases[lease_id] = candidate_id
        receipts.append(artifact)
    return receipts


def _sealed_review_state(
    root: Path, gate: dict[str, Any], design_receipt: dict[str, Any]
) -> tuple[int, str | None]:
    receipts = _closed_process_receipts(root, gate)
    epochs = [row["payload"]["design_epoch"] for row in receipts]
    current_epoch = gate["design_epoch"]
    if epochs and max(epochs) > current_epoch:
        raise ValueError("design_epoch cannot move backward behind existing history")
    current = [
        row
        for row in receipts
        if row["payload"]["design_epoch"] == current_epoch
    ]
    rejected = [
        row for row in current if row["payload"]["close_disposition"] == "rejected"
    ]
    accepted = [
        row for row in current if row["payload"]["close_disposition"] == "accepted"
    ]
    if len(accepted) > 1:
        raise ValueError("design epoch has contradictory accepted receipts")
    rejected_rounds = sorted(row["payload"]["review_round"] for row in rejected)
    if rejected_rounds != list(range(1, len(rejected_rounds) + 1)):
        raise ValueError("design epoch has contradictory rejected review rounds")
    if accepted and accepted[0]["payload"]["review_round"] != len(rejected) + 1:
        raise ValueError("design epoch has contradictory accepted review round")
    if accepted:
        raise ValueError("design epoch already has an accepted candidate")
    if len(rejected) >= 2:
        raise ValueError("design epoch is exhausted after two rejected candidates")
    if current_epoch > 1:
        prior_epoch = current_epoch - 1
        prior = [
            row
            for row in receipts
            if row["payload"]["design_epoch"] == prior_epoch
        ]
        prior_rejected = [
            row for row in prior if row["payload"]["close_disposition"] == "rejected"
        ]
        prior_accepted = [
            row for row in prior if row["payload"]["close_disposition"] == "accepted"
        ]
        prior_rejected_rounds = sorted(
            row["payload"]["review_round"] for row in prior_rejected
        )
        if prior_accepted or prior_rejected_rounds != [1, 2]:
            raise ValueError(
                "higher design_epoch is not chained to an exhausted predecessor"
            )
        supersedes = design_receipt["payload"].get("supersedes")
        expected = {
            "process_guard_key": gate["process_guard_key"],
            "design_epoch": prior_epoch,
            "operator_approval_scope": gate["operator_approval_scope"],
        }
        if supersedes != expected:
            raise ValueError("design receipt does not bind the exhausted predecessor")
    elif design_receipt["payload"].get("supersedes") not in (None, {}):
        raise ValueError("first design epoch must not claim a predecessor")
    rejected_id = rejected[0]["payload"]["candidate_id"] if rejected else None
    return len(rejected) + 1, rejected_id


def _source_contract(
    root: Path,
    args: argparse.Namespace,
    candidates: list[str],
) -> dict[str, Any]:
    if not getattr(args, "gate_contract", None):
        raise ValueError("source_candidate requires --gate-contract")
    gate = _load_gate_contract(root, args.gate_contract)
    if candidates != gate["governed_paths"]:
        raise ValueError(
            "candidate paths must exactly match gate-contract governed_paths"
        )
    result = {"gate": gate, "review_round": 1}
    if gate["review_lane"] == "runtime_bugfix":
        return result
    if not getattr(args, "design_receipt", None):
        raise ValueError("sealed_evaluation requires --design-receipt")
    design = _load_design_receipt(root, args.design_receipt, gate)
    review_round, rejected_id = _sealed_review_state(root, gate, design)
    recovery = None
    if review_round == 2:
        if not getattr(args, "recovery_receipt", None):
            raise ValueError("sealed round 2 requires --recovery-receipt")
        recovery = _load_recovery_receipt(
            root, args.recovery_receipt, gate, str(rejected_id)
        )
    result.update(
        {
            "review_round": review_round,
            "design_receipt": design,
            "recovery_receipt": recovery,
        }
    )
    return result


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
        source_contract = None
        if args.review_stage == "source_candidate":
            source_contract = _source_contract(root, args, candidates)
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
    identity: dict[str, Any] = {
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
    if source_contract is not None:
        gate = source_contract["gate"]
        identity.update(
            {
                "review_lane": gate["review_lane"],
                "review_domain": gate["review_domain"],
                "mechanism_id": gate["mechanism_id"],
                "review_round": source_contract["review_round"],
                "governing_gate": gate["isa_gate"],
                "canonical_isa_path": gate["canonical_isa_path"],
                "isa_gate": gate["isa_gate"],
                "isa_criteria": gate["isa_criteria"],
                "design_epoch": gate["design_epoch"],
                "exact_production_path": gate["exact_production_path"],
                "governed_paths": gate["governed_paths"],
                "operator_approval_scope": gate["operator_approval_scope"],
                "process_guard_key": gate["process_guard_key"],
                "gate_contract_path": gate["repo_path"],
                "gate_contract_sha256": gate["sha256"],
            }
        )
        design = source_contract.get("design_receipt")
        recovery = source_contract.get("recovery_receipt")
        if design is not None:
            identity.update(
                {
                    "design_receipt_path": design["repo_path"],
                    "design_receipt_sha256": design["sha256"],
                }
            )
        if recovery is not None:
            identity.update(
                {
                    "recovery_receipt_path": recovery["repo_path"],
                    "recovery_receipt_sha256": recovery["sha256"],
                }
            )
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
                "review_round": payload.get("review_round"),
                "review_lane": payload.get("review_lane"),
                "process_guard_key": payload.get("process_guard_key"),
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
        old_history_root = os.environ.get("SAGE_HISTORY_ROOT")
        try:
            os.environ["SAGE_ARCH_REVIEW_DIR"] = str(root / "leases")
            os.environ.pop("SAGE_HISTORY_ROOT", None)
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
            retention = _retention_module()
            gate_path, _record = retention.write_json_artifact(
                "contracts/governance",
                "review-lease-self-test-gate.json",
                {
                    "schema": "sage-architecture-gate-v1",
                    "review_lane": "runtime_bugfix",
                    "isa_path": str(source),
                    "isa_gate": "self-test",
                    "isa_criteria": ["SELF-TEST"],
                    "review_domain": "runtime_authority",
                    "mechanism_id": "self-test",
                    "design_epoch": 1,
                    "exact_production_path": "review_lease self-test",
                    "governed_paths": ["source.py"],
                    "operator_approval_scope": "self-test only",
                },
                artifact_type="architecture-gate-contract",
                root=root,
            )
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
                gate_contract=str(gate_path),
                design_receipt=None,
                recovery_receipt=None,
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
            if old_history_root is None:
                os.environ.pop("SAGE_HISTORY_ROOT", None)
            else:
                os.environ["SAGE_HISTORY_ROOT"] = old_history_root
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
    freeze_parser.add_argument("--gate-contract")
    freeze_parser.add_argument("--design-receipt")
    freeze_parser.add_argument("--recovery-receipt")
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
