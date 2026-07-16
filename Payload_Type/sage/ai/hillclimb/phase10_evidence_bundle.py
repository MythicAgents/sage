"""Phase 10 redacted portable evidence bundle composer.

This module is intentionally read-only.  It packages already-retained planning
contracts, result artifacts, trajectory provenance, and current patch state into
one self-validating JSON bundle.  It does not launch live work, mutate runtime
state, score new candidates, or authorize promotion.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence

from .experiment_contracts import content_hash, file_sha256
from ..trajectory.schema import SCHEMA_VERSION, redact_text


BUNDLE_KIND = "phase10_portable_evidence_bundle"
BUNDLE_SCHEMA_VERSION = 1
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
DEFAULT_OUTPUT_PATH = DEFAULT_RESULTS_ROOT / "phase10_portable_evidence_bundle_20260716.json"
DEFAULT_TRANSITIONS_PATH = Path(__file__).resolve().parents[2] / ".trajectory" / "transitions.jsonl"

PHASE10_REQUIRED_STATUSES = (
    "rejected_offline",
    "benchmark_nondiscriminating",
    "auto_harness_not_ready",
    "eligible_for_supervised_artifact_campaign",
    "supervised_artifact_candidate_ranked_pending_human_review",
    "eligible_for_live_canary",
    "rejected_live",
    "validated_opt_in",
    "hybrid_default_recommended_pending_operator_approval",
    "eligible_pending_review_and_commit",
)

PLAN_PATTERNS = (
    "SAGE_ARCHITECTURE_POLICY_EVAL_*.md",
    "SAGE_ARCHITECTURE_POLICY_EVAL_*.json",
    "CURRENT_WORK.md",
)
HILLCLIMB_MANIFEST_NAMES = (
    "decision_cases.json",
    "operator_replay_cases.json",
    "policy_replay_calibration_manifest.json",
    "policy_replay_corpus_sources.json",
    "policy_replay_frontier_corpus.json",
)
PHASE_REPORT_PATHS = {
    "phase6": "Payload_Type/sage/.hillclimb/results/laps_family_transfer_matrix_validation_r5_20260715.json",
    "phase7": "Payload_Type/sage/.hillclimb/results/trust_context_corroboration_live_validation_v2_20260715.json",
    "phase8": "Payload_Type/sage/.hillclimb/results/phase8_goad_regression_validation_v2_20260716.json",
    "phase9": "Payload_Type/sage/.hillclimb/results/phase9_auto_harness_readiness_verdict_20260716.json",
}
REQUIRED_EVIDENCE_MAP_KEYS = (
    "base_head_patch_stack_file_hashes",
    "architecture_approvals",
    "experiment_and_campaign_manifests",
    "sealed_commitments_and_split_manifests",
    "reset_and_snapshot_attestations",
    "all_attempts",
    "trajectory_v2_label_outcome_provenance",
    "frontier_and_policy_decision_lineage",
    "backend_transaction_task_verifier_artifact_proof_lineage",
    "readiness_statistics_and_canaries",
    "supervised_campaign",
    "verification",
    "live_matrices_and_reliability",
    "superseding_assessment",
    "final_dispositions",
)

_HASH_TOKEN_RE = re.compile(r"(?P<prefix>(?:[A-Za-z0-9_.:-]*sha256:))(?P<digest>[0-9a-fA-F]{16,64})")
_SAFE_REDACTION_MARKER_RE = re.compile(
    r"<(?:password:redacted|base64_blob|local-path:redacted|private-memory:redacted|"
    r"(?:ntlm|aes256|sage-secret|secret):sha256:[0-9a-fA-F]{16})>"
)
_LOCAL_HOME_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:file://)?/(?:home|Users)/[^/\s\"'`]+(?:/[^\s\"'`]+)*"
)
_PRIVATE_PAI_RE = re.compile(
    r"(?:(?:~|/(?:home|Users)/[^/\s\"'`]+)/)?(?:\.claude|\.codex)/PAI(?:/[^\s\"'`]+)*"
)
_HASH_KEY_RE = re.compile(r"(?:^|_)(?:sha256|hash|hashes|digest|head|commit)(?:$|_)")
_SECRET_KEY_RE = re.compile(r"(?:^|_)(?:password|passwd|pwd|secret)(?:$|_)")
_PFX_BASE64_KEY_RE = re.compile(r"(?:^|_)pfx_base64(?:$|_)")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _payload_hash(value: Any) -> str:
    if isinstance(value, str):
        return _sha256_text(value)
    return content_hash(value)


def _source_head(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _git_lines(root: Path, *args: str) -> list[str]:
    try:
        output = subprocess.check_output(
            ["git", *args],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    return [line for line in output.splitlines() if line.strip()]


def _portable_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.name


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        key = str(Path(path))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(Path(path))
    return ordered


def _protect_safe_tokens(text: str) -> tuple[str, dict[str, str]]:
    replacements: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        token = f"__SAGE_PHASE10_HASH_{len(replacements)}__"
        replacements[token] = match.group(0)
        return token

    protected = _SAFE_REDACTION_MARKER_RE.sub(repl, text)
    return _HASH_TOKEN_RE.sub(repl, protected), replacements


def _restore_safe_tokens(text: str, replacements: Mapping[str, str]) -> str:
    for token, original in replacements.items():
        text = text.replace(token, original)
    return text


def _sanitize_paths(text: str, *, root: Path) -> str:
    normalized = text.replace(str(root.resolve()), "<repo-root>")
    normalized = _PRIVATE_PAI_RE.sub("<private-memory:redacted>", normalized)
    normalized = _LOCAL_HOME_RE.sub("<local-path:redacted>", normalized)
    return normalized


def _sanitize_string(value: str, *, root: Path) -> str:
    text = _sanitize_paths(str(value), root=root)
    protected, replacements = _protect_safe_tokens(text)
    redacted = redact_text(protected)
    return _restore_safe_tokens(redacted, replacements)


def _is_hash_key(key: str) -> bool:
    return bool(_HASH_KEY_RE.search(str(key or "").casefold()))


def _is_secret_key(key: str) -> bool:
    normalized = str(key or "").casefold()
    if "redaction" in normalized or normalized.endswith("_required"):
        return False
    return bool(_SECRET_KEY_RE.search(normalized) or _PFX_BASE64_KEY_RE.search(normalized))


def _sanitize_value(value: Any, *, root: Path, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_value(item_value, root=root, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, root=root, key=key) for item in value]
    if isinstance(value, Path):
        return _sanitize_string(value.as_posix(), root=root)
    if isinstance(value, str):
        if _is_secret_key(key):
            return "<base64_blob>" if _PFX_BASE64_KEY_RE.search(str(key).casefold()) else "<password:redacted>"
        if _is_hash_key(key):
            return _sanitize_paths(value, root=root)
        return _sanitize_string(value, root=root)
    return value


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _load_artifact_payload(path: Path) -> tuple[str, Any]:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        return "application/json", _read_json(path)
    if suffix == ".jsonl":
        return "application/x-ndjson", _read_jsonl(path)
    return "text/markdown" if suffix == ".md" else "text/plain", path.read_text(encoding="utf-8")


def _classify_artifact(path: Path, *, root: Path) -> str:
    relative = _portable_path(path, root)
    if relative.startswith("Plans/"):
        return "planning_contract"
    if relative.endswith(".trajectory/transitions.jsonl"):
        return "trajectory_store"
    if relative.startswith("Payload_Type/sage/.hillclimb/results/"):
        return "result_artifact"
    if relative.startswith("Payload_Type/sage/ai/hillclimb/"):
        return "experiment_manifest"
    return "supporting_artifact"


def _artifact_record(path: Path, *, root: Path, artifact_class: str | None = None) -> dict[str, Any]:
    media_type = "application/octet-stream"
    parse_error = ""
    try:
        media_type, raw_payload = _load_artifact_payload(path)
    except Exception as exc:
        parse_error = f"{type(exc).__name__}: {exc}"
        raw_payload = path.read_text(encoding="utf-8", errors="replace")
        media_type = "text/plain"
    payload = _sanitize_value(raw_payload, root=root)
    return {
        "path": _portable_path(path, root),
        "artifact_class": artifact_class or _classify_artifact(path, root=root),
        "media_type": media_type,
        "source_sha256": f"sha256:{file_sha256(path)}",
        "source_bytes": path.stat().st_size,
        "embedded_sha256": _payload_hash(payload),
        "redaction_changed": _canonical_json(raw_payload) != _canonical_json(payload),
        "parse_error": parse_error,
        "payload": payload,
    }


def _resume_excerpt_record(root: Path, *, line_limit: int = 1200) -> dict[str, Any] | None:
    path = root / "Plans" / "RESUME.md"
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    start_index = next(
        (
            index
            for index, line in enumerate(lines)
            if "ARCHITECTURE/POLICY EVAL PHASE 9 COMPLETE" in line
        ),
        0,
    )
    selected = lines[start_index:start_index + line_limit]
    raw_payload = "\n".join(selected) + ("\n" if selected else "")
    payload = _sanitize_string(raw_payload, root=root)
    return {
        "path": "Plans/RESUME.md#phase0-phase9-excerpt",
        "artifact_class": "phase_handoff_excerpt",
        "media_type": "text/markdown",
        "source_sha256": _sha256_text(raw_payload),
        "source_bytes": len(raw_payload.encode("utf-8")),
        "source_line_range": [start_index + 1, start_index + len(selected)],
        "embedded_sha256": _payload_hash(payload),
        "redaction_changed": payload != raw_payload,
        "parse_error": "",
        "payload": payload,
    }


def _discover_plan_artifacts(root: Path) -> list[Path]:
    plans_root = root / "Plans"
    return _unique_paths(
        path
        for pattern in PLAN_PATTERNS
        for path in sorted(plans_root.glob(pattern))
        if path.exists() and path.is_file()
    )


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
    ]


def _discover_hillclimb_manifests(root: Path) -> list[Path]:
    hillclimb_root = root / "Payload_Type" / "sage" / "ai" / "hillclimb"
    return [
        hillclimb_root / name
        for name in HILLCLIMB_MANIFEST_NAMES
        if (hillclimb_root / name).exists()
    ]


def _patch_state(root: Path, *, source_head: str) -> dict[str, Any]:
    tracked_name_status = _git_lines(root, "diff", "--name-status")
    cached_name_status = _git_lines(root, "diff", "--cached", "--name-status")
    untracked_paths = _git_lines(root, "ls-files", "--others", "--exclude-standard")
    changed_paths: list[str] = []
    for line in [*tracked_name_status, *cached_name_status]:
        parts = line.split("\t")
        if parts:
            changed_paths.extend(parts[1:] or parts[:1])
    changed_paths.extend(untracked_paths)
    changed_hashes: list[dict[str, Any]] = []
    for relative in sorted(dict.fromkeys(item for item in changed_paths if item.strip())):
        path = root / relative
        changed_hashes.append({
            "path": _sanitize_string(relative, root=root),
            "exists": path.exists(),
            "sha256": f"sha256:{file_sha256(path)}" if path.exists() and path.is_file() else None,
        })
    return {
        "base_head": source_head,
        "tracked_name_status": tracked_name_status,
        "cached_name_status": cached_name_status,
        "untracked_paths": untracked_paths,
        "tracked_diff_stat": _git_lines(root, "diff", "--stat"),
        "cached_diff_stat": _git_lines(root, "diff", "--cached", "--stat"),
        "changed_file_hashes": changed_hashes,
        "worktree_clean": not tracked_name_status and not cached_name_status and not untracked_paths,
    }


def _find_artifact_payload(records: Sequence[Mapping[str, Any]], relative_path: str) -> Any | None:
    for record in records:
        if str(record.get("path") or "") == relative_path:
            return record.get("payload")
    return None


def _transition_provenance(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    transition_path = "Payload_Type/sage/.trajectory/transitions.jsonl"
    payload = _find_artifact_payload(records, transition_path)
    rows = payload if isinstance(payload, list) else []
    schema_counts = Counter(str(row.get("schema_version", "unknown")) for row in rows if isinstance(row, Mapping))
    label_sources = Counter(str(row.get("label_source") or "") for row in rows if isinstance(row, Mapping))
    outcome_sources = Counter(str(row.get("outcome_source") or "") for row in rows if isinstance(row, Mapping))
    transition_outcomes = Counter(str(row.get("transition_outcome") or "") for row in rows if isinstance(row, Mapping))
    v2_rows = [
        {
            "run_id": row.get("run_id", ""),
            "episode_id": row.get("episode_id", ""),
            "engagement_id": row.get("engagement_id", ""),
            "decision_id": row.get("decision_id", ""),
            "transaction_id": row.get("transaction_id", ""),
            "capability": row.get("capability", ""),
            "failure_label": row.get("failure_label", ""),
            "label_source": row.get("label_source", ""),
            "evidence_role": row.get("evidence_role", ""),
            "outcome_source": row.get("outcome_source", ""),
            "transition_outcome": row.get("transition_outcome", ""),
            "proof_envelope_ref": row.get("proof_envelope_ref", ""),
            "effective_backend": row.get("effective_backend", ""),
            "raw_frontier_hash": row.get("raw_frontier_hash", ""),
            "admissible_frontier_hash": row.get("admissible_frontier_hash", ""),
            "semantic_candidate_ids": list(row.get("semantic_candidate_ids") or []),
        }
        for row in rows
        if isinstance(row, Mapping) and int(row.get("schema_version") or 0) >= SCHEMA_VERSION
    ]
    return {
        "source_path": transition_path,
        "record_count": len(rows),
        "schema_counts": dict(sorted(schema_counts.items())),
        "schema_v2_record_count": len(v2_rows),
        "label_sources": dict(sorted((key, value) for key, value in label_sources.items() if key)),
        "outcome_sources": dict(sorted((key, value) for key, value in outcome_sources.items() if key)),
        "transition_outcomes": dict(sorted((key, value) for key, value in transition_outcomes.items() if key)),
        "schema_v2_rows": v2_rows,
        "limitations": [
            "Schema-v1 rows remain retained for diagnosis but do not become empirical promotion evidence.",
            "Only schema-v2 rows with exact proof lineage can support empirical outcome claims.",
        ],
    }


def _iter_nested_dicts(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _iter_nested_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_dicts(item)


def _lineage_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result_rows: list[Mapping[str, Any]] = []
    for record in records:
        if record.get("artifact_class") != "result_artifact":
            continue
        payload = record.get("payload")
        if isinstance(payload, list):
            result_rows.extend(row for row in payload if isinstance(row, Mapping))
    nested = [item for row in result_rows for item in _iter_nested_dicts(row)]
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
            if str(item.get(key) or "").strip():
                task_ids.add(str(item.get(key) or "").strip())
        for value in list(item.get("task_ids") or []):
            if str(value or "").strip():
                task_ids.add(str(value or "").strip())
        for key in ("proof_envelope_id", "proof_id", "proof_envelope_ref"):
            if str(item.get(key) or "").strip():
                proof_ids.add(str(item.get(key) or "").strip())
        for key in ("proof_ids", "proof_envelope_ids"):
            for value in list(item.get(key) or []):
                if str(value or "").strip():
                    proof_ids.add(str(value or "").strip())
        if str(item.get("verifier_id") or "").strip():
            verifier_ids.add(str(item.get("verifier_id") or "").strip())
        for value in list(item.get("verifier_ids") or []):
            if str(value or "").strip():
                verifier_ids.add(str(value or "").strip())
    return {
        "result_jsonl_row_count": len(result_rows),
        "rows_with_decisions": sum(bool(row.get("decisions")) for row in result_rows),
        "rows_with_transactions": sum(bool(row.get("transactions")) for row in result_rows),
        "rows_with_backend_provenance_complete": sum(row.get("backend_provenance_complete") is True for row in result_rows),
        "rows_with_objective_proven": sum(row.get("objective_proven") is True for row in result_rows),
        "rows_with_clean_stop": sum(row.get("clean_stop") is True for row in result_rows),
        "nested_objects_with_raw_frontier": sum("raw_frontier" in item for item in nested),
        "nested_objects_with_admissible_frontier": sum("admissible_frontier" in item for item in nested),
        "nested_objects_with_child_tasks": sum(isinstance(item.get("child_tasks"), list) for item in nested),
        "nested_child_task_entry_count": child_task_entry_count,
        "nested_objects_with_task_id": sum(bool(str(item.get("task_id") or "").strip()) for item in nested),
        "nested_objects_with_task_ids": sum(bool(item.get("task_ids")) for item in nested),
        "nested_objects_with_proof_lineage": sum(isinstance(item.get("proof_lineage"), list) for item in nested),
        "nested_proof_lineage_entry_count": proof_lineage_entry_count,
        "nested_objects_with_proof_envelope_id": sum(
            bool(str(item.get("proof_envelope_id") or "").strip()) for item in nested
        ),
        "nested_objects_with_proof_envelope_ids": sum(bool(item.get("proof_envelope_ids")) for item in nested),
        "nested_objects_with_proof_ids": sum(bool(item.get("proof_ids")) for item in nested),
        "nested_objects_with_verifier_id": sum(bool(item.get("verifier_id")) for item in nested),
        "nested_objects_with_verifier_ids": sum(bool(item.get("verifier_ids")) for item in nested),
        "unique_task_id_count": len(task_ids),
        "unique_proof_id_count": len(proof_ids),
        "unique_verifier_id_count": len(verifier_ids),
        "notes": [
            "The legacy packet corpus can lack raw-frontier and rejection-reason evidence; missing fields remain missing rather than being reconstructed.",
            "Embedded JSONL rows preserve decision, transaction, task, verifier, artifact, and proof fields when the source artifact retained them.",
            "The recursive summary recognizes singular task/proof fields, nested child_tasks/proof_lineage arrays, and legacy plural ID fields.",
        ],
    }


def _attempt_coverage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result_jsonl = [
        record
        for record in records
        if record.get("artifact_class") == "result_artifact"
        and record.get("media_type") == "application/x-ndjson"
    ]
    explicit_statuses: Counter[str] = Counter()
    row_count = 0
    for record in result_jsonl:
        rows = record.get("payload")
        if not isinstance(rows, list):
            continue
        row_count += len(rows)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for field in ("status", "controller_status", "disposition"):
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    explicit_statuses[f"{field}:{value}"] += 1
    return {
        "jsonl_artifact_count": len(result_jsonl),
        "row_count": row_count,
        "all_discovered_jsonl_result_artifacts_embedded": True,
        "explicit_row_status_counts": dict(sorted(explicit_statuses.items())),
        "provider_error_attempts": {
            "embedded": True,
            "counted_provider_error_rows": explicit_statuses.get("status:provider_error", 0),
            "note": (
                "Phase 5 retained the bounded provider-route failure as a negative execution note rather than "
                "promoting it to response-derived model evidence; Phase 9 embeds the fixture proxy-failure attempts."
            ),
        },
        "notes": [
            "The bundle embeds every JSON/JSONL result artifact currently retained under `.hillclimb/results`.",
            "Raw `.log` files are omitted because the redacted JSON/JSONL evidence is the portable authority.",
        ],
    }


def _phase_report(records: Sequence[Mapping[str, Any]], phase: str) -> Mapping[str, Any]:
    payload = _find_artifact_payload(records, PHASE_REPORT_PATHS[phase])
    return payload if isinstance(payload, Mapping) else {}


def _status_occurrences(phase8: Mapping[str, Any], phase9: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    readiness = phase9.get("readiness") if isinstance(phase9.get("readiness"), Mapping) else {}
    recommendation = phase8.get("recommendation") if isinstance(phase8.get("recommendation"), Mapping) else {}
    return {
        "rejected_offline": [
            {
                "phase": "phase9",
                "subject": "known-violation T0 control",
                "evidence": PHASE_REPORT_PATHS["phase9"],
                "reason": "T0 correctly rejects the known violating artifact.",
            }
        ],
        "benchmark_nondiscriminating": [
            {
                "phase": "phase5",
                "subject": "current packet corpus",
                "evidence": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE5_FULL_FRONTIER_T3_2026-07-14.md",
                "reason": "A deterministic control reproduced the empirical best on the current corpus.",
            }
        ],
        "auto_harness_not_ready": [
            {
                "phase": "phase9",
                "subject": str(readiness.get("candidate_surface") or "retrieval-ranking"),
                "evidence": PHASE_REPORT_PATHS["phase9"],
                "reason": str(readiness.get("cheapest_decisive_next_experiment") or ""),
            }
        ],
        "eligible_for_supervised_artifact_campaign": [],
        "supervised_artifact_candidate_ranked_pending_human_review": [],
        "eligible_for_live_canary": [],
        "rejected_live": [],
        "validated_opt_in": [],
        "hybrid_default_recommended_pending_operator_approval": [
            {
                "phase": "phase8",
                "subject": "hybrid policy mode",
                "evidence": PHASE_REPORT_PATHS["phase8"],
                "reason": str(recommendation.get("reason") or ""),
            }
        ],
        "eligible_pending_review_and_commit": [
            {
                "phase": "phase10",
                "subject": "accumulated uncommitted Phase 0-10 evidence-backed patch stack",
                "evidence": "bundle_metadata.patch_stack",
                "reason": "Phase 10 packages the completed tranche for operator review and commit.",
            }
        ],
    }


def _superseding_assessment() -> list[dict[str, Any]]:
    return [
        {
            "assessment_id": "july12-14-policy-ordering",
            "assessment": "retained_but_narrowed",
            "claim": "Hybrid and LLM outperform Symbolic on the sealed branch-choice surfaces exercised so far.",
            "evidence": [
                "Payload_Type/sage/.hillclimb/results/gpo_dc_scope_matrix_validation_20260713.json",
                PHASE_REPORT_PATHS["phase6"],
            ],
            "limitation": "This does not prove Hybrid > LLM or universal AD-range transfer.",
        },
        {
            "assessment_id": "july12-14-goad-causal-claim",
            "assessment": "superseded",
            "claim": "GOAD is reliability evidence, not the causal model-contribution proof.",
            "evidence": [
                PHASE_REPORT_PATHS["phase6"],
                PHASE_REPORT_PATHS["phase8"],
            ],
            "limitation": "Phase 6 carries the causal vignette; Phase 8 GOAD rows were kernel-only reliability rows.",
        },
        {
            "assessment_id": "july12-14-eval-only-selector-promotion",
            "assessment": "not_promoted",
            "claim": "The modeled-reachability selector remains an eval-only research candidate.",
            "evidence": [
                "Payload_Type/sage/.hillclimb/results/policy_replay_hillclimb_iteration_20260713.json",
                "Payload_Type/sage/.hillclimb/results/policy_replay_promotion_gate_20260713.json",
            ],
            "limitation": "Synthetic held-out consistency did not authorize runtime promotion.",
        },
        {
            "assessment_id": "july12-14-auto-harness-readiness",
            "assessment": "rejected_for_current_surface",
            "claim": "The current structural retrieval-ranking surface is not ready for supervised auto-harness improvement.",
            "evidence": [PHASE_REPORT_PATHS["phase9"]],
            "limitation": "This is surface-specific and does not reject a future narrower lexical/configuration surface.",
        },
    ]


def _evidence_map() -> dict[str, Any]:
    return {
        "base_head_patch_stack_file_hashes": {
            "status": "present",
            "sources": ["bundle_metadata.patch_stack"],
        },
        "architecture_approvals": {
            "status": "present",
            "sources": ["architecture_approvals", "Plans/RESUME.md#phase0-phase9-excerpt"],
        },
        "experiment_and_campaign_manifests": {
            "status": "present_with_campaign_not_applicable",
            "sources": [
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE0_BASELINE_MANIFEST_2026-07-14.json",
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE0_ATTEMPT_SCHEMA_2026-07-14.json",
                "Payload_Type/sage/ai/hillclimb/policy_replay_calibration_manifest.json",
                "Payload_Type/sage/ai/hillclimb/policy_replay_corpus_sources.json",
            ],
        },
        "sealed_commitments_and_split_manifests": {
            "status": "present",
            "sources": [
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE0_HASH_MEMBERSHIP_2026-07-14.json",
                "Payload_Type/sage/ai/hillclimb/policy_replay_frontier_corpus.json",
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE6_LAPS_HOLDOUT_REPLACEMENT_R5_2026-07-15.md",
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE7_TRUST_CONTEXT_CORROBORATION_2026-07-15.md",
            ],
        },
        "reset_and_snapshot_attestations": {
            "status": "present_with_phase9_ahi22_negative",
            "sources": [
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE6_LAPS_HOLDOUT_REPLACEMENT_R5_2026-07-15.md",
                "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE7_TRUST_CONTEXT_CORROBORATION_2026-07-15.md",
                PHASE_REPORT_PATHS["phase8"],
                PHASE_REPORT_PATHS["phase9"],
            ],
        },
        "all_attempts": {
            "status": "present",
            "sources": ["attempt_coverage", "embedded_artifacts[result_artifact]"],
        },
        "trajectory_v2_label_outcome_provenance": {
            "status": "present",
            "sources": ["trajectory_provenance", "Payload_Type/sage/.trajectory/transitions.jsonl"],
        },
        "frontier_and_policy_decision_lineage": {
            "status": "present_with_legacy_raw_frontier_limit",
            "sources": ["lineage_summary", "embedded_artifacts[result_artifact]"],
        },
        "backend_transaction_task_verifier_artifact_proof_lineage": {
            "status": "present",
            "sources": ["lineage_summary", PHASE_REPORT_PATHS["phase6"], PHASE_REPORT_PATHS["phase7"], PHASE_REPORT_PATHS["phase8"]],
        },
        "readiness_statistics_and_canaries": {
            "status": "present_negative",
            "sources": [PHASE_REPORT_PATHS["phase9"]],
        },
        "supervised_campaign": {
            "status": "not_applicable",
            "sources": [PHASE_REPORT_PATHS["phase9"]],
            "reason": "Phase 9 emitted auto_harness_not_ready, so the plan prohibits a supervised campaign.",
        },
        "verification": {
            "status": "present",
            "sources": ["verification"],
        },
        "live_matrices_and_reliability": {
            "status": "present",
            "sources": [PHASE_REPORT_PATHS["phase6"], PHASE_REPORT_PATHS["phase7"], PHASE_REPORT_PATHS["phase8"]],
        },
        "superseding_assessment": {
            "status": "present",
            "sources": ["superseding_assessment"],
        },
        "final_dispositions": {
            "status": "present",
            "sources": ["final_dispositions", "status_occurrences"],
        },
    }


def _default_verification() -> dict[str, Any]:
    return {
        "focused_tests": {"status": "pending", "command": "", "result": ""},
        "architecture_budget": {"status": "pending", "command": "", "result": ""},
        "full_offline_suite": {"status": "pending", "command": "", "result": ""},
        "coverage_note": "Pending final Phase 10 verification.",
    }


def _verification_complete(verification: Mapping[str, Any]) -> bool:
    required = ("focused_tests", "architecture_budget", "full_offline_suite")
    return all(
        isinstance(verification.get(key), Mapping)
        and str((verification.get(key) or {}).get("status") or "").casefold() == "passed"
        for key in required
    )


def _contains_personal_paths(value: Any) -> bool:
    rendered = _canonical_json(value)
    return bool(_LOCAL_HOME_RE.search(rendered) or _PRIVATE_PAI_RE.search(rendered))


def _secret_fields_redacted(value: Any, *, key: str = "") -> bool:
    if isinstance(value, Mapping):
        return all(_secret_fields_redacted(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, list):
        return all(_secret_fields_redacted(item, key=key) for item in value)
    if isinstance(value, str) and _is_secret_key(key):
        return value in {"<password:redacted>", "<base64_blob>"} or value.startswith("<")
    return True


def _artifact_hashes_valid(records: Sequence[Mapping[str, Any]]) -> bool:
    return all(
        str(record.get("embedded_sha256") or "") == _payload_hash(record.get("payload"))
        for record in records
    )


def validate_bundle(bundle: Mapping[str, Any], *, repo_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else _repo_root()
    records = bundle.get("embedded_artifacts") if isinstance(bundle.get("embedded_artifacts"), list) else []
    phase9 = _phase_report(records, "phase9")
    readiness = phase9.get("readiness") if isinstance(phase9.get("readiness"), Mapping) else {}
    campaign = bundle.get("supervised_campaign") if isinstance(bundle.get("supervised_campaign"), Mapping) else {}
    evidence_map = bundle.get("evidence_map") if isinstance(bundle.get("evidence_map"), Mapping) else {}
    bundle_without_hash = dict(bundle)
    bundle_without_hash.pop("bundle_hash", None)
    checks = {
        "kind_and_schema_valid": (
            bundle.get("kind") == BUNDLE_KIND
            and bundle.get("schema_version") == BUNDLE_SCHEMA_VERSION
        ),
        "base_head_present": bool(((bundle.get("bundle_metadata") or {}).get("patch_stack") or {}).get("base_head")),
        "all_required_evidence_map_keys_present": all(key in evidence_map for key in REQUIRED_EVIDENCE_MAP_KEYS),
        "all_embedded_artifact_hashes_valid": _artifact_hashes_valid(records),
        "no_embedded_artifact_parse_errors": all(not str(record.get("parse_error") or "") for record in records),
        "trajectory_v2_provenance_present": (
            ((bundle.get("trajectory_provenance") or {}).get("schema_v2_record_count") or 0) > 0
        ),
        "all_result_jsonl_artifacts_embedded": (
            ((bundle.get("attempt_coverage") or {}).get("all_discovered_jsonl_result_artifacts_embedded") is True)
        ),
        "phase9_negative_readiness_preserved": (
            readiness.get("readiness_decision") == "auto_harness_not_ready"
        ),
        "campaign_not_claimed_after_negative_readiness": (
            campaign.get("status") == "not_started"
            and campaign.get("eligibility_status") == "auto_harness_not_ready"
            and not campaign.get("candidate_hypotheses")
            and not campaign.get("promotion_packet")
        ),
        "status_vocabulary_complete": set(PHASE10_REQUIRED_STATUSES).issubset(
            set(bundle.get("status_vocabulary") or [])
        ),
        "personal_absolute_paths_removed": not _contains_personal_paths(bundle),
        "secret_fields_redacted": _secret_fields_redacted(bundle),
        "redaction_is_idempotent": _sanitize_value(bundle, root=root) == bundle,
        "verification_complete": _verification_complete(bundle.get("verification") or {}),
        "bundle_hash_valid": bundle.get("bundle_hash") == content_hash(bundle_without_hash),
    }
    return {
        "checks": checks,
        "passes_gate": all(checks.values()),
        "failed_checks": [key for key, value in checks.items() if not value],
    }


def build_phase10_evidence_bundle(
    *,
    repo_root: str | Path | None = None,
    source_head: str | None = None,
    generated_at: str | None = None,
    verification: Mapping[str, Any] | None = None,
    plan_artifacts: Sequence[str | Path] | None = None,
    result_artifacts: Sequence[str | Path] | None = None,
    hillclimb_manifests: Sequence[str | Path] | None = None,
    transitions_path: str | Path | None = None,
    patch_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else _repo_root()
    head = str(source_head if source_head is not None else _source_head(root)).strip()
    plan_paths = [Path(path) for path in plan_artifacts] if plan_artifacts is not None else _discover_plan_artifacts(root)
    result_paths = [Path(path) for path in result_artifacts] if result_artifacts is not None else _discover_result_artifacts(root)
    manifest_paths = [Path(path) for path in hillclimb_manifests] if hillclimb_manifests is not None else _discover_hillclimb_manifests(root)
    transition = Path(transitions_path) if transitions_path is not None else root / DEFAULT_TRANSITIONS_PATH.relative_to(_repo_root())
    artifact_paths = _unique_paths([*plan_paths, *result_paths, *manifest_paths, *([transition] if transition.exists() else [])])
    records = [_artifact_record(path, root=root) for path in artifact_paths if path.exists() and path.is_file()]
    resume_excerpt = _resume_excerpt_record(root)
    if resume_excerpt is not None:
        records.append(resume_excerpt)
    records = sorted(records, key=lambda item: str(item.get("path") or ""))

    phase6 = _phase_report(records, "phase6")
    phase7 = _phase_report(records, "phase7")
    phase8 = _phase_report(records, "phase8")
    phase9 = _phase_report(records, "phase9")
    readiness = phase9.get("readiness") if isinstance(phase9.get("readiness"), Mapping) else {}
    patch = _sanitize_value(
        dict(patch_state) if patch_state is not None else _patch_state(root, source_head=head),
        root=root,
    )
    verification_payload = _sanitize_value(
        dict(verification) if verification is not None else _default_verification(),
        root=root,
    )
    status_occurrences = _status_occurrences(phase8, phase9)
    bundle: dict[str, Any] = {
        "kind": BUNDLE_KIND,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_at": str(generated_at or _utc_now()),
        "bundle_metadata": {
            "program": "sage-architecture-policy-eval-completion",
            "phase": "phase10",
            "source_plan": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md",
            "patch_stack": patch,
            "redaction_policy": {
                "secret_redaction": "trajectory.schema.redact_text plus secret-key replacement",
                "portable_path_redaction": "repo paths become <repo-root>; personal home and private PAI memory paths are removed",
                "hash_preservation": "sha256-prefixed evidence hashes and hash-key values remain intact",
                "omitted_artifacts": [
                    "raw runtime databases",
                    "raw .log files",
                    "credential-bearing local env files",
                    "ephemeral architecture-token files",
                ],
            },
            "trust_boundary": [
                "This bundle proves retained local artifact consistency and decision lineage; it does not prove external source authenticity.",
                "Raw runtime databases and logs are intentionally excluded from the portable artifact because they are not needed for the frozen claim set and can contain local or secret material.",
                "Negative evidence remains negative: missing T1/T2 readiness evidence is represented as absent or failed, never reconstructed.",
            ],
        },
        "architecture_approvals": [
            {
                "scope": "phase1-through-phase10-controlled-tranches",
                "state": "approved_by_explicit_goal",
                "source": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md",
                "note": "The controlling goal authorizes short-lived scoped edit tokens for work required inside the selected phase.",
            },
            {
                "scope": "hybrid-product-default-narrow-follow-up",
                "state": "approved_and_implemented_uncommitted",
                "source": "Plans/RESUME.md#phase0-phase9-excerpt",
                "note": "The operator separately approved the narrow Hybrid default adoption after Phase 8.",
            },
            {
                "scope": "phase10-report-only-token",
                "state": "ephemeral_token_opened_not_embedded",
                "source": "bundle_metadata.patch_stack",
                "note": "The Phase 10 generator is report-only; the temporary gate token is intentionally not portable evidence.",
            },
        ],
        "embedded_artifacts": records,
        "evidence_map": _evidence_map(),
        "attempt_coverage": _attempt_coverage(records),
        "trajectory_provenance": _transition_provenance(records),
        "lineage_summary": _lineage_summary(records),
        "readiness": {
            "dense_reward_version": (((phase9.get("frozen_gate_record") or {}).get("reward_version")) if isinstance(phase9.get("frozen_gate_record"), Mapping) else ""),
            "typed_verdict": readiness.get("readiness_decision", ""),
            "failed_prerequisites": list(readiness.get("failed_prerequisites") or []),
            "t0": phase9.get("t0", {}),
            "t2_anchor": phase9.get("t2_anchor", {}),
            "canaries": phase9.get("canaries", {}),
            "limitations": [
                "T0 is triage-only for the current structural selector mechanism.",
                "No matching operator-returned paired T1 evidence exists for retrieval-ranking.",
                "No T2 rank correlation, bootstrap interval, MDE, or power claim can qualify without the missing T1 substrate and pairing.",
            ],
        },
        "live_matrices_and_reliability": {
            "phase6_laps_holdout": {
                "passes_gate": phase6.get("passes_gate") is True,
                "authorization": phase6.get("authorization", {}),
                "policy_summaries": phase6.get("policy_summaries", {}),
                "source": PHASE_REPORT_PATHS["phase6"],
            },
            "phase7_trust_context_corroboration": {
                "passes_gate": phase7.get("passes_gate") is True,
                "authorization": phase7.get("authorization", {}),
                "matching_row_count": phase7.get("matching_row_count"),
                "source": PHASE_REPORT_PATHS["phase7"],
            },
            "phase8_goad_regression": {
                "passes_gate": phase8.get("passes_gate") is True,
                "authorization": phase8.get("authorization", {}),
                "policy_summaries": phase8.get("policy_summaries", {}),
                "recommendation": phase8.get("recommendation", {}),
                "source": PHASE_REPORT_PATHS["phase8"],
            },
        },
        "supervised_campaign": {
            "status": "not_started",
            "eligibility_status": readiness.get("readiness_decision", ""),
            "candidate_hypotheses": [],
            "candidate_hashes": [],
            "costs": [],
            "dispositions": [],
            "promotion_packet": None,
            "reason": "Phase 9 emitted auto_harness_not_ready, which stops the campaign path.",
        },
        "superseding_assessment": _superseding_assessment(),
        "status_vocabulary": list(PHASE10_REQUIRED_STATUSES),
        "status_occurrences": status_occurrences,
        "final_dispositions": {
            "program": {
                "status": "eligible_pending_review_and_commit",
                "reason": "Phases 0-10 are packaged as one uncommitted evidence-backed patch stack for operator review.",
            },
            "product_default": {
                "current_default": "hybrid",
                "implementation_state": "implemented_uncommitted",
                "phase8_gate_status": "hybrid_default_recommended_pending_operator_approval",
                "operator_approval_recorded": True,
                "rollback": "Set the centralized policy default back to symbolic or explicitly configure policy_mode=symbolic.",
            },
            "research_candidates": {
                "modeled_reachability_selector": {
                    "state": "eval_only_not_runtime_promoted",
                    "evidence": [
                        "Payload_Type/sage/.hillclimb/results/policy_replay_hillclimb_iteration_20260713.json",
                        "Payload_Type/sage/.hillclimb/results/policy_replay_promotion_gate_20260713.json",
                    ],
                },
                "retrieval_ranking_auto_harness": {
                    "status": "auto_harness_not_ready",
                    "next_experiment": readiness.get("cheapest_decisive_next_experiment", ""),
                },
            },
            "exact_next_step": (
                "Operator review and commit of the accumulated Phase 0-10 patch stack. "
                "If auto-harness improvement is reopened later, start a focused follow-on plan only after "
                "choosing a valid structural T1 family or a narrower lexical/configuration surface."
            ),
        },
        "verification": verification_payload,
    }
    bundle["bundle_hash"] = content_hash(bundle)
    validation = validate_bundle(bundle, repo_root=root)
    bundle["validation"] = validation
    bundle_without_hash = dict(bundle)
    bundle_without_hash.pop("bundle_hash", None)
    bundle["bundle_hash"] = content_hash(bundle_without_hash)
    bundle["validation"] = validate_bundle(bundle, repo_root=root)
    return bundle


def render_bundle(bundle: Mapping[str, Any]) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=True)


def _load_verification(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    return _read_json(Path(path))


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase10-evidence-bundle",
        help="build the Phase 10 redacted portable evidence bundle from frozen artifacts",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="portable JSON bundle output path")
    parser.add_argument("--verification-record", default=None, help="optional JSON file with final Phase 10 verification results")
    parser.add_argument("--generated-at", default=None, help="optional fixed UTC timestamp for reproducible bundle rebuilds")
    parser.set_defaults(func=_cmd_phase10_evidence_bundle)


def _cmd_phase10_evidence_bundle(args: Any) -> int:
    try:
        verification = _load_verification(args.verification_record)
        bundle = build_phase10_evidence_bundle(verification=verification, generated_at=args.generated_at)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_bundle(bundle) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"phase10-evidence-bundle: {exc}", file=sys.stderr)
        return 2
    validation = bundle["validation"]
    summary = {
        "kind": bundle["kind"],
        "bundle_hash": bundle["bundle_hash"],
        "embedded_artifact_count": len(bundle["embedded_artifacts"]),
        "result_jsonl_row_count": bundle["lineage_summary"]["result_jsonl_row_count"],
        "readiness": bundle["readiness"]["typed_verdict"],
        "program_disposition": bundle["final_dispositions"]["program"]["status"],
        "passes_gate": validation["passes_gate"],
        "failed_checks": validation["failed_checks"],
        "output": _portable_path(output, _repo_root()),
    }
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True))
    print(
        f"\nVERDICT: {'PASS' if validation['passes_gate'] else 'FAIL'}  "
        f"(bundle_hash={bundle['bundle_hash']}, readiness={bundle['readiness']['typed_verdict']})",
        flush=True,
    )
    return 0 if validation["passes_gate"] else 1
