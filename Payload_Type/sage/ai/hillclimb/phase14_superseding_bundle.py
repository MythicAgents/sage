"""Phase 14 superseding retrospective evidence bundle.

This module is offline-only. It preserves the original Phase 10 bundle as a
historical artifact, rebuilds the retrospective package with Git-object-derived
disposition and unique-attempt accounting, and emits no promotion decision of
its own. The companion ``phase14_bundle_validator`` module validates the output
without importing this generator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence

try:  # package import
    from .experiment_contracts import (
        POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS,
        POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
        POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS,
        SCOPE_GOVERNANCE_NOT_EVALUATED,
        content_hash,
        file_sha256,
    )
    from . import phase10_evidence_bundle as phase10
    from .phase12_proof_binding_audit import historical_authorization_provenance
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from experiment_contracts import (  # type: ignore
        POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS,
        POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
        POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS,
        SCOPE_GOVERNANCE_NOT_EVALUATED,
        content_hash,
        file_sha256,
    )
    import phase10_evidence_bundle as phase10  # type: ignore
    from phase12_proof_binding_audit import historical_authorization_provenance  # type: ignore


KIND = "phase14_superseding_retrospective_evidence_bundle"
SCHEMA_VERSION = 1
LOGICAL_ATTEMPT_KEY_VERSION = "logical-attempt-key-v1"
LINEAGE_SUMMARY_VERSION = "recursive-lineage-summary-v1"
PORTABLE_HASH_STATUS = "portable_recomputable"
SOURCE_COMMITMENT_ONLY_STATUS = "source_commitment_only_due_to_redaction"
EXTERNAL_AUTHENTICITY_STATUS = "unverified_pending_phase22_independent_reproduction"

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
DEFAULT_ORIGINAL_BUNDLE_PATH = DEFAULT_RESULTS_ROOT / "phase10_portable_evidence_bundle_20260716.json"
DEFAULT_PHASE12_AUDIT_PATH = (
    DEFAULT_REPO_ROOT / "Plans" / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE12_PROOF_BINDING_AUDIT_2026-07-16.json"
)
DEFAULT_PHASE13_STATUS_PATH = (
    DEFAULT_REPO_ROOT / "Plans" / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE13_CANONICAL_PROMOTION_STATUS_2026-07-16.json"
)
DEFAULT_OUTPUT_PATH = DEFAULT_RESULTS_ROOT / "phase14_superseding_retrospective_bundle_v1_20260716.json"

POLICY_PATH = "Payload_Type/sage/ai/langgraph/policy.py"
PHASE10_GENERATOR_PATH = "Payload_Type/sage/ai/hillclimb/phase10_evidence_bundle.py"
PHASE9_GENERATOR_PATH = "Payload_Type/sage/ai/hillclimb/phase9_auto_harness_readiness.py"

_POLICY_DEFAULT_HYBRID_RE = re.compile(r"(?m)^POLICY_DEFAULT\s*=\s*POLICY_HYBRID\s*$")
_OUTPUT_OR_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|pwd|secret|pfx_base64|raw_output|raw_result|task_output|result)(?:$|_)"
)
_PROVIDER_OR_INFRA_RE = re.compile(
    r"provider|bedrock|timeout|timed out|reset|collection retry budget|infrastructure|connection|transport",
    re.IGNORECASE,
)

# These are retained-history dispositions, not pass/fail booleans. They encode the
# explicit burn records in the Phase 6/7 contracts so stale rows cannot silently
# become unique evidentiary samples.
_BURNED_ARTIFACT_REASONS = {
    "laps_family_transfer_mechanics_canaries_20260714.jsonl": "phase6_original_holdout_burned",
    "laps_family_transfer_mechanics_canaries_r1_20260714.jsonl": "phase6_r1_burned",
    "laps_family_transfer_mechanics_canaries_r2_20260714.jsonl": "phase6_r2_burned",
    "laps_family_transfer_mechanics_canaries_r3_20260714.jsonl": "phase6_r3_burned",
    "laps_family_transfer_mechanics_canaries_pinned_r3_20260714.jsonl": "phase6_r3_burned",
    "laps_family_transfer_forced_confirmations_r3_20260714.jsonl": "phase6_r3_burned",
    "laps_family_transfer_mechanics_canaries_r4_20260715.jsonl": "phase6_r4_burned",
    "laps_family_transfer_mechanics_canaries_pinned_r4_20260715.jsonl": "phase6_r4_burned",
    "laps_family_transfer_forced_confirmations_r4_20260715.jsonl": "phase6_r4_burned",
    "laps_family_transfer_policy_matrix_r4_20260715.jsonl": "phase6_r4_burned",
    "trust_context_corroboration_positive_rows_20260715.jsonl": "phase7_v1_burned",
}


class Phase14BundleError(ValueError):
    """Raised when the superseding bundle inputs are missing or malformed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8", errors="replace"))


