"""Independent validator for the Phase 14 superseding retrospective bundle.

The validator intentionally does not import ``phase14_superseding_bundle`` or
the Phase 10 generator. It recomputes bundle hashes, source commitments, Git
disposition, unique-attempt accounting, and recursive lineage summaries from
the emitted bundle plus local source files.
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


KIND = "phase14_superseding_retrospective_evidence_bundle"
SCHEMA_VERSION = 1
LOGICAL_ATTEMPT_KEY_VERSION = "logical-attempt-key-v1"
LINEAGE_SUMMARY_VERSION = "recursive-lineage-summary-v1"
PORTABLE_HASH_STATUS = "portable_recomputable"
SOURCE_COMMITMENT_ONLY_STATUS = "source_commitment_only_due_to_redaction"
EXTERNAL_AUTHENTICITY_STATUS = "unverified_pending_phase22_independent_reproduction"
POLICY_DEFAULT_RECOMMENDATION_INVALIDATED = "hybrid_default_recommendation_invalidated_pending_fresh_evidence"
POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS = "authorized_lab_harness"
POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS = "explicit_authorized_harness_sessions_only"
SCOPE_GOVERNANCE_NOT_EVALUATED = "not_evaluated_governance_program_not_authorized"

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_BUNDLE_PATH = (
    Path(__file__).resolve().parents[2]
    / ".hillclimb"
    / "results"
    / "phase14_superseding_retrospective_bundle_v1_20260716.json"
)
DEFAULT_REPORT_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE14_SUPERSEDING_BUNDLE_VALIDATION_2026-07-16.json"
)

POLICY_PATH = "Payload_Type/sage/ai/langgraph/policy.py"
_POLICY_DEFAULT_HYBRID_RE = re.compile(r"(?m)^POLICY_DEFAULT\s*=\s*POLICY_HYBRID\s*$")
_LOCAL_HOME_RE = re.compile(r"(?<![A-Za-z0-9])(?:file://)?/(?:home|Users)/[^/\s\"'`]+(?:/[^\s\"'`]+)*")
_PRIVATE_PAI_RE = re.compile(r"(?:(?:~|/(?:home|Users)/[^/\s\"'`]+)/)?(?:\.claude|\.codex)/PAI(?:/[^\s\"'`]+)*")
_SECRET_KEY_RE = re.compile(r"(?:^|_)(?:password|passwd|pwd|secret|pfx_base64)(?:$|_)")
_PROVIDER_OR_INFRA_RE = re.compile(
    r"provider|bedrock|timeout|timed out|reset|collection retry budget|infrastructure|connection|transport",
    re.IGNORECASE,
)
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


class Phase14ValidationError(ValueError):
    """Raised when the bundle or a local source cannot be parsed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase14ValidationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase14ValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Phase14ValidationError(f"expected JSON object: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Phase14ValidationError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise Phase14ValidationError(f"expected object at {path}:{line_no}")
        rows.append(row)
    return rows


def _load_source_payload(path: Path, media_type: str) -> Any:
    if media_type == "application/json":
        return _read_json(path)
    if media_type == "application/x-ndjson":
        return _read_jsonl(path)
    return path.read_text(encoding="utf-8")


def _json_pointer_get(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    current = value
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


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


def _git_show_text(root: Path, rev: str, path: str) -> str:
    return _git_output(root, "show", f"{rev}:{path}")


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


def _logical_attempt_identity(row: Mapping[str, Any]) -> dict[str, Any]:
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
        "source_row_sha256": _content_hash(row),
    }


def _logical_attempt_key(row: Mapping[str, Any]) -> str:
    return _content_hash(_logical_attempt_identity(row))


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


def _row_is_infrastructure_or_provider_failure(row: Mapping[str, Any]) -> bool:
    fields = [
        _text(row.get("status")),
        _text(row.get("controller_status")),
        _text(row.get("controller_terminal_reason")),
        _text((row.get("controller_blocker") or {}).get("reason") if isinstance(row.get("controller_blocker"), Mapping) else ""),
    ]
    return any(_PROVIDER_OR_INFRA_RE.search(field) for field in fields if field)


def _row_is_diagnostic(row: Mapping[str, Any], *, source_paths: Sequence[str]) -> bool:
    if any("diagnostic" in Path(path).name.casefold() for path in source_paths):
        return True
    if not _transaction_ids(row) and not _decision_ids(row):
        return True
    return not _branch_surface_observed(row)


def _row_is_accepted(row: Mapping[str, Any]) -> bool:
    return (
        row.get("objective_proven") is True
        and row.get("clean_stop") is True
        and row.get("backend_provenance_complete") is True
        and row.get("policy_identity_valid") is True
        and _text(row.get("controller_status")).casefold() in {"", "complete"}
    )


def _primary_disposition(row: Mapping[str, Any], *, source_paths: Sequence[str]) -> tuple[str, bool]:
    if any(Path(path).name in _BURNED_ARTIFACT_REASONS for path in source_paths):
        return "burned", _row_is_infrastructure_or_provider_failure(row)
    diagnostic = _row_is_diagnostic(row, source_paths=source_paths)
    infrastructure = _row_is_infrastructure_or_provider_failure(row)
    if diagnostic:
        return "diagnostic_only", infrastructure
    if infrastructure:
        return "infrastructure_or_provider_failure", True
    if _row_is_accepted(row):
        return "accepted", False
    return "failed", False


def _iter_result_jsonl_rows(records: Sequence[Mapping[str, Any]]) -> Iterable[tuple[str, int, Mapping[str, Any]]]:
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


def _recompute_sample_accounting(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, row_index, row in _iter_result_jsonl_rows(records):
        groups[_logical_attempt_key(row)].append({
            "source_path": path,
            "source_row_index": row_index,
            "embedded_row_sha256": _content_hash(row),
            "identity": _logical_attempt_identity(row),
            "row": row,
        })
    primary_counts: Counter[str] = Counter()
    duplicate_source_row_count = 0
    infrastructure_count = 0
    identity_collision_count = 0
    keys: list[str] = []
    for key in sorted(groups):
        keys.append(key)
        occurrences = sorted(groups[key], key=lambda item: (item["source_path"], int(item["source_row_index"])))
        if len({item["embedded_row_sha256"] for item in occurrences}) > 1:
            identity_collision_count += 1
        disposition, infrastructure = _primary_disposition(
            occurrences[0]["row"],
            source_paths=[item["source_path"] for item in occurrences],
        )
        primary_counts[disposition] += 1
        infrastructure_count += int(infrastructure)
        duplicate_source_row_count += max(0, len(occurrences) - 1)
    source_row_count = sum(len(items) for items in groups.values())
    return {
        "source_row_count": source_row_count,
        "unique_logical_attempt_count": len(groups),
        "duplicate_source_row_count": duplicate_source_row_count,
        "primary_disposition_counts": dict(sorted(primary_counts.items())),
        "infrastructure_or_provider_failure_count": infrastructure_count,
        "promotion_eligible_count": 0,
        "identity_collision_count": identity_collision_count,
        "logical_attempt_keys": keys,
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
        for value in list(item.get("task_ids") or []):
            if _text(value):
                task_ids.add(_text(value))
        for key in ("proof_envelope_id", "proof_id", "proof_envelope_ref"):
            if _text(item.get(key)):
                proof_ids.add(_text(item.get(key)))
        for key in ("proof_ids", "proof_envelope_ids"):
            for value in list(item.get(key) or []):
                if _text(value):
                    proof_ids.add(_text(value))
        if _text(item.get("verifier_id")):
            verifier_ids.add(_text(item.get("verifier_id")))
        for value in list(item.get("verifier_ids") or []):
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


def _unique_attempt_rows(bundle: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows_by_location = {
        (path, row_index): row
        for path, row_index, row in _iter_result_jsonl_rows(records)
    }
    rows: list[Mapping[str, Any]] = []
    sample = bundle.get("sample_accounting") if isinstance(bundle.get("sample_accounting"), Mapping) else {}
    for item in list(sample.get("inventory") or []):
        if not isinstance(item, Mapping):
            continue
        canonical = item.get("canonical_occurrence") if isinstance(item.get("canonical_occurrence"), Mapping) else {}
        row = rows_by_location.get((_text(canonical.get("source_path")), int(canonical.get("source_row_index") or 0)))
        if isinstance(row, Mapping):
            rows.append(row)
    return rows


def _contains_personal_paths(value: Any) -> bool:
    rendered = _canonical_json(value)
    return bool(_LOCAL_HOME_RE.search(rendered) or _PRIVATE_PAI_RE.search(rendered))


def _secret_fields_redacted(value: Any, *, key: str = "") -> bool:
    if isinstance(value, Mapping):
        return all(_secret_fields_redacted(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, list):
        return all(_secret_fields_redacted(item, key=key) for item in value)
    if isinstance(value, str) and _SECRET_KEY_RE.search(key.casefold()):
        return value.startswith("<")
    return True


def _validate_artifact_records(records: Sequence[Mapping[str, Any]], *, root: Path) -> dict[str, Any]:
    checks: list[bool] = []
    source_commitment_only_count = 0
    portable_recomputable_count = 0
    raw_output_commitment_count = 0
    for record in records:
        payload = record.get("payload")
        checks.append(record.get("embedded_payload_sha256") == _content_hash(payload))
        status = _text(record.get("portable_hash_status"))
        if status == SOURCE_COMMITMENT_ONLY_STATUS:
            source_commitment_only_count += 1
            checks.append(record.get("redaction_changed") is True)
        elif status == PORTABLE_HASH_STATUS:
            portable_recomputable_count += 1
            checks.append(record.get("redaction_changed") is False)
            checks.append(record.get("source_payload_sha256") == record.get("embedded_payload_sha256"))
        else:
            checks.append(False)
        relative = _text(record.get("path"))
        source_path = root / relative
        if source_path.exists() and source_path.is_file():
            raw_payload = _load_source_payload(source_path, _text(record.get("media_type")))
            checks.append(record.get("source_file_sha256") == _file_sha256(source_path))
            checks.append(record.get("source_payload_sha256") == _content_hash(raw_payload))
            for commitment in list(record.get("omitted_sensitive_value_commitments") or []):
                if not isinstance(commitment, Mapping):
                    checks.append(False)
                    continue
                raw_value = _json_pointer_get(raw_payload, _text(commitment.get("json_pointer")))
                checks.append(commitment.get("source_value_sha256") == _content_hash(raw_value))
                if commitment.get("commitment_role") == "canonical_raw_output_commitment":
                    raw_output_commitment_count += 1
        else:
            checks.append(False)
    return {
        "all_checks_pass": all(checks),
        "artifact_record_count": len(records),
        "portable_recomputable_count": portable_recomputable_count,
        "source_commitment_only_count": source_commitment_only_count,
        "canonical_raw_output_commitment_count": raw_output_commitment_count,
    }


def validate_phase14_bundle(
    bundle: Mapping[str, Any],
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or DEFAULT_REPO_ROOT)
    records = bundle.get("embedded_artifacts") if isinstance(bundle.get("embedded_artifacts"), list) else []
    artifact_report = _validate_artifact_records(records, root=root)
    sample = bundle.get("sample_accounting") if isinstance(bundle.get("sample_accounting"), Mapping) else {}
    counts = sample.get("counts") if isinstance(sample.get("counts"), Mapping) else {}
    recomputed_counts = _recompute_sample_accounting(records)
    inventory = [item for item in list(sample.get("inventory") or []) if isinstance(item, Mapping)]
    inventory_keys = sorted(_text(item.get("logical_attempt_key")) for item in inventory)
    unique_rows = _unique_attempt_rows(bundle, records)
    recomputed_lineage = _lineage_summary(unique_rows)
    supersedes = bundle.get("supersedes") if isinstance(bundle.get("supersedes"), Mapping) else {}
    original_path = root / _text(supersedes.get("path"))
    git_disposition = bundle.get("git_disposition") if isinstance(bundle.get("git_disposition"), Mapping) else {}
    original_head = _text(git_disposition.get("original_phase10_bundle_base_head"))
    current_head = _text(git_disposition.get("current_head"))
    original_policy_text = _git_show_text(root, original_head, POLICY_PATH)
    current_policy_text = _git_show_text(root, current_head, POLICY_PATH)
    historical_auth = [
        item for item in list(bundle.get("historical_authorization_provenance") or [])
        if isinstance(item, Mapping)
    ]
    typed = bundle.get("typed_dispositions") if isinstance(bundle.get("typed_dispositions"), Mapping) else {}
    bundle_without_hash = dict(bundle)
    bundle_without_hash.pop("bundle_hash", None)
    checks = {
        "kind_and_schema_valid": bundle.get("kind") == KIND and bundle.get("schema_version") == SCHEMA_VERSION,
        "bundle_hash_valid": bundle.get("bundle_hash") == _content_hash(bundle_without_hash),
        "artifact_payloads_and_source_commitments_validate_independently": artifact_report["all_checks_pass"],
        "personal_absolute_paths_removed": not _contains_personal_paths(bundle),
        "secret_fields_redacted": _secret_fields_redacted(bundle),
        "logical_attempt_key_version_frozen": sample.get("logical_attempt_key_version") == LOGICAL_ATTEMPT_KEY_VERSION,
        "inventory_keys_match_independent_recompute": inventory_keys == recomputed_counts["logical_attempt_keys"],
        "sample_counts_match_independent_recompute": {
            key: counts.get(key)
            for key in (
                "source_row_count",
                "unique_logical_attempt_count",
                "duplicate_source_row_count",
                "primary_disposition_counts",
                "infrastructure_or_provider_failure_count",
                "promotion_eligible_count",
                "identity_collision_count",
            )
        } == {
            key: recomputed_counts.get(key)
            for key in (
                "source_row_count",
                "unique_logical_attempt_count",
                "duplicate_source_row_count",
                "primary_disposition_counts",
                "infrastructure_or_provider_failure_count",
                "promotion_eligible_count",
                "identity_collision_count",
            )
        },
        "sample_count_reconciliation_holds": all(
            bool(value) for value in dict(counts.get("reconciliation") or {}).values()
        ),
        "lineage_summary_matches_independent_recursive_recompute": bundle.get("lineage_summary") == recomputed_lineage,
        "original_phase10_bundle_remains_byte_identical": (
            original_path.exists()
            and supersedes.get("preserve_byte_identical") is True
            and supersedes.get("source_file_sha256") == _file_sha256(original_path)
        ),
        "git_disposition_is_object_derived_and_hybrid_was_already_committed": (
            git_disposition.get("derived_from_git_objects") is True
            and _git_success(root, "cat-file", "-e", f"{original_head}^{{commit}}")
            and _git_success(root, "cat-file", "-e", f"{current_head}^{{commit}}")
            and bool(_POLICY_DEFAULT_HYBRID_RE.search(original_policy_text))
            and bool(_POLICY_DEFAULT_HYBRID_RE.search(current_policy_text))
            and ((git_disposition.get("derived_disposition") or {}).get("hybrid_default_at_original_bundle_baseline") == "committed")
            and ((git_disposition.get("derived_disposition") or {}).get("hybrid_default_at_current_head") == "committed")
        ),
        "historical_authorization_provenance_not_retrofitted": (
            len(historical_auth) == 11
            and {item.get("phase") for item in historical_auth} == set(range(0, 11))
            and all(item.get("provenance_status") == "out_of_band" for item in historical_auth)
            and all(item.get("prospective_manifest_binding_status") == "unavailable" for item in historical_auth)
            and all(item.get("prospective_manifest_synthesized") is False for item in historical_auth)
            and all(item.get("retrofit_permitted") is False for item in historical_auth)
        ),
        "typed_two_axis_statuses_remain_separate": (
            typed.get("product_policy_status") == POLICY_DEFAULT_RECOMMENDATION_INVALIDATED
            and typed.get("policy_evidence_scope") == POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS
            and typed.get("policy_application_scope") == POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS
            and typed.get("scope_governance_status") == SCOPE_GOVERNANCE_NOT_EVALUATED
            and typed.get("external_authenticity_status") == EXTERNAL_AUTHENTICITY_STATUS
            and typed.get("phase18_manifest_present") is False
        ),
    }
    return {
        "kind": "phase14_superseding_retrospective_bundle_validation",
        "schema_version": 1,
        "validated_at": _now(),
        "bundle_hash": bundle.get("bundle_hash"),
        "checks": checks,
        "passes_gate": all(checks.values()),
        "failed_checks": [key for key, value in checks.items() if not value],
        "independent_recompute": {
            "sample_counts": recomputed_counts,
            "lineage_summary": recomputed_lineage,
            "artifact_report": artifact_report,
        },
    }


def _write_json_with_sidecar(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    digest = _file_sha256(path)
    path.with_suffix(".sha256").write_text(f"{digest.removeprefix('sha256:')}  {path.name}\n", encoding="utf-8")
    return digest


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase14-superseding-bundle-validate",
        help="independently validate the Phase 14 superseding retrospective bundle",
    )
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE_PATH), help="bundle JSON path")
    parser.add_argument("--output", default=str(DEFAULT_REPORT_PATH), help="validation report JSON path")
    parser.set_defaults(func=_cmd_phase14_superseding_bundle_validate)


def _cmd_phase14_superseding_bundle_validate(args: Any) -> int:
    try:
        bundle = _read_json(Path(args.bundle))
        report = validate_phase14_bundle(bundle)
        digest = _write_json_with_sidecar(Path(args.output), report)
    except Exception as exc:
        print(f"phase14-superseding-bundle-validate: {exc}", file=sys.stderr)
        return 2
    summary = {
        "kind": report["kind"],
        "bundle_hash": report["bundle_hash"],
        "passes_gate": report["passes_gate"],
        "failed_checks": report["failed_checks"],
        "output_sha256": digest,
        "output": str(Path(args.output)),
    }
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True))
    return 0 if report["passes_gate"] else 1

