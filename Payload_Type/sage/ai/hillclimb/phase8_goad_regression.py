"""Phase 8 GOAD regression and Hybrid-default recommendation validator.

This module is eval-only.  It consumes one dedicated append-only JSONL artifact
for the Phase 8 GOAD rows plus the accepted Phase 6 and Phase 7 reports.  It
does not run the lab, mutate policy defaults, or infer a recommendation from
historical ``cross-forest-objective`` rows that were not explicitly tagged for
this frozen tranche.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

try:  # package import
    from . import policy_replay_calibration as calibration
    from . import fitness
    from .experiment_contracts import TypedVerdict
    from ..langgraph import proof_boundary
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import policy_replay_calibration as calibration  # type: ignore
    import fitness  # type: ignore
    from experiment_contracts import TypedVerdict  # type: ignore
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import proof_boundary  # type: ignore


SCHEMA_VERSION = 2
CONTRACT_NAME = "sage-phase8-goad-regression-v2"
SCENARIO_NAME = "cross-forest-objective"
EXPECTED_POLICY_ARMS = ("symbolic", "hybrid")
OPTIONAL_POLICY_ARMS = ("llm",)
EXPECTED_SEEDS_PER_POLICY = 5
MAX_LLM_CANARY_ROWS = 1
MAX_PREFRONTIER_DIAGNOSTIC_RETRIES = 2
COLLECTION_CAPABILITY = "collect-graph"
RECOMMENDATION = "hybrid_default_recommended_pending_operator_approval"
REJECTION = "symbolic_temporary_default_hybrid_explicit_experimental"
SUPERSEDING_INVALIDATION = "hybrid_default_recommendation_invalidated_pending_fresh_evidence"
PHASE8_SURFACE_ID = "phase8-goad-regression"
PHASE8_FAMILY_ID = "goad-cross-forest-objective"
DEFAULT_RESULTS_PATH = calibration.DEFAULT_RESULTS_ROOT / "phase8_goad_regression_rows_v2_20260715.jsonl"
DEFAULT_PHASE6_REPORT_PATH = (
    calibration.DEFAULT_RESULTS_ROOT / "laps_family_transfer_matrix_validation_r5_20260715.json"
)
DEFAULT_PHASE7_REPORT_PATH = (
    calibration.DEFAULT_RESULTS_ROOT / "trust_context_corroboration_live_validation_v2_20260715.json"
)
EXPECTED_PHASE6_REPORT_SHA256 = "sha256:c5d2a947fc1ffe97b21e9c1e3b3c25055e450354306f9d3b83e3ea6a667324fe"
EXPECTED_PHASE7_REPORT_SHA256 = "sha256:8ebbe0d89c9901e8dd9b3bc2302092cfa40a2d88ce749850918f7203929e848e"
LIMITATIONS = (
    "Phase 8 proves GOAD regression reliability and recommendation readiness, not universal AD-range transfer.",
    "The independent causal model-contribution claim remains limited to the sealed Phase 6 LAPS discriminator.",
    "GOAD Hybrid rows may remain kernel-only when the live admissible frontier has no true branch opportunity; those rows are reliability evidence, not new causal model-contribution evidence.",
    "The trust/context transfer claim remains limited to the sealed Phase 7 child/parent versus direct-forest corroboration.",
)
ROLLBACK_PATH = (
    "Keep Symbolic as the configured temporary default and require explicit SAGE_POLICY_MODE=hybrid opt-in "
    "until an operator separately approves any product-default change."
)
_STABLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class Phase8GoadRegressionError(ValueError):
    """Raised when Phase 8 evidence is missing or malformed."""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _portable_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(calibration.DEFAULT_RESULTS_ROOT.resolve()))
    except ValueError:
        return path.name


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase8GoadRegressionError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase8GoadRegressionError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Phase8GoadRegressionError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise Phase8GoadRegressionError(f"missing {label}: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Phase8GoadRegressionError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise Phase8GoadRegressionError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise Phase8GoadRegressionError(f"{label} has no rows: {path}")
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def planned_row_id(policy_arm: str, seed_index: int) -> str:
    arm = str(policy_arm or "").strip().casefold()
    if arm == "llm":
        return "phase8-llm-canary-1"
    return f"phase8-{arm}-seed-{int(seed_index)}"


def sealed_manifest_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "phase8_goad_regression_manifest",
        "contract_name": CONTRACT_NAME,
        "scenario": SCENARIO_NAME,
        "policy_arms": {
            "required": list(EXPECTED_POLICY_ARMS),
            "optional": list(OPTIONAL_POLICY_ARMS),
        },
        "budgets": {
            "required_seeds_per_policy": EXPECTED_SEEDS_PER_POLICY,
            "max_llm_canary_rows": MAX_LLM_CANARY_ROWS,
            "max_prefrontier_diagnostic_retries_per_planned_row": MAX_PREFRONTIER_DIAGNOSTIC_RETRIES,
        },
        "prerequisites": {
            "phase6_report_sha256": EXPECTED_PHASE6_REPORT_SHA256,
            "phase7_report_sha256": EXPECTED_PHASE7_REPORT_SHA256,
        },
        "countable_requirements": {
            "complete_reset_per_seed": True,
            "fresh_locked_channel_per_seed": True,
            "stock_generic_objective": SCENARIO_NAME,
            "objective_proof_and_clean_stop_per_required_row": True,
            "policy_and_backend_identity": True,
            "kernel_only_hybrid_goad_rows_allowed_only_when_no_true_branch_opportunity_and_phase6_causal_vignette_passes": True,
            "complete_runtime_lineage": True,
            "zero_off_agent_proof": True,
            "append_only_attempt_retention": True,
        },
        "recommendation": RECOMMENDATION,
        "rollback_path": ROLLBACK_PATH,
        "limitations": list(LIMITATIONS),
    }


def sealed_manifest() -> dict[str, Any]:
    payload = sealed_manifest_payload()
    return {**payload, "manifest_hash": _sha256(payload)}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _expected_planned_ids() -> set[str]:
    return {
        planned_row_id(policy_arm, seed_index)
        for policy_arm in EXPECTED_POLICY_ARMS
        for seed_index in range(1, EXPECTED_SEEDS_PER_POLICY + 1)
    }


def _allowed_planned_ids() -> set[str]:
    return {*_expected_planned_ids(), planned_row_id("llm", 1)}


def expected_planned_row_ids() -> set[str]:
    return _expected_planned_ids()


def allowed_planned_row_ids() -> set[str]:
    return _allowed_planned_ids()


def _matching_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest_hash = sealed_manifest()["manifest_hash"]
    return [
        row
        for row in rows
        if str(row.get("side") or "") == "harness"
        and str(row.get("scenario") or "") == SCENARIO_NAME
        and str(row.get("phase8_contract_hash") or "") == manifest_hash
    ]


def _branch_surface_observed(row: dict[str, Any]) -> bool:
    for decision in list(row.get("decisions") or []):
        if not isinstance(decision, dict):
            continue
        capability = str(decision.get("selected_capability") or "").strip().casefold()
        if capability and capability != COLLECTION_CAPABILITY:
            return True
    return False


def _attempt_report(row: dict[str, Any]) -> dict[str, Any]:
    policy_arm = str(row.get("phase8_policy_arm") or "").strip().casefold()
    planned_id = str(row.get("phase8_planned_row_id") or "").strip()
    attempt_index = _safe_int(row.get("phase8_attempt_index"))
    countable = _branch_surface_observed(row)
    expected_id = None
    if policy_arm in EXPECTED_POLICY_ARMS and isinstance(attempt_index, int):
        seed_index = _safe_int(re.sub(r"^phase8-[^-]+-seed-", "", planned_id))
        expected_id = planned_row_id(policy_arm, seed_index) if seed_index is not None else None
    elif policy_arm == "llm":
        expected_id = planned_row_id("llm", 1)
    checks = {
        "contract_hash_matches": str(row.get("phase8_contract_hash") or "") == sealed_manifest()["manifest_hash"],
        "policy_arm_allowed": policy_arm in {*EXPECTED_POLICY_ARMS, *OPTIONAL_POLICY_ARMS},
        "planned_row_id_is_stable": bool(planned_id) and bool(_STABLE_TOKEN_RE.fullmatch(planned_id)),
        "planned_row_id_allowed": planned_id in _allowed_planned_ids(),
        "planned_row_id_matches_policy_arm": expected_id == planned_id,
        "attempt_index_present": isinstance(attempt_index, int) and attempt_index >= 1,
    }
    return {
        "policy_arm": policy_arm or None,
        "planned_row_id": planned_id or None,
        "attempt_index": attempt_index,
        "countable": countable,
        "pre_frontier_diagnostic": not countable,
        "request_completed": row.get("request_completed"),
        "objective_proven": row.get("objective_proven"),
        "clean_stop": row.get("clean_stop"),
        "controller_status": row.get("controller_status"),
        "controller_terminal_reason": row.get("controller_terminal_reason"),
        "checks": checks,
        "passes_metadata_gate": all(checks.values()),
    }


def select_countable_attempt_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bool]]:
    reports = [_attempt_report(row) for row in rows]
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    metadata_complete = True
    for row, report in zip(rows, reports):
        planned_id = str(report.get("planned_row_id") or "")
        attempt_index = report.get("attempt_index")
        if not report["passes_metadata_gate"] or not planned_id or not isinstance(attempt_index, int):
            metadata_complete = False
            continue
        groups.setdefault(planned_id, []).append((row, report))

    selected: list[dict[str, Any]] = []
    attempt_indices_contiguous = True
    diagnostic_budget_respected = True
    countable_unique = True
    no_attempt_after_countable = True
    for planned_id, entries in groups.items():
        del planned_id
        ordered = sorted(entries, key=lambda item: int(item[1]["attempt_index"]))
        indices = [int(report["attempt_index"]) for _, report in ordered]
        if indices != list(range(1, len(indices) + 1)):
            attempt_indices_contiguous = False
        countable_entries = [(row, report) for row, report in ordered if report["countable"]]
        if len(countable_entries) != 1:
            countable_unique = False
        if countable_entries:
            selected.append(countable_entries[0][0])
            countable_index = int(countable_entries[0][1]["attempt_index"])
            if any(int(report["attempt_index"]) > countable_index for _, report in ordered):
                no_attempt_after_countable = False
            if sum(1 for _, report in ordered if report["pre_frontier_diagnostic"]) > MAX_PREFRONTIER_DIAGNOSTIC_RETRIES:
                diagnostic_budget_respected = False

    checks = {
        "attempt_metadata_complete": metadata_complete and bool(reports),
        "attempt_indices_are_contiguous": attempt_indices_contiguous,
        "pre_frontier_diagnostic_retry_budget_respected": diagnostic_budget_respected,
        "countable_attempt_is_unique_per_planned_row": countable_unique,
        "no_attempt_after_countable_attempt": no_attempt_after_countable,
    }
    return selected, reports, checks


def _lineage_report(row: dict[str, Any]) -> dict[str, Any]:
    decisions = [item for item in list(row.get("decisions") or []) if isinstance(item, dict)]
    transactions = [item for item in list(row.get("transactions") or []) if isinstance(item, dict)]
    failures: list[str] = []
    decision_ids: list[str] = [str(item.get("decision_id") or "").strip() for item in decisions]
    transaction_ids: list[str] = [str(item.get("transaction_id") or "").strip() for item in transactions]
    decision_map = {decision_id: item for decision_id, item in zip(decision_ids, decisions) if decision_id}
    if not decisions:
        failures.append("missing_decisions")
    if not transactions:
        failures.append("missing_transactions")
    if any(not decision_id for decision_id in decision_ids):
        failures.append("decision_missing_id")
    if len(decision_map) != len(decision_ids):
        failures.append("duplicate_decision_id")
    if any(not str(item.get("decision_owner") or "").strip() for item in decisions):
        failures.append("decision_owner_missing")
    if any(not transaction_id for transaction_id in transaction_ids):
        failures.append("transaction_missing_id")
    if len({item for item in transaction_ids if item}) != len(transaction_ids):
        failures.append("duplicate_transaction_id")
    if set(decision_ids) != {str(item.get("decision_id") or "").strip() for item in transactions}:
        failures.append("decision_transaction_join_incomplete")

    proof_count = 0
    inadmissible_proof_count = 0
    for tx_index, transaction in enumerate(transactions, start=1):
        prefix = f"tx{tx_index}"
        transaction_id = str(transaction.get("transaction_id") or "").strip()
        decision_id = str(transaction.get("decision_id") or "").strip()
        if not decision_id or decision_id not in decision_map:
            failures.append(f"{prefix}:missing_decision_join")
        child_tasks = [item for item in list(transaction.get("child_tasks") or []) if isinstance(item, dict)]
        verifier_ids = [str(item or "").strip() for item in list(transaction.get("verifier_ids") or []) if str(item or "").strip()]
        proof_ids = [str(item or "").strip() for item in list(transaction.get("proof_envelope_ids") or []) if str(item or "").strip()]
        proof_lineage = [item for item in list(transaction.get("proof_lineage") or []) if isinstance(item, dict)]
        if not child_tasks:
            failures.append(f"{prefix}:missing_child_task")
        if not verifier_ids:
            failures.append(f"{prefix}:missing_verifier")
        if not proof_ids:
            failures.append(f"{prefix}:missing_proof")
        if not proof_lineage:
            failures.append(f"{prefix}:missing_proof_lineage")
        child_task_ids = {str(item.get("task_id") or "").strip() for item in child_tasks if str(item.get("task_id") or "").strip()}
        if len(child_task_ids) != len(child_tasks):
            failures.append(f"{prefix}:child_task_missing_or_duplicate_id")
        lineage_ids: set[str] = set()
        for proof in proof_lineage:
            proof_count += 1
            proof_id = str(proof.get("proof_envelope_id") or "").strip()
            lineage_ids.add(proof_id)
            origin = str(proof.get("origin") or "").strip().casefold()
            scope = str(proof.get("scope") or "").strip().casefold()
            admissible = proof.get("admissible_for_runtime_achievement") is True
            if (
                not proof_id
                or str(proof.get("transaction_id") or "").strip() != transaction_id
                or str(proof.get("task_id") or "").strip() not in child_task_ids
                or str(proof.get("verifier_id") or "").strip() not in set(verifier_ids)
            ):
                failures.append(f"{prefix}:proof_join_invalid")
            if (
                scope != proof_boundary.RUNTIME_SCOPE
                or origin not in proof_boundary.ALLOWED_RUNTIME_ORIGINS
                or not admissible
            ):
                inadmissible_proof_count += 1
                failures.append(f"{prefix}:inadmissible_runtime_proof")
        if set(proof_ids) != lineage_ids:
            failures.append(f"{prefix}:proof_lineage_set_mismatch")

    model_branch_decisions = [
        item for item in decisions if str(item.get("decision_owner") or "").strip() == "model_branch"
    ]
    kernel_singleton_decisions = [
        item for item in decisions if str(item.get("decision_owner") or "").strip() == "kernel_singleton"
    ]
    branch_opportunity_count = sum(
        int(value)
        for item in decisions
        if (value := _safe_int(item.get("branch_opportunity_count"))) is not None and value >= 0
    )
    attributed_model_branch_decisions = [
        item
        for item in model_branch_decisions
        if str(item.get("effective_backend") or "").strip()
        and str(item.get("backend_provenance_source") or "").strip() not in {"", "unavailable"}
        and str(item.get("decision_packet_hash") or "").strip().startswith("sha256:")
    ]
    checks = {
        "decision_transaction_lineage_complete": not failures,
        "zero_off_agent_proof": inadmissible_proof_count == 0,
        "decision_attribution_present": bool(decisions) and all(
            str(item.get("decision_owner") or "").strip() for item in decisions
        ),
        "model_branch_attribution_complete": len(model_branch_decisions) == len(attributed_model_branch_decisions),
    }
    return {
        "decision_count": len(decisions),
        "transaction_count": len(transactions),
        "proof_count": proof_count,
        "model_branch_decision_count": len(model_branch_decisions),
        "attributed_model_branch_decision_count": len(attributed_model_branch_decisions),
        "kernel_singleton_decision_count": len(kernel_singleton_decisions),
        "branch_opportunity_count": branch_opportunity_count,
        "inadmissible_proof_count": inadmissible_proof_count,
        "failures": sorted(set(failures)),
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _backend_report(row: dict[str, Any], policy_arm: str) -> dict[str, Any]:
    model_calls = _safe_int(row.get("model_calls"))
    model_calls = model_calls if model_calls is not None else 0
    effective_backends = [
        str(item or "").strip()
        for item in list(row.get("effective_backends") or [])
        if str(item or "").strip()
    ]
    requests = [item for item in list(row.get("effective_backend_requests") or []) if isinstance(item, dict)]
    requests_complete = all(
        str(item.get("effective_backend") or "").strip()
        and str(item.get("backend_provenance_source") or "").strip() not in {"", "unavailable"}
        for item in requests
    )
    if policy_arm == "symbolic":
        arm_specific_identity = model_calls == 0 and not effective_backends and not requests
    else:
        arm_specific_identity = len(requests) == model_calls and requests_complete
    checks = {
        "backend_provenance_complete": row.get("backend_provenance_complete") is True,
        "model_request_provenance_complete": len(requests) == model_calls and requests_complete,
        "policy_arm_backend_identity": arm_specific_identity,
    }
    return {
        "model_calls": model_calls,
        "effective_backends": sorted(set(effective_backends)),
        "effective_backend_request_count": len(requests),
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _row_report(row: dict[str, Any]) -> dict[str, Any]:
    policy_arm = str(row.get("phase8_policy_arm") or "").strip().casefold()
    lineage = _lineage_report(row)
    backend = _backend_report(row, policy_arm)
    invariant_failures = calibration._row_invariant_failures(row)
    checks = {
        "scenario_matches": str(row.get("scenario") or "") == SCENARIO_NAME,
        "side_is_harness": str(row.get("side") or "") == "harness",
        "contract_hash_matches": str(row.get("phase8_contract_hash") or "") == sealed_manifest()["manifest_hash"],
        "policy_arm_matches_configured_policy": policy_arm == str(row.get("configured_policy_mode") or "").strip().casefold(),
        "policy_arm_matches_observed_policy": policy_arm == str(row.get("policy_mode") or "").strip().casefold(),
        "row_invariants_pass": not invariant_failures,
        "request_completed": row.get("request_completed") is True,
        "fresh_channel_identifiers_present": (
            row.get("chat_channel_id") is not None and row.get("chat_request_id") is not None
        ),
        "complete_runtime_lineage": lineage["passes_gate"] is True,
        "backend_identity_complete": backend["passes_gate"] is True,
    }
    canonical_row_verdict = fitness.canonical_row_verdict(
        row,
        row_id=str(row.get("phase8_planned_row_id") or ""),
        policy_arm=policy_arm,
        surface_id=PHASE8_SURFACE_ID,
        family_id=PHASE8_FAMILY_ID,
        derived_outcome=(row.get("derived_outcome") if isinstance(row.get("derived_outcome"), dict) else None),
        proof_envelopes=(row.get("proof_envelopes") or ()),
        semantic_transaction_count=_safe_int(row.get("semantic_transaction_count")),
        model_owned_branch_observed=lineage["model_branch_decision_count"] > 0,
    )
    return {
        "policy_arm": policy_arm,
        "planned_row_id": row.get("phase8_planned_row_id"),
        "attempt_index": row.get("phase8_attempt_index"),
        "ts_iso": row.get("ts_iso"),
        "chat_channel_id": row.get("chat_channel_id"),
        "chat_request_id": row.get("chat_request_id"),
        "semantic_transaction_count": row.get("semantic_transaction_count"),
        "objective_proven": row.get("objective_proven"),
        "clean_stop": row.get("clean_stop"),
        "controller_terminal_reason": row.get("controller_terminal_reason"),
        "invariant_failures": invariant_failures,
        "lineage": lineage,
        "backend": backend,
        "canonical_row_verdict": canonical_row_verdict,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _phase6_prerequisite_report(report: dict[str, Any], artifact_sha256: str) -> dict[str, Any]:
    checks = dict(report.get("checks") or {})
    policy_summaries = dict(report.get("policy_summaries") or {})
    hybrid_summary = dict(policy_summaries.get("hybrid") or {})
    symbolic_summary = dict(policy_summaries.get("symbolic") or {})
    result_checks = {
        "artifact_hash_matches_accepted_phase6_report": artifact_sha256 == EXPECTED_PHASE6_REPORT_SHA256,
        "kind_matches": report.get("kind") == "laps_family_transfer_matrix_validation",
        "phase6_passes": report.get("passes_gate") is True,
        "phase6_complete": (report.get("authorization") or {}).get("phase6_complete") is True,
        "hybrid_selects_correct_first_in_all_six": checks.get("hybrid_selects_correct_first_in_all_six") is True,
        "hybrid_proves_and_clean_stops_in_all_six": checks.get("hybrid_proves_and_clean_stops_in_all_six") is True,
        "hybrid_has_lower_aggregate_regret_than_symbolic": checks.get("hybrid_has_lower_aggregate_regret_than_symbolic") is True,
        "hybrid_causal_branch_count_is_six": hybrid_summary.get("correct_first_count") == 6,
        "symbolic_control_exposes_regret": (
            isinstance(symbolic_summary.get("aggregate_regret"), (int, float))
            and isinstance(hybrid_summary.get("aggregate_regret"), (int, float))
            and float(symbolic_summary["aggregate_regret"]) > float(hybrid_summary["aggregate_regret"])
        ),
    }
    return {
        "artifact_sha256": artifact_sha256,
        "checks": result_checks,
        "passes_gate": all(result_checks.values()),
    }


def _phase7_prerequisite_report(report: dict[str, Any], artifact_sha256: str) -> dict[str, Any]:
    checks = dict(report.get("checks") or {})
    result_checks = {
        "artifact_hash_matches_accepted_phase7_report": artifact_sha256 == EXPECTED_PHASE7_REPORT_SHA256,
        "kind_matches": report.get("kind") == "trust_context_corroboration_live_validation",
        "phase7_passes": report.get("passes_gate") is True,
        "phase7_complete": (report.get("authorization") or {}).get("phase7_complete") is True,
        "all_rows_pass": checks.get("all_rows_pass") is True,
        "three_post_context_mythic_credential_proofs": checks.get("three_post_context_mythic_credential_proofs") is True,
        "zero_premature_parent_dcsync_exposure": checks.get("zero_premature_parent_dcsync_exposure") is True,
    }
    return {
        "artifact_sha256": artifact_sha256,
        "checks": result_checks,
        "passes_gate": all(result_checks.values()),
    }


def _artifact_record(path: Path | None, sha256: str | None) -> dict[str, Any] | None:
    if path is None and not sha256:
        return None
    return {
        "relative_path": _portable_path(path) if path is not None else None,
        "sha256": sha256,
    }


def _phase7_symbolic_surrogate_transfer_evidence(phase7_report: dict[str, Any]) -> dict[str, Any]:
    """Retain the historical Phase 7 relationship without misclassifying it as Hybrid transfer."""

    return {
        "evaluated_policy": "symbolic",
        "surface_id": "phase7-trust-context-corroboration",
        "held_out_family_id": "phase7-trust-context-corroboration",
        "source_family_ids": [PHASE8_FAMILY_ID],
        "row_verdict_hashes": [],
        "separately_held_out": True,
        "source_report_kind": phase7_report.get("kind"),
        "historical_scope": "symbolic_only_mechanics_context_corroboration",
    }


def validate_goad_regression_rows(
    rows: list[dict[str, Any]],
    phase6_report: dict[str, Any],
    phase7_report: dict[str, Any],
    *,
    results_source_path: Path | None = None,
    phase6_source_path: Path | None = None,
    phase7_source_path: Path | None = None,
    results_artifact_sha256: str | None = None,
    phase6_artifact_sha256: str | None = None,
    phase7_artifact_sha256: str | None = None,
    transfer_evidence: dict[str, Any] | None = None,
    claimed_hard_gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results_sha = results_artifact_sha256 or (_sha256_file(results_source_path) if results_source_path is not None else "")
    phase6_sha = phase6_artifact_sha256 or (_sha256_file(phase6_source_path) if phase6_source_path is not None else "")
    phase7_sha = phase7_artifact_sha256 or (_sha256_file(phase7_source_path) if phase7_source_path is not None else "")
    matching = _matching_rows(rows)
    countable_rows, attempt_reports, attempt_checks = select_countable_attempt_rows(matching)
    row_reports = [_row_report(row) for row in countable_rows]
    row_reports_by_arm: dict[str, list[dict[str, Any]]] = {
        arm: [report for report in row_reports if report["policy_arm"] == arm]
        for arm in (*EXPECTED_POLICY_ARMS, *OPTIONAL_POLICY_ARMS)
    }
    countable_planned_ids = {str(report.get("planned_row_id") or "") for report in row_reports}
    required_planned_ids = _expected_planned_ids()
    phase6 = _phase6_prerequisite_report(phase6_report, phase6_sha)
    phase7 = _phase7_prerequisite_report(phase7_report, phase7_sha)
    symbolic_reports = row_reports_by_arm["symbolic"]
    hybrid_reports = row_reports_by_arm["hybrid"]
    llm_reports = row_reports_by_arm["llm"]
    symbolic_costs = [
        int(report["semantic_transaction_count"])
        for report in symbolic_reports
        if isinstance(report.get("semantic_transaction_count"), int)
    ]
    hybrid_costs = [
        int(report["semantic_transaction_count"])
        for report in hybrid_reports
        if isinstance(report.get("semantic_transaction_count"), int)
    ]
    hybrid_model_branch_decisions = sum(
        int((report.get("lineage") or {}).get("model_branch_decision_count") or 0)
        for report in hybrid_reports
    )
    hybrid_attributed_model_branch_decisions = sum(
        int((report.get("lineage") or {}).get("attributed_model_branch_decision_count") or 0)
        for report in hybrid_reports
    )
    hybrid_kernel_singleton_decisions = sum(
        int((report.get("lineage") or {}).get("kernel_singleton_decision_count") or 0)
        for report in hybrid_reports
    )
    hybrid_branch_opportunity_count = sum(
        int((report.get("lineage") or {}).get("branch_opportunity_count") or 0)
        for report in hybrid_reports
    )
    hybrid_decision_count = sum(
        int((report.get("lineage") or {}).get("decision_count") or 0)
        for report in hybrid_reports
    )
    hybrid_model_call_count = sum(
        int((report.get("backend") or {}).get("model_calls") or 0)
        for report in hybrid_reports
    )
    hybrid_effective_backends = sorted({
        backend
        for report in hybrid_reports
        for backend in list((report.get("backend") or {}).get("effective_backends") or [])
        if backend
    })
    hybrid_goad_is_kernel_only_reliability = (
        len(hybrid_reports) == EXPECTED_SEEDS_PER_POLICY
        and hybrid_decision_count > 0
        and hybrid_branch_opportunity_count == 0
        and hybrid_model_branch_decisions == 0
        and hybrid_attributed_model_branch_decisions == 0
        and hybrid_model_call_count == 0
        and hybrid_kernel_singleton_decisions == hybrid_decision_count
        and not hybrid_effective_backends
    )
    hybrid_goad_has_attributed_model_branch = (
        hybrid_model_branch_decisions > 0
        and hybrid_attributed_model_branch_decisions == hybrid_model_branch_decisions
        and len(hybrid_effective_backends) == 1
    )
    hybrid_attribution_mode = (
        "goad_kernel_only_reliability_phase6_causal_vignette"
        if hybrid_goad_is_kernel_only_reliability
        else "goad_model_branch_attributed"
        if hybrid_goad_has_attributed_model_branch
        else "invalid_or_incomplete"
    )
    checks = {
        "dedicated_results_artifact_has_only_phase8_rows": len(rows) == len(matching),
        "results_artifact_hash_present": str(results_sha).startswith("sha256:"),
        "attempt_accounting_valid": all(attempt_checks.values()),
        "required_planned_rows_are_present": required_planned_ids.issubset(countable_planned_ids),
        "no_unplanned_countable_rows": countable_planned_ids.issubset(_allowed_planned_ids()),
        "exact_five_symbolic_rows": len(symbolic_reports) == EXPECTED_SEEDS_PER_POLICY,
        "exact_five_hybrid_rows": len(hybrid_reports) == EXPECTED_SEEDS_PER_POLICY,
        "llm_canary_budget_respected": len(llm_reports) <= MAX_LLM_CANARY_ROWS,
        "all_countable_rows_pass": bool(row_reports) and all(report["passes_gate"] is True for report in row_reports),
        "symbolic_five_of_five_objective_proof_and_clean_stop": (
            len(symbolic_reports) == EXPECTED_SEEDS_PER_POLICY
            and all(report["objective_proven"] is True and report["clean_stop"] is True for report in symbolic_reports)
        ),
        "hybrid_five_of_five_objective_proof_and_clean_stop": (
            len(hybrid_reports) == EXPECTED_SEEDS_PER_POLICY
            and all(report["objective_proven"] is True and report["clean_stop"] is True for report in hybrid_reports)
        ),
        "all_rows_have_complete_runtime_lineage": bool(row_reports) and all(
            (report.get("lineage") or {}).get("passes_gate") is True for report in row_reports
        ),
        "zero_off_agent_proof": bool(row_reports) and all(
            (report.get("lineage") or {}).get("checks", {}).get("zero_off_agent_proof") is True
            for report in row_reports
        ),
        "all_rows_have_policy_and_backend_identity": bool(row_reports) and all(
            (report.get("backend") or {}).get("passes_gate") is True
            and report["checks"].get("policy_arm_matches_configured_policy") is True
            and report["checks"].get("policy_arm_matches_observed_policy") is True
            for report in row_reports
        ),
        "hybrid_backend_identity_is_stable": (
            hybrid_goad_is_kernel_only_reliability or hybrid_goad_has_attributed_model_branch
        ),
        "conference_visible_hybrid_decision_attribution": (
            hybrid_goad_has_attributed_model_branch
            or (hybrid_goad_is_kernel_only_reliability and phase6["passes_gate"] is True)
        ),
        "phase6_causal_model_contribution_passes": phase6["passes_gate"] is True,
        "phase7_transfer_and_context_corroboration_passes": phase7["passes_gate"] is True,
        "hybrid_reliability_not_worse_than_symbolic": (
            len(hybrid_reports) == len(symbolic_reports) == EXPECTED_SEEDS_PER_POLICY
            and sum(1 for report in hybrid_reports if report["passes_gate"]) >= sum(
                1 for report in symbolic_reports if report["passes_gate"]
            )
        ),
        "hybrid_cost_not_worse_than_symbolic": (
            len(symbolic_costs) == len(hybrid_costs) == EXPECTED_SEEDS_PER_POLICY
            and sum(hybrid_costs) <= sum(symbolic_costs)
        ),
    }
    legacy_typed_verdict = TypedVerdict(
        artifact_integrity_passed=(
            checks["dedicated_results_artifact_has_only_phase8_rows"]
            and checks["results_artifact_hash_present"]
            and phase6["checks"]["artifact_hash_matches_accepted_phase6_report"]
            and phase7["checks"]["artifact_hash_matches_accepted_phase7_report"]
        ),
        boundary_passed=checks["all_rows_have_complete_runtime_lineage"] and checks["zero_off_agent_proof"],
        policy_identity_passed=checks["all_rows_have_policy_and_backend_identity"],
        causal_model_contribution_passed=checks["phase6_causal_model_contribution_passes"],
        backend_provenance_passed=(
            checks["all_rows_have_policy_and_backend_identity"]
            and checks["hybrid_backend_identity_is_stable"]
            and checks["conference_visible_hybrid_decision_attribution"]
        ),
        candidate_efficacy_passed=checks["hybrid_five_of_five_objective_proof_and_clean_stop"],
        non_regression_passed=(
            checks["symbolic_five_of_five_objective_proof_and_clean_stop"]
            and checks["hybrid_five_of_five_objective_proof_and_clean_stop"]
            and checks["hybrid_reliability_not_worse_than_symbolic"]
            and checks["hybrid_cost_not_worse_than_symbolic"]
        ),
        transfer_passed=checks["phase7_transfer_and_context_corroboration_passes"],
        reason_codes=tuple(name for name, passed in checks.items() if passed is not True),
    )
    explicit_transfer = dict(transfer_evidence or _phase7_symbolic_surrogate_transfer_evidence(phase7_report))
    transfer_rows = [
        dict(item)
        for item in list(explicit_transfer.pop("row_verdicts", []) or [])
        if isinstance(item, dict)
    ]
    canonical_aggregate = fitness.canonical_aggregate_verdict(
        [report["canonical_row_verdict"] for report in row_reports],
        evaluated_policy="hybrid",
        baseline_policy="symbolic",
        causal_row_verdicts=phase6_report.get("canonical_row_verdicts") or (),
        transfer_row_verdicts=transfer_rows,
        transfer_evidence=explicit_transfer,
        claimed_hard_gates=(
            claimed_hard_gates
            if claimed_hard_gates is not None
            else {
                "artifact_integrity": legacy_typed_verdict.artifact_integrity_passed,
                "boundary": legacy_typed_verdict.boundary_passed,
                "policy_identity": legacy_typed_verdict.policy_identity_passed,
                "backend_provenance": legacy_typed_verdict.backend_provenance_passed,
            }
        ),
        implementation_status="historical_phase8_replay_no_source_change",
    )
    typed_verdict = TypedVerdict.from_dict(canonical_aggregate["typed_verdict"])
    checks["canonical_row_verdicts_present"] = bool(row_reports)
    checks["canonical_aggregate_consumes_row_verdicts"] = (
        canonical_aggregate["row_verdict_hashes"]
        == [report["canonical_row_verdict"]["row_verdict_hash"] for report in row_reports]
    )
    checks["canonical_aggregate_promotion_passes"] = canonical_aggregate["promotion_evidence_passed"] is True
    passes_gate = all(checks.values()) and typed_verdict.promotion_evidence_passed
    return {
        "kind": "phase8_goad_regression_validation",
        "generated_at": _now(),
        "manifest": sealed_manifest(),
        "source_artifacts": {
            "results": _artifact_record(results_source_path, results_sha),
            "phase6_report": _artifact_record(phase6_source_path, phase6_sha),
            "phase7_report": _artifact_record(phase7_source_path, phase7_sha),
        },
        "matching_row_count": len(matching),
        "unmatched_row_count": len(rows) - len(matching),
        "attempt_accounting": {
            "matched_attempt_row_count": len(matching),
            "countable_row_count": len(countable_rows),
            "diagnostic_row_count": len(matching) - len(countable_rows),
            "checks": attempt_checks,
            "attempt_reports": attempt_reports,
        },
        "policy_summaries": {
            "symbolic": {
                "rows": len(symbolic_reports),
                "objective_proof_and_clean_stop_count": sum(
                    1 for report in symbolic_reports if report["objective_proven"] is True and report["clean_stop"] is True
                ),
                "semantic_transaction_counts": symbolic_costs,
                "aggregate_semantic_transaction_count": sum(symbolic_costs) if symbolic_costs else None,
            },
            "hybrid": {
                "rows": len(hybrid_reports),
                "objective_proof_and_clean_stop_count": sum(
                    1 for report in hybrid_reports if report["objective_proven"] is True and report["clean_stop"] is True
                ),
                "semantic_transaction_counts": hybrid_costs,
                "aggregate_semantic_transaction_count": sum(hybrid_costs) if hybrid_costs else None,
                "model_branch_decision_count": hybrid_model_branch_decisions,
                "attributed_model_branch_decision_count": hybrid_attributed_model_branch_decisions,
                "kernel_singleton_decision_count": hybrid_kernel_singleton_decisions,
                "branch_opportunity_count": hybrid_branch_opportunity_count,
                "model_call_count": hybrid_model_call_count,
                "effective_backends": hybrid_effective_backends,
                "attribution_mode": hybrid_attribution_mode,
            },
            "llm": {
                "rows": len(llm_reports),
            },
        },
        "row_reports": row_reports,
        "prerequisites": {
            "phase6": phase6,
            "phase7": phase7,
        },
        "checks": checks,
        "canonical_promotion": canonical_aggregate,
        "typed_verdict": typed_verdict.to_dict(),
        "passes_gate": passes_gate,
        "authorization": {
            "phase8_complete": passes_gate,
            "hybrid_default_recommendation_authorized": passes_gate,
            "product_default_changed": False,
        },
        "recommendation": {
            "disposition": RECOMMENDATION if passes_gate else "invalidated_pending_fresh_evidence",
            "decision": RECOMMENDATION if passes_gate else SUPERSEDING_INVALIDATION,
            "operator_approval_required": passes_gate,
            "reason": (
                "All Phase 8 GOAD regression, canonical row, transfer, provenance, and non-regression gates passed."
                if passes_gate
                else "The historical Phase 8 recommendation is invalidated because canonical row, transfer, proof, or promotion gates do not all pass."
            ),
            "limitations": list(LIMITATIONS),
            "rollback_path": ROLLBACK_PATH,
        },
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase8-goad-regression-validate",
        help="validate the Phase 8 Symbolic/Hybrid GOAD regression and recommendation gate",
    )
    parser.add_argument("--results", default=str(DEFAULT_RESULTS_PATH), help="dedicated Phase 8 JSONL results artifact")
    parser.add_argument("--phase6-report", default=str(DEFAULT_PHASE6_REPORT_PATH), help="accepted Phase 6 matrix report JSON")
    parser.add_argument("--phase7-report", default=str(DEFAULT_PHASE7_REPORT_PATH), help="accepted Phase 7 live report JSON")
    parser.add_argument("--output", default=None, help="optional JSON validation report path")
    parser.set_defaults(func=_cmd_phase8_goad_regression_validate)


def _cmd_phase8_goad_regression_validate(args: Any) -> int:
    results_path = Path(args.results)
    phase6_path = Path(args.phase6_report)
    phase7_path = Path(args.phase7_report)
    try:
        rows = _load_jsonl(results_path, label="Phase 8 results artifact")
        phase6_report = _load_json(phase6_path, label="Phase 6 report")
        phase7_report = _load_json(phase7_path, label="Phase 7 report")
        report = validate_goad_regression_rows(
            rows,
            phase6_report,
            phase7_report,
            results_source_path=results_path,
            phase6_source_path=phase6_path,
            phase7_source_path=phase7_path,
        )
    except Phase8GoadRegressionError as exc:
        print(f"phase8-goad-regression-validate: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(phase8_complete={report['authorization']['phase8_complete']}, "
        f"decision={report['recommendation']['decision']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