def _portable_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.name


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase14BundleError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase14BundleError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Phase14BundleError(f"{label} must be a JSON object")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Phase14BundleError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise Phase14BundleError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    return rows


def _load_artifact_payload(path: Path) -> tuple[str, Any]:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        return "application/json", _read_json(path, label="artifact")
    if suffix == ".jsonl":
        return "application/x-ndjson", _read_jsonl(path)
    return "text/markdown" if suffix == ".md" else "text/plain", path.read_text(encoding="utf-8")


def _json_pointer_token(value: str) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _changed_leaf_commitments(raw: Any, redacted: Any, *, pointer: str = "") -> list[dict[str, Any]]:
    if isinstance(raw, Mapping) and isinstance(redacted, Mapping):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(raw) | set(redacted), key=str):
            rows.extend(
                _changed_leaf_commitments(
                    raw.get(key),
                    redacted.get(key),
                    pointer=f"{pointer}/{_json_pointer_token(str(key))}",
                )
            )
        return rows
    if isinstance(raw, list) and isinstance(redacted, list):
        rows: list[dict[str, Any]] = []
        for index in range(max(len(raw), len(redacted))):
            rows.extend(
                _changed_leaf_commitments(
                    raw[index] if index < len(raw) else None,
                    redacted[index] if index < len(redacted) else None,
                    pointer=f"{pointer}/{index}",
                )
            )
        return rows
    if _canonical_json(raw) == _canonical_json(redacted):
        return []
    key = pointer.rsplit("/", 1)[-1] if pointer else ""
    return [
        {
            "json_pointer": pointer or "/",
            "source_value_sha256": content_hash(raw),
            "redacted_value_sha256": content_hash(redacted),
            "redacted_value_preview": redacted if isinstance(redacted, str) and redacted.startswith("<") else None,
            "source_type": type(raw).__name__,
            "commitment_role": (
                "canonical_raw_output_commitment"
                if _OUTPUT_OR_SECRET_KEY_RE.search(key.casefold())
                else "redacted_source_commitment"
            ),
        }
    ]


def _artifact_record(path: Path, *, root: Path, artifact_class: str | None = None) -> dict[str, Any]:
    media_type, raw_payload = _load_artifact_payload(path)
    redacted_payload = phase10._sanitize_value(raw_payload, root=root)  # noqa: SLF001 - shared redaction contract
    source_payload_sha256 = content_hash(raw_payload)
    embedded_payload_sha256 = content_hash(redacted_payload)
    changed = source_payload_sha256 != embedded_payload_sha256
    commitments = _changed_leaf_commitments(raw_payload, redacted_payload)
    return {
        "path": _portable_path(path, root),
        "artifact_class": artifact_class or phase10._classify_artifact(path, root=root),  # noqa: SLF001
        "media_type": media_type,
        "source_file_sha256": f"sha256:{file_sha256(path)}",
        "source_bytes": path.stat().st_size,
        "source_payload_sha256": source_payload_sha256,
        "embedded_payload_sha256": embedded_payload_sha256,
        "portable_hash_status": SOURCE_COMMITMENT_ONLY_STATUS if changed else PORTABLE_HASH_STATUS,
        "redaction_changed": changed,
        "omitted_sensitive_value_commitments": commitments,
        "payload": redacted_payload,
    }


