"""Positive-allowlist evaluator views; full operator artifacts stay outside."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

EVALUATOR_EVIDENCE_SCHEMA = "evaluator-evidence-projection-v1"
RUNTIME_EVALUATOR_VIEW_SCHEMA = "runtime-evaluator-view-v1"

_RISK_FIELDS = "boundary_violation_count secret_exposure_count lab_literal_count unauthorized_live_request_count active_runtime_config_mutation_count".split()
_DERIVED_BOOL_FIELDS = "objective_proven clean_stop proof_provenance_complete artifact_integrity_passed policy_identity_passed backend_provenance_passed".split()
_PROOF_TEXT_FIELDS = "schema scope persistence_state origin engagement_id callback_id transaction_id task_id terminal_status verifier_id verifier_input_sha256 verifier_result_sha256".split()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def projection_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def project_identifier(value: Any, *, casefold: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    return text.casefold() if casefold else text


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) and value >= 0 else None


def _strict_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _first_nonnegative_int(source: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        value = _nonnegative_int(source.get(name))
        if value is not None:
            return value
    return None


def _project_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    trace: dict[str, Any] = {}
    integer_fields = {
        "verified_hop_count": _first_nonnegative_int(row, "verified_hop_count"),
        "repeated_ineffective_work_count": _first_nonnegative_int(row, "repeated_ineffective_work_count", "repeated_work_count"),
        "wait_count": _first_nonnegative_int(row, "wait_count"),
        "retry_count": _first_nonnegative_int(row, "retry_count"),
        "child_task_count": _first_nonnegative_int(row, "child_task_count", "task_count"),
        "total_tokens": _first_nonnegative_int(row, "total_tokens", "model_tokens"),
        "provider_failure_count": _first_nonnegative_int(row, "provider_failure_count"),
    }
    for name in _RISK_FIELDS:
        integer_fields[name] = _nonnegative_int(row.get(name))
    for name, value in integer_fields.items():
        if value is not None:
            trace[name] = value
    for name in ("wall_seconds", "monetary_cost_usd", "complexity_cost", "portability_score"):
        value = _finite_number(row.get(name))
        if value is not None:
            trace[name] = value
    for name in ("correct_replan", "bounded_wait_retry"):
        value = _strict_bool(row.get(name))
        if value is not None:
            trace[name] = value
    return trace


def _project_derived(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    outcome_source = project_identifier(source.get("outcome_source"))
    if outcome_source:
        projected["outcome_source"] = outcome_source
    for name in _DERIVED_BOOL_FIELDS:
        item = _strict_bool(source.get(name))
        if item is not None:
            projected[name] = item
    return projected


def _project_proof(value: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for name in _PROOF_TEXT_FIELDS:
        raw_value = value.get(name)
        if name.endswith("_sha256") and not isinstance(raw_value, str):
            continue
        item = project_identifier(raw_value, casefold=name in {"scope", "persistence_state", "origin", "terminal_status"})
        if item:
            projected[name] = item
    return projected


def _project_proofs(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        projected = _project_proof(value)
        if not projected:
            continue
        unique[_canonical_json(projected)] = projected
    return [unique[key] for key in sorted(unique)]


def project_canonical_evidence(
    row: Mapping[str, Any] | None,
    *,
    row_id: Any,
    policy_arm: Any,
    surface_id: Any,
    family_id: Any,
    derived_outcome: Mapping[str, Any] | None,
    proof_envelopes: Iterable[Mapping[str, Any]] = (),
    semantic_transaction_count: Any = None,
    model_owned_branch_observed: Any = None,
) -> dict[str, Any]:
    source = row if isinstance(row, Mapping) else {}
    transaction_value = source.get("semantic_transaction_count") if semantic_transaction_count is None else semantic_transaction_count
    transaction_count = _nonnegative_int(transaction_value)
    model_branch = _strict_bool(model_owned_branch_observed)
    projection = {
        "schema": EVALUATOR_EVIDENCE_SCHEMA,
        "identity": {
            "row_id": project_identifier(row_id),
            "policy_arm": project_identifier(policy_arm, casefold=True),
            "surface_id": project_identifier(surface_id),
            "family_id": project_identifier(family_id),
        },
        "trace": _project_trace(source),
        "derived_outcome": _project_derived(derived_outcome),
        "proof_envelopes": _project_proofs(proof_envelopes),
        "semantic_transaction_count": transaction_count,
        "model_owned_branch_observed": model_branch,
    }
    return {**projection, "projection_sha256": projection_sha256(projection)}


def project_runtime_telemetry(telemetry: Mapping[str, Any] | None) -> dict[str, Any]:
    source = telemetry if isinstance(telemetry, Mapping) else {}
    summary: dict[str, Any] = {}
    for name in ("episode_id", "policy_mode", "configured_policy_mode", "model_provider", "model_id", "controller_status"):
        item = project_identifier(source.get(name))
        if item:
            summary[name] = item
    for name in (
        "model_calls", "controller_cycle_count", "semantic_transaction_count", "authorized_transaction_count",
        "branch_opportunity_count", "model_owned_decision_count", "kernel_singleton_count", "causally_decisive_decision_count",
    ):
        item = _nonnegative_int(source.get(name))
        if item is not None:
            summary[name] = item
    for name in ("policy_identity_valid", "backend_provenance_complete", "objective_recognized"):
        item = _strict_bool(source.get(name))
        if item is not None:
            summary[name] = item
    for name in ("semantic_policy_coverage", "model_branch_coverage"):
        item = _finite_number(source.get(name))
        if item is not None:
            summary[name] = item
    backends = source.get("effective_backends")
    items = backends if isinstance(backends, (list, tuple)) else ()
    summary["effective_backends"] = sorted(filter(None, (project_identifier(item) for item in items)))
    for source_name, count_name in (
        ("decisions", "decision_record_count"),
        ("transactions", "transaction_record_count"),
        ("proof_lineage", "proof_lineage_record_count"), ("effective_backend_requests", "backend_request_record_count"),
    ):
        values = source.get(source_name)
        if isinstance(values, list):
            summary[count_name] = sum(isinstance(item, Mapping) for item in values)
    projection = {"schema": RUNTIME_EVALUATOR_VIEW_SCHEMA, "runtime_telemetry": summary}
    return {**projection, "projection_sha256": projection_sha256(projection)}
