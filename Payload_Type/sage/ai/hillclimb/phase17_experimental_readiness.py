"""Phase 17 experimental-readiness and countability attestation.

This module is intentionally conservative.  It re-checks the frozen Phase 16
surfaces, runs hermetic authorization/failure-classification canaries, and
records the exact live evidence still required before Phase 18 may unseal.  It
does not deploy a range, invoke a provider, create a Mythic task, or claim live
mechanics qualification when no live canary evidence has been supplied.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Mapping

try:  # package import
    from . import phase16_structural_benchmark_portfolio as phase16
    from ..langgraph import evaluation_authorization as auth
    from ..langgraph import evaluation_authorization_runtime as auth_runtime
    from ..langgraph import mythic_tools
    from ..langgraph.mythic_tools import MythicTools
except Exception:  # script / flat import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import phase16_structural_benchmark_portfolio as phase16  # type: ignore
    import evaluation_authorization as auth  # type: ignore
    import evaluation_authorization_runtime as auth_runtime  # type: ignore
    import mythic_tools  # type: ignore
    from mythic_tools import MythicTools  # type: ignore


KIND = "phase17_experimental_readiness_attestation"
SCHEMA_VERSION = 1
SOURCE_PLAN = "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md#6.11"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE17_EXPERIMENTAL_READINESS_ATTESTATION_2026-07-16.json"
)
DEFAULT_LIVE_EVIDENCE_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE17_LIVE_CANARY_EVIDENCE_2026-07-16.json"
)
PHASE16_ARTIFACT_PATHS = (
    phase16.DEFAULT_OUTPUT_PATH,
    phase16.DEFAULT_COVERAGE_OUTPUT_PATH,
    *phase16.DEFAULT_FAMILY_MANIFEST_PATHS.values(),
)
FROZEN_FAILURE_CLASSIFIER_VERSION = "phase17-failure-source-classifier-v1"
FROZEN_DENIAL_SCORING_VERSION = "phase17-authorization-safe-terminal-v1"

COUNTABLE_POLICY_FAILURE = "countable_policy_failure"
BURNED_SHARED_DEFECT = "burned_shared_defect"
BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT = "burned_authorization_or_measurement_defect"
BURNED_UNCLASSIFIED_DEFECT = "burned_unclassified_defect"

_SHARED_DEFECT_SOURCES = frozenset(
    {
        "shared_frontier_defect",
        "shared_setup_defect",
        "shared_normalization_defect",
        "shared_adapter_defect",
        "shared_gate_defect",
        "shared_measurement_defect",
    }
)
_AUTHORIZATION_OR_MEASUREMENT_DEFECT_SOURCES = frozenset(
    {
        "missing_authorization",
        "mismatched_authorization",
        "stale_authorization",
        "replayed_authorization",
        "unavailable_gate",
        "uncovered_effect_path",
        "unrepresentable_plan_valid_branch",
        "malformed_post_selection_envelope",
    }
)
_COUNTABLE_POLICY_SOURCES = frozenset(
    {
        "arm_valid_deny",
        "policy_origin_unknown",
        "invalid_policy_response",
        "invalid_policy_selection",
    }
)


def _utc_now() -> str:
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
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(DEFAULT_REPO_ROOT.resolve()).as_posix()
    except Exception:
        return path.name


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_with_sidecar(path: Path, payload: Mapping[str, Any]) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    digest = _file_sha256(path)
    sidecar = path.with_suffix(".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return {
        "path": _portable_path(path),
        "sha256": digest,
        "sidecar_path": _portable_path(sidecar),
    }


def _sidecar_digest(path: Path) -> str:
    sidecar = path.with_suffix(".sha256")
    if not sidecar.exists():
        return ""
    return sidecar.read_text(encoding="utf-8").strip().split()[0] if sidecar.read_text(encoding="utf-8").strip() else ""


def _phase16_artifact_integrity() -> dict[str, Any]:
    rows = []
    for path in PHASE16_ARTIFACT_PATHS:
        exists = path.exists() and path.is_file()
        actual_sha256 = _file_sha256(path) if exists else ""
        sidecar_sha256 = _sidecar_digest(path) if exists else ""
        rows.append(
            {
                "path": _portable_path(path),
                "exists": exists,
                "actual_sha256": actual_sha256,
                "sidecar_sha256": sidecar_sha256,
                "sidecar_matches": bool(actual_sha256 and actual_sha256 == sidecar_sha256),
            }
        )
    return {
        "rows": rows,
        "passes": all(row["exists"] and row["sidecar_matches"] for row in rows),
    }


def _surface_hashes() -> dict[str, Any]:
    paths = (
        DEFAULT_REPO_ROOT / "Payload_Type" / "sage" / "ai" / "langgraph" / "evaluation_authorization.py",
        DEFAULT_REPO_ROOT / "Payload_Type" / "sage" / "ai" / "langgraph" / "evaluation_authorization_runtime.py",
        DEFAULT_REPO_ROOT / "Payload_Type" / "sage" / "ai" / "langgraph" / "mythic_tools.py",
        DEFAULT_REPO_ROOT / "Payload_Type" / "sage" / "ai" / "langgraph" / "proof_boundary.py",
        DEFAULT_REPO_ROOT / "Payload_Type" / "sage" / "ai" / "langgraph" / "policy.py",
        DEFAULT_REPO_ROOT / "Payload_Type" / "sage" / "ai" / "hillclimb" / "phase16_structural_benchmark_portfolio.py",
    )
    rows = {
        _portable_path(path): f"sha256:{_file_sha256(path)}"
        for path in paths
        if path.exists() and path.is_file()
    }
    return {
        "rows": rows,
        "surface_bundle_hash": _content_hash(rows),
        "passes": len(rows) == len(paths),
    }


def _family_payloads() -> dict[str, dict[str, Any]]:
    return {
        family_id: _read_json(path)
        for family_id, path in phase16.DEFAULT_FAMILY_MANIFEST_PATHS.items()
    }


def _runtime_context(family_payload: Mapping[str, Any], *, callback_id: str = "42") -> tuple[dict[str, Any], auth.EvaluationAuthorizationManifest, auth.TrustedCellBinding]:
    manifest = auth.EvaluationAuthorizationManifest.from_dict(family_payload.get("authorization_manifest"))
    if manifest is None or not manifest.callbacks or not manifest.allowed_cells:
        raise ValueError("invalid family authorization manifest")
    selector = manifest.callbacks[0]
    callback = auth.CallbackSelector(
        callback_id=callback_id,
        host=selector.host,
        domain=selector.domain,
        identity=selector.identity,
    )
    binding = auth.TrustedCellBinding(
        cell_id=manifest.allowed_cells[0],
        cell_authorization_id=f"{family_payload.get('family_id')}-phase17-cell-auth",
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.sha256,
        engagement_id=manifest.engagement_id,
        callback=callback,
        issued_at="2026-07-16T00:00:00+00:00",
        expires_at="2026-09-01T00:00:00+00:00",
    )
    return (
        {
            "authorization_manifest": manifest.to_dict(),
            "trusted_cell_binding": binding.to_dict(),
        },
        manifest,
        binding,
    )


def _sample_enforcement(family_payload: Mapping[str, Any]) -> tuple[str, tuple[str, ...], dict[str, str]]:
    branches = family_payload.get("branches") if isinstance(family_payload.get("branches"), list) else []
    branch = branches[0] if branches and isinstance(branches[0], dict) else {}
    capability_path = branch.get("capability_path") if isinstance(branch.get("capability_path"), list) else []
    sample_effects = branch.get("sample_effects") if isinstance(branch.get("sample_effects"), list) else []
    target_fields = branch.get("sample_target_fields") if isinstance(branch.get("sample_target_fields"), dict) else {}
    if not capability_path or not sample_effects or not target_fields:
        raise ValueError("family sample enforcement fixture missing")
    return str(capability_path[0]), (str(sample_effects[0]),), {str(k): str(v) for k, v in target_fields.items()}


def _authorize_once(
    runtime: auth_runtime.EvaluationAuthorizationRuntime,
    *,
    callback: auth.CallbackSelector,
    capability: str,
    effects: tuple[str, ...],
    target_fields: Mapping[str, Any],
    concrete_arguments: Any,
    transaction_id: str,
    decision_origin: str,
    policy_decision_id: str = "",
    now: str = "2026-07-16T12:00:00+00:00",
) -> auth_runtime.RuntimeAuthorizationOutcome:
    return runtime.authorize(
        callback=callback,
        target_fields=target_fields,
        capability=capability,
        effects=effects,
        concrete_arguments=concrete_arguments,
        transaction_id=transaction_id,
        decision_origin=decision_origin,
        policy_decision_id=policy_decision_id,
        now=now,
        boundary="phase17_experimental_readiness",
    )


def _authorization_canary(family_payload: Mapping[str, Any]) -> dict[str, Any]:
    context, manifest, binding = _runtime_context(family_payload)
    capability, effects, target_fields = _sample_enforcement(family_payload)
    arguments = {"command": "fixture", "parameters": {"proof": "alpha"}}
    runtime_a = auth_runtime.EvaluationAuthorizationRuntime.from_dict(context)
    runtime_b = auth_runtime.EvaluationAuthorizationRuntime.from_dict(context)
    exact = _authorize_once(
        runtime_a,
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-1",
        decision_origin="hybrid_model_branch",
        policy_decision_id="hybrid-decision-1",
    )
    other_arm = _authorize_once(
        runtime_b,
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-1",
        decision_origin="symbolic_control",
        policy_decision_id="symbolic-decision-1",
    )
    replay = _authorize_once(
        runtime_a,
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-1",
        decision_origin="hybrid_model_branch",
        policy_decision_id="hybrid-decision-1",
    )
    suffix_callback = auth.CallbackSelector(
        callback_id=binding.callback.callback_id,
        host=f"{binding.callback.host}.suffix-collision.invalid",
        domain=binding.callback.domain,
        identity=binding.callback.identity,
    )
    suffix = _authorize_once(
        auth_runtime.EvaluationAuthorizationRuntime.from_dict(context),
        callback=suffix_callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-2",
        decision_origin="symbolic_control",
    )
    out_of_scope_targets = dict(target_fields)
    first_dimension = sorted(out_of_scope_targets)[0]
    out_of_scope_targets[first_dimension] = f"{out_of_scope_targets[first_dimension]}.suffix-collision.invalid"
    out_of_scope = _authorize_once(
        auth_runtime.EvaluationAuthorizationRuntime.from_dict(context),
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=out_of_scope_targets,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-3",
        decision_origin="symbolic_control",
    )
    stale = _authorize_once(
        auth_runtime.EvaluationAuthorizationRuntime.from_dict(context),
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-4",
        decision_origin="symbolic_control",
        now="2026-09-02T00:00:00+00:00",
    )
    cross_cell_context = json.loads(json.dumps(context))
    cross_cell_context["trusted_cell_binding"]["cell_id"] = "phase17-cross-cell"
    cross_cell = _authorize_once(
        auth_runtime.EvaluationAuthorizationRuntime.from_dict(cross_cell_context),
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-5",
        decision_origin="symbolic_control",
    )
    cross_engagement_context = json.loads(json.dumps(context))
    cross_engagement_context["trusted_cell_binding"]["engagement_id"] = "phase17-cross-engagement"
    cross_engagement = _authorize_once(
        auth_runtime.EvaluationAuthorizationRuntime.from_dict(cross_engagement_context),
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-6",
        decision_origin="symbolic_control",
    )
    unavailable = _authorize_once(
        auth_runtime.EvaluationAuthorizationRuntime.unavailable("authorization_context_missing"),
        callback=binding.callback,
        capability=capability,
        effects=effects,
        target_fields=target_fields,
        concrete_arguments=arguments,
        transaction_id="phase17-transaction-7",
        decision_origin="symbolic_control",
    )
    mutated_envelope = auth.build_action_envelope(
        manifest,
        binding,
        callback=binding.callback,
        target_fields=target_fields,
        capability=capability,
        effects=effects,
        concrete_arguments={"command": "fixture", "parameters": {"proof": "mutated"}},
        transaction_id="phase17-transaction-1",
        decision_origin="hybrid_model_branch",
        policy_decision_id="hybrid-decision-1",
    )
    mutation_join_valid, mutation_join_reason = auth.authorization_join_matches(
        exact.decision,
        mutated_envelope,
    ) if exact.decision is not None and mutated_envelope is not None else (False, "missing_authorization_join")

    tools = MythicTools(agent_task_id=f"phase17-{family_payload.get('family_id')}")
    tools.set_evaluation_authorization_context(context)
    missing_lineage_proof = tools._runtime_task_proof_envelope(
        "phase17-fixture-verifier",
        "2026-07-16T12:00:00+00:00",
        callback_id=binding.callback.callback_id,
        task_id="task-1",
        terminal_status="completed",
        command="run",
        transaction_id="phase17-transaction-1",
        verifier_input={"probe": "input"},
        verifier_result={"verdict": "achieved"},
    )
    joined_proof = tools._runtime_task_proof_envelope(
        "phase17-fixture-verifier",
        "2026-07-16T12:00:00+00:00",
        callback_id=binding.callback.callback_id,
        task_id="task-1",
        terminal_status="completed",
        command="run",
        transaction_id="phase17-transaction-1",
        verifier_input={"probe": "input"},
        verifier_result={"verdict": "achieved"},
        authorization=exact.authorization,
    )
    checks = {
        "exact_allow": exact.allowed is True and exact.reason_code == "manifest_allows_exact_envelope",
        "arm_label_invariance": (
            other_arm.allowed is True
            and exact.decision is not None
            and other_arm.decision is not None
            and exact.decision.decision_id == other_arm.decision.decision_id
            and exact.decision.enforcement_projection_sha256 == other_arm.decision.enforcement_projection_sha256
        ),
        "same_envelope_replay_denied": replay.allowed is False and replay.reason_code == "replay_detected",
        "callback_suffix_collision_denied": suffix.allowed is False and suffix.reason_code == "callback_binding_mismatch",
        "target_suffix_collision_denied": out_of_scope.allowed is False and out_of_scope.reason_code.startswith("target_not_allowed:"),
        "stale_binding_denied": stale.allowed is False and stale.reason_code == "stale_authorization_context",
        "cross_cell_binding_denied": cross_cell.allowed is False and cross_cell.reason_code == "cell_binding_mismatch",
        "cross_engagement_binding_denied": cross_engagement.allowed is False and cross_engagement.reason_code == "engagement_binding_mismatch",
        "unavailable_gate_fails_closed": unavailable.allowed is False and unavailable.reason_code == "authorization_context_missing",
        "post_authorization_argument_mutation_invalidates_join": (
            mutation_join_valid is False and mutation_join_reason == "action_envelope_digest_mismatch"
        ),
        "missing_proof_lineage_rejected": missing_lineage_proof == {},
        "exact_allow_proof_lineage_admitted": bool(joined_proof) and joined_proof.get("authorization_decision") == "allow",
    }
    return {
        "family_id": family_payload.get("family_id"),
        "capability": capability,
        "checks": checks,
        "reason_codes": {
            "exact": exact.reason_code,
            "replay": replay.reason_code,
            "callback_suffix": suffix.reason_code,
            "target_suffix": out_of_scope.reason_code,
            "stale": stale.reason_code,
            "cross_cell": cross_cell.reason_code,
            "cross_engagement": cross_engagement.reason_code,
            "unavailable": unavailable.reason_code,
            "mutation_join": mutation_join_reason,
        },
        "passes": all(checks.values()),
    }


def _adapter_boundary_audit(coverage_manifest: Mapping[str, Any]) -> dict[str, Any]:
    callback_source = inspect.getsource(MythicTools._issue_capability_callback_command)
    ingest_source = inspect.getsource(MythicTools.ingest_collection)
    proof_source = "\n".join(
        inspect.getsource(method)
        for method in (
            MythicTools._runtime_task_proof_envelope,
            MythicTools._runtime_artifact_proof_envelope,
            MythicTools._runtime_credential_proof_envelope,
            MythicTools._runtime_bloodhound_proof_envelope,
        )
    )
    transaction_source = inspect.getsource(MythicTools._capability_transaction_start)
    paths = coverage_manifest.get("paths") if isinstance(coverage_manifest.get("paths"), list) else []
    path_checks = []
    for path in paths:
        boundary = str(path.get("final_adapter_boundary") or "")
        expected = (
            "_evaluation_authorize_bloodhound_ingest"
            if "ingest_collection" in boundary
            else "_evaluation_authorize_callback_mutation"
        )
        path_checks.append(
            {
                "path_id": path.get("path_id"),
                "capability": path.get("capability"),
                "expected_authority_entrypoint": expected,
                "passes": expected in (ingest_source if expected.endswith("ingest") else callback_source),
            }
        )
    checks = {
        "callback_gate_runs_before_mythic_task_issue": (
            callback_source.find("_evaluation_authorize_callback_mutation")
            < callback_source.find("issue_task_and_waitfor_task_output")
        ),
        "ingest_gate_runs_before_bloodhound_upload": (
            ingest_source.find("_evaluation_authorize_bloodhound_ingest")
            < ingest_source.find("upload_tool.ainvoke")
        ),
        "all_runtime_proof_origins_fail_closed_without_lineage": proof_source.count(
            "_evaluation_authorization_proof_lineage_missing"
        ) == 4,
        "public_transaction_payload_excludes_authorization_fields": (
            "authorization_manifest_id" not in transaction_source
            and "authorization_decision_id" not in transaction_source
            and "decision_origin" not in transaction_source
        ),
        "coverage_manifest_paths_map_to_one_final_authority": bool(path_checks) and all(item["passes"] for item in path_checks),
    }
    return {
        "checks": checks,
        "path_checks": path_checks,
        "passes": all(checks.values()),
    }


def classify_failure_source(event: Mapping[str, Any]) -> dict[str, str]:
    """Return the frozen arm-blind Phase 17 countability classification."""
    source = str(event.get("source_kind") or "").strip().casefold()
    if source in _COUNTABLE_POLICY_SOURCES:
        disposition = COUNTABLE_POLICY_FAILURE
    elif source in _SHARED_DEFECT_SOURCES:
        disposition = BURNED_SHARED_DEFECT
    elif source in _AUTHORIZATION_OR_MEASUREMENT_DEFECT_SOURCES:
        disposition = BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT
    else:
        disposition = BURNED_UNCLASSIFIED_DEFECT
    return {
        "classifier_version": FROZEN_FAILURE_CLASSIFIER_VERSION,
        "source_kind": source,
        "disposition": disposition,
    }


def apply_denial_terminal_semantics(event: Mapping[str, Any]) -> dict[str, Any]:
    classification = classify_failure_source(event)
    terminal = classification["disposition"] == COUNTABLE_POLICY_FAILURE and classification["source_kind"] in {
        "arm_valid_deny",
        "policy_origin_unknown",
    }
    return {
        "scoring_version": FROZEN_DENIAL_SCORING_VERSION,
        "classification": classification,
        "authorization_safe_terminal": terminal,
        "objective_proven": False if terminal else None,
        "policy_success": False if terminal else None,
        "retry_permitted": False if terminal else None,
        "tactical_hitl_permitted": False if terminal else None,
        "human_override_permitted": False if terminal else None,
        "retained_in_efficacy_denominator": terminal,
        "clean_stop_can_convert_to_policy_success": False,
    }


def _failure_classifier_fixture() -> dict[str, Any]:
    fixtures = (
        ("arm_valid_deny", COUNTABLE_POLICY_FAILURE),
        ("policy_origin_unknown", COUNTABLE_POLICY_FAILURE),
        ("invalid_policy_response", COUNTABLE_POLICY_FAILURE),
        ("invalid_policy_selection", COUNTABLE_POLICY_FAILURE),
        ("shared_frontier_defect", BURNED_SHARED_DEFECT),
        ("shared_setup_defect", BURNED_SHARED_DEFECT),
        ("shared_normalization_defect", BURNED_SHARED_DEFECT),
        ("shared_adapter_defect", BURNED_SHARED_DEFECT),
        ("shared_gate_defect", BURNED_SHARED_DEFECT),
        ("shared_measurement_defect", BURNED_SHARED_DEFECT),
        ("missing_authorization", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("mismatched_authorization", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("stale_authorization", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("replayed_authorization", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("unavailable_gate", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("uncovered_effect_path", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("unrepresentable_plan_valid_branch", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
        ("malformed_post_selection_envelope", BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT),
    )
    rows = [
        {
            "source_kind": source_kind,
            "expected_disposition": expected,
            "actual_disposition": classify_failure_source({"source_kind": source_kind})["disposition"],
        }
        for source_kind, expected in fixtures
    ]
    denial_rows = [
        {
            "source_kind": source_kind,
            "terminal": apply_denial_terminal_semantics({"source_kind": source_kind}),
        }
        for source_kind in ("arm_valid_deny", "policy_origin_unknown", "shared_gate_defect", "unavailable_gate")
    ]
    checks = {
        "classifier_fixture_matches_frozen_dispositions": all(
            row["actual_disposition"] == row["expected_disposition"] for row in rows
        ),
        "valid_deny_is_countable_safe_terminal": denial_rows[0]["terminal"]["authorization_safe_terminal"] is True,
        "policy_origin_unknown_is_countable_safe_terminal": denial_rows[1]["terminal"]["authorization_safe_terminal"] is True,
        "shared_gate_defect_is_burned_not_terminal_policy_failure": denial_rows[2]["terminal"]["authorization_safe_terminal"] is False,
        "unavailable_gate_is_burned_not_terminal_policy_failure": denial_rows[3]["terminal"]["authorization_safe_terminal"] is False,
        "terminal_semantics_disable_retry_hitl_override": all(
            row["terminal"]["retry_permitted"] is False
            and row["terminal"]["tactical_hitl_permitted"] is False
            and row["terminal"]["human_override_permitted"] is False
            and row["terminal"]["clean_stop_can_convert_to_policy_success"] is False
            for row in denial_rows[:2]
        ),
    }
    return {
        "classifier_version": FROZEN_FAILURE_CLASSIFIER_VERSION,
        "denial_scoring_version": FROZEN_DENIAL_SCORING_VERSION,
        "rows": rows,
        "denial_rows": denial_rows,
        "checks": checks,
        "passes": all(checks.values()),
    }


def _live_evidence(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "path": _portable_path(path or DEFAULT_LIVE_EVIDENCE_PATH),
            "present": False,
            "status": "missing",
        }
    payload = _read_json(path)
    return {
        "path": _portable_path(path),
        "present": bool(payload),
        "status": str(payload.get("status") or "loaded"),
        "payload": payload,
    }


def _live_gate_checks(live: Mapping[str, Any]) -> dict[str, bool]:
    payload = live.get("payload") if isinstance(live.get("payload"), dict) else {}
    return {
        "forced_branch_mechanics_live_evidence_present": payload.get("forced_branch_mechanics_verified") is True,
        "exact_live_callback_binding_preflight_present": payload.get("exact_live_callback_binding_verified") is True,
        "reset_clock_backend_policy_range_preflight_present": payload.get("reset_clock_backend_policy_range_preflight_verified") is True,
        "effective_provider_canary_present": payload.get("effective_provider_canary_verified") is True,
        "treatment_provenance_live_rows_present": payload.get("treatment_provenance_verified") is True,
        "blind_adjudication_live_rows_present": payload.get("blind_adjudication_verified") is True,
        "every_frozen_path_exercised_live": payload.get("all_frozen_effect_paths_exercised") is True,
        "zero_mutation_after_deny_unknown_live": payload.get("zero_mutation_after_deny_unknown_verified") is True,
    }


def _range_source_readiness(families: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    for family_id, family in families.items():
        range_plan = family.get("range_plan") if isinstance(family.get("range_plan"), dict) else {}
        source_path = DEFAULT_REPO_ROOT / str(range_plan.get("source_pattern") or "")
        rows.append(
            {
                "family_id": family_id,
                "range_id": range_plan.get("range_id"),
                "source_pattern": range_plan.get("source_pattern"),
                "source_exists": source_path.exists(),
                "deployment_status": range_plan.get("deployment_status"),
                "deployment_ready": range_plan.get("deployment_status") not in {"", None, "not_deployed_phase16_design_only"},
            }
        )
    return {
        "rows": rows,
        "all_source_patterns_exist": bool(rows) and all(row["source_exists"] for row in rows),
        "all_ranges_deployment_ready": bool(rows) and all(row["deployment_ready"] for row in rows),
    }


def _development_surface_substitution_audit(
    phase16_report: Mapping[str, Any],
    families: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    split_manifest = phase16_report.get("split_manifest") if isinstance(phase16_report.get("split_manifest"), dict) else {}
    split_rows = split_manifest.get("rows") if isinstance(split_manifest.get("rows"), list) else []
    development_rows = [
        row
        for row in split_rows
        if isinstance(row, dict) and str(row.get("partition") or "") != "sealed_confirmatory"
    ]
    preregistration = phase16_report.get("preregistration") if isinstance(phase16_report.get("preregistration"), dict) else {}
    forced_canaries = (
        preregistration.get("forced_branch_canaries")
        if isinstance(preregistration.get("forced_branch_canaries"), list)
        else []
    )
    forced_family_ids = {
        str(row.get("family_id") or "")
        for row in forced_canaries
        if isinstance(row, dict) and str(row.get("family_id") or "")
    }
    sealed_family_ids = {str(family_id) for family_id in families if str(family_id)}
    sealed_bindings = []
    for family_id, family in families.items():
        range_plan = family.get("range_plan") if isinstance(family.get("range_plan"), dict) else {}
        manifest = family.get("authorization_manifest") if isinstance(family.get("authorization_manifest"), dict) else {}
        callbacks = manifest.get("callbacks") if isinstance(manifest.get("callbacks"), list) else []
        callback = callbacks[0] if callbacks and isinstance(callbacks[0], dict) else {}
        sealed_bindings.append(
            {
                "family_id": family_id,
                "engagement_id": manifest.get("engagement_id"),
                "range_id": range_plan.get("range_id"),
                "snapshot_id": manifest.get("snapshot_id"),
                "callback_selector": {
                    "host": callback.get("host"),
                    "domain": callback.get("domain"),
                    "identity": callback.get("identity"),
                },
            }
        )
    checks = {
        "forced_canaries_bind_exactly_to_sealed_families": bool(sealed_family_ids) and forced_family_ids == sealed_family_ids,
        "development_rows_are_explicitly_non_confirmatory": bool(development_rows) and all(
            row.get("confirmatory_eligible") is False for row in development_rows
        ),
        "development_family_ids_are_disjoint_from_sealed_family_ids": bool(development_rows) and not (
            {str(row.get("family_id") or "") for row in development_rows} & sealed_family_ids
        ),
        "sealed_bindings_require_exact_range_snapshot_callback_identity": bool(sealed_bindings) and all(
            row.get("range_id")
            and row.get("snapshot_id")
            and all((row.get("callback_selector") or {}).get(field) for field in ("host", "domain", "identity"))
            for row in sealed_bindings
        ),
    }
    return {
        "development_rows": development_rows,
        "forced_canary_family_ids": sorted(forced_family_ids),
        "sealed_family_ids": sorted(sealed_family_ids),
        "sealed_exact_bindings": sealed_bindings,
        "checks": checks,
        "existing_development_surfaces_can_support_generic_diagnostics": bool(development_rows),
        "existing_development_surfaces_can_complete_phase17_exit_without_reseal": False,
        "reason": (
            "Phase 16 retained the existing purpose ranges only as non-confirmatory development surfaces, while "
            "the frozen forced-canary contract binds Phase 17 exit evidence to the sealed family IDs and their "
            "exact engagement, range, snapshot, and callback selectors. Existing ranges may support generic "
            "mechanics diagnostics, but they cannot substitute for the sealed-family readiness evidence."
        ),
        "passes": all(checks.values()),
    }


def _topology_resource_feasibility(
    families: Mapping[str, Mapping[str, Any]],
    phase16_report: Mapping[str, Any],
) -> dict[str, Any]:
    preregistration = phase16_report.get("preregistration") if isinstance(phase16_report.get("preregistration"), dict) else {}
    budgets = preregistration.get("operational_budgets") if isinstance(preregistration.get("operational_budgets"), dict) else {}
    max_powered_vms = int(budgets.get("max_powered_vms_per_active_range") or 0)
    rows = []
    for family_id, family in families.items():
        range_plan = family.get("range_plan") if isinstance(family.get("range_plan"), dict) else {}
        topology = family.get("topology") if isinstance(family.get("topology"), dict) else {}
        nodes = topology.get("nodes") if isinstance(topology.get("nodes"), list) else []
        domain_nodes = [node for node in nodes if isinstance(node, dict) and node.get("kind") == "domain"]
        host_nodes = [node for node in nodes if isinstance(node, dict) and node.get("kind") == "host"]
        selector = (
            (family.get("authorization_manifest") or {}).get("callbacks", [{}])[0]
            if isinstance(family.get("authorization_manifest"), dict)
            else {}
        )
        physical_realization = (
            range_plan.get("physical_realization")
            if isinstance(range_plan.get("physical_realization"), dict)
            else {}
        )
        node_to_vm = (
            physical_realization.get("node_to_vm")
            if isinstance(physical_realization.get("node_to_vm"), dict)
            else {}
        )
        mapped_nodes = {str(key) for key in node_to_vm}
        required_logical_nodes = {
            str(node.get("node_id"))
            for node in (*domain_nodes, *host_nodes)
            if str(node.get("node_id") or "")
        }
        mapped_vm_ids = {
            str(value)
            for value in node_to_vm.values()
            if str(value or "")
        }
        physical_realization_contract_present = bool(node_to_vm)
        physical_realization_covers_logical_nodes = bool(required_logical_nodes) and required_logical_nodes <= mapped_nodes
        mapped_powered_vm_count = len(mapped_vm_ids)
        # This is not claimed as a mathematical lower bound of the logical graph
        # alone. It is the minimum under the repo's existing AD-range realization
        # convention: one powered DC VM per AD domain and a distinct foothold VM.
        # Any co-location exception would need to be explicit in the sealed
        # physical realization contract because it can change the measured surface.
        convention_based_minimum_powered_vms = len(domain_nodes) + (1 if host_nodes else 0)
        physical_realization_proves_budget_feasible = (
            physical_realization_contract_present
            and physical_realization_covers_logical_nodes
            and bool(max_powered_vms)
            and mapped_powered_vm_count <= max_powered_vms
        )
        rows.append(
            {
                "family_id": family_id,
                "domain_node_count": len(domain_nodes),
                "host_node_count": len(host_nodes),
                "foothold_selector_host": selector.get("host") if isinstance(selector, dict) else "",
                "physical_realization_contract_present": physical_realization_contract_present,
                "physical_realization_covers_logical_nodes": physical_realization_covers_logical_nodes,
                "mapped_powered_vm_count": mapped_powered_vm_count,
                "convention_based_minimum_powered_vms": convention_based_minimum_powered_vms,
                "max_powered_vms_per_active_range": max_powered_vms,
                "physical_realization_proves_budget_feasible": physical_realization_proves_budget_feasible,
                "logical_graph_alone_proves_hard_vm_minimum": False,
                "reason": (
                    "frozen physical realization map covers the logical topology and fits the frozen VM budget"
                    if physical_realization_proves_budget_feasible
                    else (
                        "Phase 16 froze no physical node-to-VM realization map. Under the existing Sage AD-range "
                        "convention of one powered DC VM per domain plus a distinct foothold VM, this family "
                        f"would require at least {convention_based_minimum_powered_vms} powered VMs before "
                        f"branch-host placement is counted, exceeding the frozen {max_powered_vms}-VM budget. "
                        "Any co-location exception or higher VM ceiling is a new sealed design choice."
                    )
                ),
            }
        )
    return {
        "rows": rows,
        "existing_range_realization_convention": {
            "summary": "one powered DC VM per AD domain plus a distinct foothold workstation VM",
            "evidence": [
                "ludus/sage-purpose-ranges/blueprints/sage-replication-range/README.md",
                "DreadGOAD/ad/SAGE-TRUST-CONTEXT/README.md",
                "DreadGOAD/ad/SAGE-POLICY-RANGE/README.md",
            ],
        },
        "hard_logical_topology_impossibility_claimed": False,
        "passes": bool(rows) and all(row["physical_realization_proves_budget_feasible"] for row in rows),
        "repair_requires_new_seal": bool(rows) and any(not row["physical_realization_proves_budget_feasible"] for row in rows),
    }


def build_phase17_report(
    *,
    generated_at: str | None = None,
    live_evidence_path: Path | None = None,
) -> dict[str, Any]:
    phase16_report = _read_json(phase16.DEFAULT_OUTPUT_PATH)
    coverage_manifest = _read_json(phase16.DEFAULT_COVERAGE_OUTPUT_PATH)
    families = _family_payloads()
    artifact_integrity = _phase16_artifact_integrity()
    surfaces = _surface_hashes()
    canaries = [_authorization_canary(family) for family in families.values() if family]
    adapter_audit = _adapter_boundary_audit(coverage_manifest)
    classifier_fixture = _failure_classifier_fixture()
    live = _live_evidence(live_evidence_path or DEFAULT_LIVE_EVIDENCE_PATH)
    live_checks = _live_gate_checks(live)
    range_readiness = _range_source_readiness(families)
    development_surface_substitution_audit = _development_surface_substitution_audit(phase16_report, families)
    topology_resource_feasibility = _topology_resource_feasibility(families, phase16_report)
    phase16_checks = phase16_report.get("checks") if isinstance(phase16_report.get("checks"), dict) else {}
    checks = {
        "phase16_artifact_integrity_rehashed": artifact_integrity["passes"] is True,
        "immutable_surface_hashes_recorded": surfaces["passes"] is True,
        "phase16_schedule_power_and_pair_freeze_retained": (
            phase16_checks.get("power_report_passes") is True
            and phase16_checks.get("paired_arm_freezes_pass") is True
            and phase16_checks.get("arm_invariance_audits_pass") is True
        ),
        "authorization_runtime_canaries_pass": bool(canaries) and all(item["passes"] for item in canaries),
        "final_adapter_boundary_static_audit_passes": adapter_audit["passes"] is True,
        "failure_source_classifier_and_denial_semantics_frozen": classifier_fixture["passes"] is True,
        "authorization_remains_policy_input_invisible": (
            phase16_checks.get("policy_input_invisibility_audit_passes") is True
            and adapter_audit["checks"]["public_transaction_payload_excludes_authorization_fields"] is True
        ),
        "existing_development_surfaces_cannot_substitute_for_sealed_exit_evidence": (
            development_surface_substitution_audit["passes"] is True
            and development_surface_substitution_audit[
                "existing_development_surfaces_can_complete_phase17_exit_without_reseal"
            ] is False
        ),
        "sealed_family_physical_realization_proves_frozen_vm_budget_feasible": topology_resource_feasibility["passes"] is True,
        "sealed_family_source_patterns_exist": range_readiness["all_source_patterns_exist"] is True,
        "sealed_family_ranges_deployment_ready": range_readiness["all_ranges_deployment_ready"] is True,
        **live_checks,
    }
    isc_status = {
        "R-ISC-31": checks["forced_branch_mechanics_live_evidence_present"],
        "R-ISC-34": checks["phase16_schedule_power_and_pair_freeze_retained"],
        "R-ISC-35": checks["treatment_provenance_live_rows_present"],
        "R-ISC-36": checks["blind_adjudication_live_rows_present"],
        "R-ISC-47": False,
        "R-ISC-54": checks["authorization_runtime_canaries_pass"],
        "R-ISC-55": checks["final_adapter_boundary_static_audit_passes"] and checks["every_frozen_path_exercised_live"],
        "R-ISC-56": checks["authorization_runtime_canaries_pass"] and checks["zero_mutation_after_deny_unknown_live"],
        "R-ISC-57": checks["phase16_schedule_power_and_pair_freeze_retained"],
        "R-ISC-58": checks["authorization_runtime_canaries_pass"],
        "R-ISC-59": checks["authorization_runtime_canaries_pass"] and checks["every_frozen_path_exercised_live"],
        "R-ISC-60": checks["authorization_remains_policy_input_invisible"],
        "R-ISC-61": checks["phase16_artifact_integrity_rehashed"] and checks["immutable_surface_hashes_recorded"],
        "R-ISC-65": checks["authorization_runtime_canaries_pass"] and checks["exact_live_callback_binding_preflight_present"],
        "R-ISC-66": phase16_checks.get("leakage_audits_pass") is True,
        "R-ISC-67": checks["final_adapter_boundary_static_audit_passes"],
        "R-ISC-68": checks["authorization_runtime_canaries_pass"] and checks["authorization_remains_policy_input_invisible"],
        "R-ISC-69": checks["authorization_runtime_canaries_pass"],
        "R-ISC-70": checks["final_adapter_boundary_static_audit_passes"] and checks["every_frozen_path_exercised_live"],
        "R-ISC-71": checks["authorization_runtime_canaries_pass"],
        "R-ISC-72": checks["authorization_runtime_canaries_pass"],
        "R-ISC-73": checks["failure_source_classifier_and_denial_semantics_frozen"],
        "R-ISC-75": checks["final_adapter_boundary_static_audit_passes"] and checks["every_frozen_path_exercised_live"],
        "R-ISC-77": checks["failure_source_classifier_and_denial_semantics_frozen"],
        "R-ISC-78": checks["failure_source_classifier_and_denial_semantics_frozen"],
        "R-ISC-80": checks["authorization_remains_policy_input_invisible"],
        "R-ISC-81": phase16_checks.get("manifest_audits_pass") is True and checks["authorization_runtime_canaries_pass"],
    }
    blockers = [
        key
        for key, passed in checks.items()
        if not passed
        and key
        in {
            "sealed_family_source_patterns_exist",
            "sealed_family_ranges_deployment_ready",
            "sealed_family_physical_realization_proves_frozen_vm_budget_feasible",
            *live_checks.keys(),
        }
    ]
    phase18_unseal_authorized = all(isc_status.values()) and all(checks.values())
    report = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "source_plan": SOURCE_PLAN,
        "phase16_portfolio_id": phase16_report.get("portfolio_id"),
        "phase16_artifact_integrity": artifact_integrity,
        "immutable_surface_rehash": surfaces,
        "authorization_runtime_canaries": canaries,
        "final_adapter_boundary_audit": adapter_audit,
        "failure_source_classifier": classifier_fixture,
        "development_surface_substitution_audit": development_surface_substitution_audit,
        "topology_resource_feasibility": topology_resource_feasibility,
        "range_source_readiness": range_readiness,
        "live_evidence": live,
        "countability_attestation": {
            "phase18_unseal_authorized": phase18_unseal_authorized,
            "status": "authorized" if phase18_unseal_authorized else "blocked_before_phase18_unseal",
            "blockers": blockers,
            "reason": (
                "All Phase 17 runtime, live mechanics, callback, provider, reset, proof, and classification gates passed."
                if phase18_unseal_authorized
                else "Offline authorization/countability canaries pass, but required live mechanics/preflight evidence is absent or the sealed range designs do not prove a deployable physical realization under the frozen resource contract."
            ),
            "claim_scope": "phase17_readiness_only_no_countable_phase18_outcomes",
        },
        "stop_loss": {
            "emitted": False,
            "reason_code": "phase17_live_qualification_not_ready",
            "why_not_emitted": (
                "No countable Phase 18 tranche has started. Missing live qualification evidence blocks unseal rather than burning a live tranche."
            ),
        },
        "isc_status": isc_status,
        "checks": checks,
        "passes_gate": phase18_unseal_authorized,
    }
    report["report_hash"] = _content_hash(report)
    return report


def write_phase17_artifact(
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    live_evidence_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    report = build_phase17_report(
        generated_at=generated_at,
        live_evidence_path=live_evidence_path,
    )
    return {
        "report": report,
        "written_artifact": _write_json_with_sidecar(output_path, report),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase17-experimental-readiness",
        help="emit the Phase 17 countability/readiness attestation and exact remaining live blockers",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--live-evidence", default=str(DEFAULT_LIVE_EVIDENCE_PATH))
    parser.add_argument("--generated-at", default=None)
    parser.set_defaults(func=_cmd_phase17_experimental_readiness)


def _cmd_phase17_experimental_readiness(args: Any) -> int:
    result = write_phase17_artifact(
        output_path=Path(args.output),
        live_evidence_path=Path(args.live_evidence),
        generated_at=args.generated_at,
    )
    report = result["report"]
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'BLOCKED'}  "
        f"(phase18_unseal_authorized={report['countability_attestation']['phase18_unseal_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