def _discover_result_artifacts(root: Path) -> list[Path]:
    results_root = root / "Payload_Type" / "sage" / ".hillclimb" / "results"
    if not results_root.exists():
        return []
    return [
        path
        for path in sorted(results_root.iterdir())
        if path.is_file()
        and path.suffix.casefold() in {".json", ".jsonl"}
        and not path.name.startswith("phase10_portable_evidence_bundle_")
        and not path.name.startswith("phase14_superseding_retrospective_bundle_")
    ]


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def _git_output(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_success(root: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _git_lines(root: Path, *args: str) -> list[str]:
    output = _git_output(root, *args)
    return [line for line in output.splitlines() if line.strip()]


def _git_object_exists(root: Path, rev: str, path: str) -> bool:
    if not rev or not path:
        return False
    return _git_success(root, "cat-file", "-e", f"{rev}:{path}")


def _git_show_text(root: Path, rev: str, path: str) -> str:
    return _git_output(root, "show", f"{rev}:{path}")


def _git_blob_record(root: Path, rev: str, path: str) -> dict[str, Any]:
    exists = _git_object_exists(root, rev, path)
    text = _git_show_text(root, rev, path) if exists else ""
    return {
        "path": path,
        "rev": rev,
        "exists_in_git_object": exists,
        "git_blob_id": _git_output(root, "rev-parse", f"{rev}:{path}") if exists else "",
        "sha256": _sha256_text(text) if exists else "",
        "policy_default": "hybrid" if exists and _POLICY_DEFAULT_HYBRID_RE.search(text) else "not_hybrid_or_unavailable",
    }


def _git_disposition(root: Path, *, original_bundle: Mapping[str, Any]) -> dict[str, Any]:
    current_head = _git_output(root, "rev-parse", "HEAD")
    original_head = str(
        (((original_bundle.get("bundle_metadata") or {}).get("patch_stack") or {}).get("base_head")) or ""
    )
    introducing_commit = _git_output(
        root,
        "log",
        "--format=%H",
        "--reverse",
        "-S",
        "POLICY_DEFAULT = POLICY_HYBRID",
        "--",
        POLICY_PATH,
    ).splitlines()
    introducing_commit_id = introducing_commit[0].strip() if introducing_commit else ""
    current_policy = _git_blob_record(root, current_head, POLICY_PATH)
    original_policy = _git_blob_record(root, original_head, POLICY_PATH)
    return {
        "derived_from_git_objects": True,
        "current_head": current_head,
        "original_phase10_bundle_base_head": original_head,
        "original_phase10_base_head_exists": _git_success(root, "cat-file", "-e", f"{original_head}^{{commit}}"),
        "hybrid_default_introducing_commit": introducing_commit_id,
        "hybrid_default_introducing_commit_is_ancestor_of_original_bundle_base_head": (
            bool(introducing_commit_id)
            and _git_success(root, "merge-base", "--is-ancestor", introducing_commit_id, original_head)
        ),
        "hybrid_default_introducing_commit_is_ancestor_of_current_head": (
            bool(introducing_commit_id)
            and _git_success(root, "merge-base", "--is-ancestor", introducing_commit_id, current_head)
        ),
        "original_bundle_baseline_policy_blob": original_policy,
        "current_head_policy_blob": current_policy,
        "phase10_generator_blob_at_original_bundle_base_head": _git_blob_record(root, original_head, PHASE10_GENERATOR_PATH),
        "phase10_generator_blob_at_current_head": _git_blob_record(root, current_head, PHASE10_GENERATOR_PATH),
        "phase9_generator_blob_at_original_bundle_base_head": _git_blob_record(root, original_head, PHASE9_GENERATOR_PATH),
        "phase9_generator_blob_at_current_head": _git_blob_record(root, current_head, PHASE9_GENERATOR_PATH),
        "current_worktree_status_porcelain": _git_lines(root, "status", "--short"),
        "derived_disposition": {
            "hybrid_default_at_original_bundle_baseline": (
                "committed" if original_policy["policy_default"] == "hybrid" else "not_committed_or_unavailable"
            ),
            "hybrid_default_at_current_head": (
                "committed" if current_policy["policy_default"] == "hybrid" else "not_committed_or_unavailable"
            ),
            "phase10_generator_at_original_bundle_baseline": (
                "committed" if _git_blob_record(root, original_head, PHASE10_GENERATOR_PATH)["exists_in_git_object"] else "uncommitted_or_absent"
            ),
            "phase10_generator_at_current_head": (
                "committed" if _git_blob_record(root, current_head, PHASE10_GENERATOR_PATH)["exists_in_git_object"] else "uncommitted_or_absent"
            ),
        },
    }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decision_ids(row: Mapping[str, Any]) -> list[str]:
    return [
        _text(item.get("decision_id"))
        for item in list(row.get("decisions") or [])
        if isinstance(item, Mapping) and _text(item.get("decision_id"))
    ]


def _transaction_ids(row: Mapping[str, Any]) -> list[str]:
    return [
        _text(item.get("transaction_id"))
        for item in list(row.get("transactions") or [])
        if isinstance(item, Mapping) and _text(item.get("transaction_id"))
    ]


def logical_attempt_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    transaction_ids = _transaction_ids(row)
    decision_ids = _decision_ids(row)
    if transaction_ids or decision_ids:
        return {
            "identity_kind": "runtime_lineage",
            "scenario": _text(row.get("scenario")),
            "side": _text(row.get("side")),
            "policy_mode": _text(row.get("policy_mode") or row.get("configured_policy_mode")),
            "ts_iso": _text(row.get("ts_iso")),
            "chat_channel_id": row.get("chat_channel_id"),
            "chat_request_id": row.get("chat_request_id"),
            "transaction_ids": transaction_ids,
            "decision_ids": decision_ids,
        }
    if _text(row.get("run_id")):
        return {
            "identity_kind": "benchmark_run",
            "run_id": _text(row.get("run_id")),
            "created_at": _text(row.get("created_at")),
        }
    return {
        "identity_kind": "canonical_row_fallback",
        "source_row_sha256": content_hash(row),
    }


def logical_attempt_key(row: Mapping[str, Any]) -> str:
    return content_hash(logical_attempt_identity(row))


def _branch_surface_observed(row: Mapping[str, Any]) -> bool:
    for decision in list(row.get("decisions") or []):
        if not isinstance(decision, Mapping):
            continue
        selected = _text(decision.get("selected_capability")).casefold()
        if selected and selected != "collect-graph":
            return True
        for candidate in list(decision.get("admissible_frontier") or []):
            if isinstance(candidate, Mapping) and _text(candidate.get("name")).casefold() not in {"", "collect-graph"}:
                return True
    return False


def _artifact_burn_reason(path: str) -> str:
    return _BURNED_ARTIFACT_REASONS.get(Path(path).name, "")


def _row_is_diagnostic(row: Mapping[str, Any], *, source_paths: Sequence[str]) -> bool:
    if any("diagnostic" in Path(path).name.casefold() for path in source_paths):
        return True
    if not _transaction_ids(row) and not _decision_ids(row):
        return True
    return not _branch_surface_observed(row)


def _row_is_infrastructure_or_provider_failure(row: Mapping[str, Any]) -> bool:
    fields = [
        _text(row.get("status")),
        _text(row.get("controller_status")),
        _text(row.get("controller_terminal_reason")),
        _text((row.get("controller_blocker") or {}).get("reason") if isinstance(row.get("controller_blocker"), Mapping) else ""),
    ]
    return any(_PROVIDER_OR_INFRA_RE.search(field) for field in fields if field)


def _row_is_accepted(row: Mapping[str, Any]) -> bool:
    return (
        row.get("objective_proven") is True
        and row.get("clean_stop") is True
        and row.get("backend_provenance_complete") is True
        and row.get("policy_identity_valid") is True
        and _text(row.get("controller_status")).casefold() in {"", "complete"}
    )


def _primary_disposition(
    row: Mapping[str, Any],
    *,
    source_paths: Sequence[str],
) -> tuple[str, list[str], bool]:
    reason_codes: list[str] = []
    burn_reasons = sorted({_artifact_burn_reason(path) for path in source_paths if _artifact_burn_reason(path)})
    if burn_reasons:
        reason_codes.extend(burn_reasons)
        return "burned", reason_codes, _row_is_infrastructure_or_provider_failure(row)
    diagnostic = _row_is_diagnostic(row, source_paths=source_paths)
    infrastructure = _row_is_infrastructure_or_provider_failure(row)
    if diagnostic:
        reason_codes.append("missing_countable_branch_surface_or_lineage")
        if infrastructure:
            reason_codes.append("infrastructure_or_provider_failure_observed")
        return "diagnostic_only", reason_codes, infrastructure
    if infrastructure:
        reason_codes.append("infrastructure_or_provider_failure_observed")
        return "infrastructure_or_provider_failure", reason_codes, True
    if _row_is_accepted(row):
        reason_codes.append("retained_descriptive_row_passed_local_runtime_checks")
        return "accepted", reason_codes, False
    reason_codes.append("retained_row_failed_local_runtime_or_provenance_checks")
    return "failed", reason_codes, False


def _iter_result_jsonl_rows(
    records: Sequence[Mapping[str, Any]],
) -> Iterable[tuple[str, int, Mapping[str, Any]]]:
    for record in records:
        if (
            record.get("artifact_class") != "result_artifact"
            or record.get("media_type") != "application/x-ndjson"
        ):
            continue
        path = _text(record.get("path"))
        payload = record.get("payload")
        if not isinstance(payload, list):
            continue
        for row_index, row in enumerate(payload, start=1):
            if isinstance(row, Mapping):
                yield path, row_index, row


def _sample_accounting(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, row_index, row in _iter_result_jsonl_rows(records):
        key = logical_attempt_key(row)
        groups[key].append({
            "source_path": path,
            "source_row_index": row_index,
            "embedded_row_sha256": content_hash(row),
            "identity": logical_attempt_identity(row),
            "row": row,
        })
    inventory: list[dict[str, Any]] = []
    primary_counts: Counter[str] = Counter()
    infrastructure_count = 0
    duplicate_source_row_count = 0
    identity_collision_count = 0
    for key in sorted(groups):
        occurrences = sorted(
            groups[key],
            key=lambda item: (item["source_path"], int(item["source_row_index"])),
        )
        row_hashes = {item["embedded_row_sha256"] for item in occurrences}
        if len(row_hashes) > 1:
            identity_collision_count += 1
        canonical = occurrences[0]
        source_paths = [item["source_path"] for item in occurrences]
        disposition, reason_codes, infrastructure = _primary_disposition(
            canonical["row"],
            source_paths=source_paths,
        )
        duplicate_count = max(0, len(occurrences) - 1)
        duplicate_source_row_count += duplicate_count
        primary_counts[disposition] += 1
        infrastructure_count += int(infrastructure)
        inventory.append({
            "logical_attempt_key": key,
            "logical_attempt_identity": canonical["identity"],
            "canonical_occurrence": {
                "source_path": canonical["source_path"],
                "source_row_index": canonical["source_row_index"],
                "embedded_row_sha256": canonical["embedded_row_sha256"],
            },
            "source_occurrences": [
                {
                    "source_path": item["source_path"],
                    "source_row_index": item["source_row_index"],
                    "embedded_row_sha256": item["embedded_row_sha256"],
                }
                for item in occurrences
            ],
            "duplicate_source_row_count": duplicate_count,
            "identity_collision": len(row_hashes) > 1,
            "primary_disposition": disposition,
            "reason_codes": reason_codes,
            "infrastructure_or_provider_failure": infrastructure,
            "promotion_eligible": False,
            "promotion_ineligibility_reasons": [
                "historical_phase0_10_authorization_binding_unavailable",
                "canonical_promotion_requires_fresh_derived_outcome_and_exact_proof_fields",
            ],
        })
    source_row_count = sum(len(items) for items in groups.values())
    unique_count = len(inventory)
    promotion_eligible_count = sum(item["promotion_eligible"] is True for item in inventory)
    counts = {
        "source_row_count": source_row_count,
        "unique_logical_attempt_count": unique_count,
        "duplicate_source_row_count": duplicate_source_row_count,
        "primary_disposition_counts": dict(sorted(primary_counts.items())),
        "infrastructure_or_provider_failure_count": infrastructure_count,
        "promotion_eligible_count": promotion_eligible_count,
        "identity_collision_count": identity_collision_count,
    }
    counts["reconciliation"] = {
        "source_rows_equal_unique_plus_duplicates": source_row_count == unique_count + duplicate_source_row_count,
        "unique_attempts_equal_primary_disposition_partition": unique_count == sum(primary_counts.values()),
        "promotion_eligible_is_subset_of_accepted": promotion_eligible_count <= primary_counts.get("accepted", 0),
        "no_logical_attempt_identity_collisions": identity_collision_count == 0,
    }
    return {
        "logical_attempt_key_version": LOGICAL_ATTEMPT_KEY_VERSION,
        "count_basis": "unique logical attempts; duplicate rows are retained as source occurrences only",
        "historical_authorization_binding_rule": (
            "Historical Phase 0-10 rows are out-of-band and cannot be promotion-eligible without a retained "
            "prospective authorization binding."
        ),
        "counts": counts,
        "inventory": inventory,
    }


def _iter_nested_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _iter_nested_mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_mappings(item)


def _lineage_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    nested = [item for row in rows for item in _iter_nested_mappings(row)]
    task_ids: set[str] = set()
    proof_ids: set[str] = set()
    verifier_ids: set[str] = set()
    child_task_entry_count = 0
    proof_lineage_entry_count = 0
    for item in nested:
        if isinstance(item.get("child_tasks"), list):
            child_task_entry_count += sum(isinstance(child, Mapping) for child in item["child_tasks"])
        if isinstance(item.get("proof_lineage"), list):
            proof_lineage_entry_count += sum(isinstance(proof, Mapping) for proof in item["proof_lineage"])
        for key in ("task_id", "evidence_task_id", "proof_task_id"):
            if _text(item.get(key)):
                task_ids.add(_text(item.get(key)))
        for key in ("task_ids",):
            for value in list(item.get(key) or []):
                if _text(value):
                    task_ids.add(_text(value))
        for key in ("proof_envelope_id", "proof_id", "proof_envelope_ref"):
            if _text(item.get(key)):
                proof_ids.add(_text(item.get(key)))
        for key in ("proof_ids", "proof_envelope_ids"):
            for value in list(item.get(key) or []):
                if _text(value):
                    proof_ids.add(_text(value))
        for key in ("verifier_id",):
            if _text(item.get(key)):
                verifier_ids.add(_text(item.get(key)))
        for key in ("verifier_ids",):
            for value in list(item.get(key) or []):
                if _text(value):
                    verifier_ids.add(_text(value))
    return {
        "version": LINEAGE_SUMMARY_VERSION,
        "row_count": len(rows),
        "nested_mapping_count": len(nested),
        "nested_objects_with_child_tasks": sum(isinstance(item.get("child_tasks"), list) for item in nested),
        "nested_child_task_entry_count": child_task_entry_count,
        "nested_objects_with_task_id": sum(bool(_text(item.get("task_id"))) for item in nested),
        "nested_objects_with_task_ids": sum(bool(item.get("task_ids")) for item in nested),
        "nested_objects_with_proof_lineage": sum(isinstance(item.get("proof_lineage"), list) for item in nested),
        "nested_proof_lineage_entry_count": proof_lineage_entry_count,
        "nested_objects_with_proof_envelope_id": sum(bool(_text(item.get("proof_envelope_id"))) for item in nested),
        "nested_objects_with_proof_envelope_ids": sum(bool(item.get("proof_envelope_ids")) for item in nested),
        "nested_objects_with_proof_ids": sum(bool(item.get("proof_ids")) for item in nested),
        "nested_objects_with_verifier_id": sum(bool(_text(item.get("verifier_id"))) for item in nested),
        "nested_objects_with_verifier_ids": sum(bool(item.get("verifier_ids")) for item in nested),
        "unique_task_id_count": len(task_ids),
        "unique_proof_id_count": len(proof_ids),
        "unique_verifier_id_count": len(verifier_ids),
        "notes": [
            "Traversal is recursive across mappings and lists rather than top-level row keys only.",
            "Singular task/proof fields and plural legacy fields are both retained as lineage evidence.",
        ],
    }


def _unique_attempt_rows(sample_accounting: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows_by_location = {
        (path, row_index): row
        for path, row_index, row in _iter_result_jsonl_rows(records)
    }
    rows: list[Mapping[str, Any]] = []
    for item in list(sample_accounting.get("inventory") or []):
        if not isinstance(item, Mapping):
            continue
        canonical = item.get("canonical_occurrence") if isinstance(item.get("canonical_occurrence"), Mapping) else {}
        row = rows_by_location.get((_text(canonical.get("source_path")), int(canonical.get("source_row_index") or 0)))
        if isinstance(row, Mapping):
            rows.append(row)
    return rows


def _chronology(git_disposition: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "event": "phase8_recommendation",
            "status": POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
            "source": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE13_CANONICAL_PROMOTION_STATUS_2026-07-16.json",
            "note": "The historical recommendation is retained as a superseded claim, not a current promotion verdict.",
        },
        {
            "event": "operator_approval",
            "status": "historical_narrow_approval_recorded_not_current_promotion_authority",
            "source": "Plans/RESUME.md",
            "note": "Approval remains a chronological fact separate from evidence verdict and current application scope.",
        },
        {
            "event": "code_application",
            "status": "committed_source_default_hybrid",
            "source": POLICY_PATH,
            "git_commit": git_disposition.get("hybrid_default_introducing_commit", ""),
            "note": "This is a Git-derived implementation fact, not scientific evidence.",
        },
        {
            "event": "git_commit",
            "status": "committed",
            "source": "git_objects",
            "git_commit": git_disposition.get("hybrid_default_introducing_commit", ""),
            "note": "The Hybrid default was already present in the historical Phase 10 bundle baseline object.",
        },
        {
            "event": "deployment",
            "status": "not_evaluated",
            "source": "retrospective_bundle_scope",
            "note": "The retrospective package does not claim a deployment event or production governance qualification.",
        },
    ]


def _write_json_with_sidecar(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    digest = file_sha256(path)
    path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return f"sha256:{digest}"


def build_phase14_superseding_bundle(
    *,
    repo_root: str | Path | None = None,
    generated_at: str | None = None,
    plan_artifacts: Sequence[str | Path] | None = None,
    result_artifacts: Sequence[str | Path] | None = None,
    hillclimb_manifests: Sequence[str | Path] | None = None,
    transitions_path: str | Path | None = None,
    original_bundle_path: str | Path | None = None,
    phase12_audit_path: str | Path | None = None,
    phase13_status_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or DEFAULT_REPO_ROOT)
    original_path = Path(original_bundle_path or DEFAULT_ORIGINAL_BUNDLE_PATH)
    phase12_path = Path(phase12_audit_path or DEFAULT_PHASE12_AUDIT_PATH)
    phase13_path = Path(phase13_status_path or DEFAULT_PHASE13_STATUS_PATH)
    original_bundle = _read_json(original_path, label="original Phase 10 bundle")
    phase12_audit = _read_json(phase12_path, label="Phase 12 proof audit")
    phase13_status = _read_json(phase13_path, label="Phase 13 status map")

    plan_paths = [Path(path) for path in plan_artifacts] if plan_artifacts is not None else phase10._discover_plan_artifacts(root)  # noqa: SLF001
    plan_paths = [
        path
        for path in plan_paths
        if not path.name.startswith("SAGE_ARCHITECTURE_POLICY_EVAL_PHASE14_SUPERSEDING_BUNDLE_VALIDATION_")
    ]
    result_paths = [Path(path) for path in result_artifacts] if result_artifacts is not None else _discover_result_artifacts(root)
    manifest_paths = [Path(path) for path in hillclimb_manifests] if hillclimb_manifests is not None else phase10._discover_hillclimb_manifests(root)  # noqa: SLF001
    transition = (
        Path(transitions_path)
        if transitions_path is not None
        else root / phase10.DEFAULT_TRANSITIONS_PATH.relative_to(DEFAULT_REPO_ROOT)
    )
    artifact_paths = _unique_paths([
        *plan_paths,
        *result_paths,
        *manifest_paths,
        *([transition] if transition.exists() else []),
    ])
    records = sorted(
        [_artifact_record(path, root=root) for path in artifact_paths if path.exists() and path.is_file()],
        key=lambda item: _text(item.get("path")),
    )
    sample_accounting = _sample_accounting(records)
    unique_rows = _unique_attempt_rows(sample_accounting, records)
    git_disposition = _git_disposition(root, original_bundle=original_bundle)
    original_sha256 = f"sha256:{file_sha256(original_path)}"
    historical_auth = phase12_audit.get("historical_authorization_provenance")
    if not isinstance(historical_auth, list):
        historical_auth = historical_authorization_provenance()
    phase13_typed = (
        ((phase13_status.get("superseding_status") or {}).get("typed_verdict") or {})
        if isinstance(phase13_status.get("superseding_status"), Mapping)
        else {}
    )
    bundle: dict[str, Any] = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _now(),
        "source_plan": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md",
        "supersedes": {
            "path": _portable_path(original_path, root),
            "source_file_sha256": original_sha256,
            "internal_bundle_hash": original_bundle.get("bundle_hash", ""),
            "schema_version": original_bundle.get("schema_version"),
            "preserve_byte_identical": True,
            "supersession_status": "historical_bundle_superseded_not_edited",
            "known_reason_codes": [
                "git_disposition_contradicted_by_git_objects",
                "file_row_count_not_unique_sample_count",
                "recursive_lineage_shapes_missed",
                "redacted_nested_source_commitments_not_labeled",
            ],
        },
        "git_disposition": git_disposition,
        "chronology": _chronology(git_disposition),
        "embedded_artifacts": records,
        "artifact_commitment_summary": {
            "embedded_artifact_count": len(records),
            "portable_recomputable_count": sum(
                record.get("portable_hash_status") == PORTABLE_HASH_STATUS for record in records
            ),
            "source_commitment_only_count": sum(
                record.get("portable_hash_status") == SOURCE_COMMITMENT_ONLY_STATUS for record in records
            ),
            "omitted_sensitive_value_commitment_count": sum(
                len(list(record.get("omitted_sensitive_value_commitments") or [])) for record in records
            ),
            "canonical_raw_output_commitment_count": sum(
                commitment.get("commitment_role") == "canonical_raw_output_commitment"
                for record in records
                for commitment in list(record.get("omitted_sensitive_value_commitments") or [])
                if isinstance(commitment, Mapping)
            ),
            "limitation": (
                "A source payload changed by redaction retains a source commitment but is not portable-recomputable "
                "from the embedded payload alone."
            ),
        },
        "sample_accounting": sample_accounting,
        "lineage_summary": _lineage_summary(unique_rows),
        "historical_authorization_provenance": historical_auth,
        "typed_dispositions": {
            "product_policy_status": phase13_typed.get(
                "product_policy_status",
                POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
            ),
            "policy_evidence_scope": phase13_typed.get(
                "policy_evidence_scope",
                POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS,
            ),
            "policy_application_scope": phase13_typed.get(
                "policy_application_scope",
                POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS,
            ),
            "scope_governance_status": phase13_typed.get(
                "scope_governance_status",
                SCOPE_GOVERNANCE_NOT_EVALUATED,
            ),
            "approval_status": phase13_typed.get("approval_status", "historical_approval_not_current_promotion_authority"),
            "implementation_status": "committed_source_default_hybrid_unchanged",
            "external_authenticity_status": EXTERNAL_AUTHENTICITY_STATUS,
            "phase18_manifest_present": False,
        },
        "trust_boundary": [
            "This bundle is a corrected retrospective package; it does not turn historical rows into prospective Phase 18 evidence.",
            "Historical Phase 0-10 authorization provenance remains out-of-band or unavailable and is never retrofitted.",
            "External authenticity remains unverified until Phase 22 clean-checkout reproduction.",
        ],
    }
    bundle["bundle_hash"] = content_hash(bundle)
    return bundle


def render_bundle(bundle: Mapping[str, Any]) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=True)


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase14-superseding-bundle",
        help="build the Phase 14 superseding retrospective evidence bundle",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="JSON bundle output path")
    parser.add_argument("--generated-at", default=None, help="optional fixed UTC timestamp")
    parser.set_defaults(func=_cmd_phase14_superseding_bundle)


def _cmd_phase14_superseding_bundle(args: Any) -> int:
    try:
        bundle = build_phase14_superseding_bundle(generated_at=args.generated_at)
        output = Path(args.output)
        digest = _write_json_with_sidecar(output, bundle)
    except Exception as exc:
        print(f"phase14-superseding-bundle: {exc}", file=sys.stderr)
        return 2
    summary = {
        "kind": bundle["kind"],
        "bundle_hash": bundle["bundle_hash"],
        "output_sha256": digest,
        "output": _portable_path(output, DEFAULT_REPO_ROOT),
        "source_row_count": bundle["sample_accounting"]["counts"]["source_row_count"],
        "unique_logical_attempt_count": bundle["sample_accounting"]["counts"]["unique_logical_attempt_count"],
        "duplicate_source_row_count": bundle["sample_accounting"]["counts"]["duplicate_source_row_count"],
        "promotion_eligible_count": bundle["sample_accounting"]["counts"]["promotion_eligible_count"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True))
    return 0
