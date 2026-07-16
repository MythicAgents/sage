"""Phase 9 auto-harness-improvement readiness verdict.

This module is a report composer over the existing evaluation foundation. It freezes
one existing replay experiment record for one structural selector surface, proves the
T0 boundary, and emits the typed negative readiness verdict when the matching T1
substrate is still unavailable. It does not launch live work, invoke a provider,
create a campaign, or mutate runtime state.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from . import fitness
from . import policy_replay_hillclimb_iteration
from . import policy_replay_promotion_gate
from .evaluation_foundation import (
    FrozenArtifactConfiguration,
    PairedIdentityValidation,
    bind_candidate_surface,
    build_provisional_readiness_report,
    build_t2_anchor_report,
    compose_official_reset_workflow,
    run_integrity_audit,
    run_t0_triage,
)
from .experiment_contracts import (
    AuthorizationBoundary,
    CandidateArtifact,
    ImmutableHashes,
    READINESS_ELIGIBLE,
    READINESS_NOT_READY,
    SURFACE_CLASS_STRUCTURAL,
    T0_REJECTED_OFFLINE,
    T0_UNSCORABLE_NEW_BEHAVIOR,
    T1_STRUCTURAL_UNAVAILABLE,
    content_hash,
    file_sha256,
)
from .proposer_canary import ProposerOutcome, ProviderRoute, run_proposer_canary
from ..trajectory.schema import TransitionRecord, load_jsonl


CANDIDATE_SURFACE = "retrieval-ranking"
EXPERIMENT_ID = "phase9-retrieval-ranking-readiness-v1"
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
DEFAULT_TRANSITIONS_PATH = Path(__file__).resolve().parents[2] / ".trajectory" / "transitions.jsonl"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


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


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _hash_file_map(paths: Iterable[Path], *, root: Path, absent_label: str) -> str:
    rows = {
        _relative(path, root): f"sha256:{file_sha256(path)}"
        for path in sorted({Path(path) for path in paths}, key=lambda item: item.as_posix())
        if path.exists() and path.is_file()
    }
    return content_hash(rows if rows else {"state": "absent", "label": absent_label})


def _hash_globs(base: Path, patterns: Sequence[str], *, root: Path, absent_label: str) -> str:
    paths = [
        path
        for pattern in patterns
        for path in base.glob(pattern)
        if path.exists() and path.is_file()
    ]
    return _hash_file_map(paths, root=root, absent_label=absent_label)


def _runtime_state_hashes(root: Path) -> tuple[str, str, str]:
    runtime_root = root / "Payload_Type" / "sage"
    sage_db_hash = _hash_file_map(
        (runtime_root / "sage.db",),
        root=root,
        absent_label="active-sage-db",
    )
    phoenix_db_hash = _hash_file_map(
        (runtime_root / ".phoenix" / "phoenix.db",),
        root=root,
        absent_label="active-phoenix-db",
    )
    ledger_hash = _hash_globs(
        runtime_root / ".sage_engagement",
        ("*",),
        root=root,
        absent_label="active-ledger",
    )
    return sage_db_hash, phoenix_db_hash, ledger_hash


def _immutable_hashes(root: Path, *, before: tuple[str, str, str], after: tuple[str, str, str]) -> ImmutableHashes:
    runtime_root = root / "Payload_Type" / "sage"
    hillclimb_root = runtime_root / "ai" / "hillclimb"
    evaluator_hash = _hash_file_map(
        (
            hillclimb_root / "evaluation_foundation.py",
            hillclimb_root / "experiment_contracts.py",
            hillclimb_root / "gate_experiment.py",
            hillclimb_root / "policy_replay_hillclimb_iteration.py",
            hillclimb_root / "policy_replay_promotion_gate.py",
            hillclimb_root / "policy_replay_unseen_candidate_evaluator.py",
        ),
        root=root,
        absent_label="evaluator-files",
    )
    reward_hash = content_hash({
        "reward_version": fitness.DENSE_REWARD_VERSION,
        "verifier_hash": fitness.verifier_hash(),
        "reward_file_hash": _hash_file_map(
            (hillclimb_root / "fitness.py",),
            root=root,
            absent_label="reward-file",
        ),
    })
    test_hash = _hash_globs(
        runtime_root / "tests",
        ("test_phase4_evaluation_foundation.py", "test_phase9_auto_harness_readiness.py"),
        root=root,
        absent_label="readiness-tests",
    )
    dataset_hash = _hash_file_map(
        (
            runtime_root / ".trajectory" / "transitions.jsonl",
            hillclimb_root / "policy_replay_frontier_corpus.json",
            hillclimb_root / "policy_replay_calibration_manifest.json",
            hillclimb_root / "policy_replay_corpus_sources.json",
        ),
        root=root,
        absent_label="readiness-dataset",
    )
    sealed_hash = _hash_globs(
        runtime_root / ".hillclimb" / "results",
        (
            "phase8_*_regression_validation_v2_*.json",
            "trust_context_corroboration_live_validation_v2_*.json",
            "laps_family_transfer_matrix_validation_r5_*.json",
        ),
        root=root,
        absent_label="sealed-evidence-inventory",
    )
    prompt_hash = _hash_globs(
        runtime_root / "prompts",
        ("*.md",),
        root=root,
        absent_label="prompt-files",
    )
    policy_hash = _hash_file_map(
        (runtime_root / "ai" / "langgraph" / "policy.py",),
        root=root,
        absent_label="policy-file",
    )
    return ImmutableHashes(
        evaluator_hash=evaluator_hash,
        reward_hash=reward_hash,
        test_hash=test_hash,
        dataset_hash=dataset_hash,
        sealed_hash=sealed_hash,
        prompt_hash=prompt_hash,
        policy_hash=policy_hash,
        active_sage_db_hash_before=before[0],
        active_phoenix_db_hash_before=before[1],
        active_ledger_hash_before=before[2],
        active_sage_db_hash_after=after[0],
        active_phoenix_db_hash_after=after[1],
        active_ledger_hash_after=after[2],
    )


def _load_records(path: Path) -> tuple[TransitionRecord, ...]:
    if not path.exists():
        return ()
    return tuple(load_jsonl(str(path)))


def _known_different_artifacts(source_head: str) -> tuple[FrozenArtifactConfiguration, ...]:
    descriptors = (
        ("selector-incumbent", "incumbent", "blocked_effect_aware_visible_cost"),
        ("selector-degradation-first", "deliberate_degradation", "first_admissible"),
        ("selector-degradation-wait", "deliberate_degradation", "lowest_visible_wait"),
        ("selector-candidate-review", "candidate_under_review", "modeled_reachability_aware_visible_cost"),
    )
    return tuple(
        FrozenArtifactConfiguration(
            artifact_id=artifact_id,
            source_head=source_head,
            candidate_surface=CANDIDATE_SURFACE,
            artifact_hash=content_hash({
                "artifact_id": artifact_id,
                "source_head": source_head,
                "candidate_surface": CANDIDATE_SURFACE,
                "selector": selector,
            }),
            role=role,
            evidence_kind="diagnostic_only",
            evidence_backed=False,
        )
        for artifact_id, role, selector in descriptors
    )


class _FixtureProcessController:
    def __init__(self) -> None:
        self.killed_process_groups: list[int] = []

    def kill_process_group(self, process_group_id: int) -> None:
        self.killed_process_groups.append(int(process_group_id))

    def detect_orphans(self, _child_process_ids: Sequence[int]) -> Sequence[int]:
        return ()


def _fixture_proposer_canary() -> dict[str, Any]:
    controller = _FixtureProcessController()

    def executor(_payload: str, route: ProviderRoute, iteration: int, retry_index: int) -> ProposerOutcome:
        if iteration == 0 and route.provider == "fixture-primary":
            return ProposerOutcome(
                status="proxy_failure",
                effective_backend=route.effective_backend,
                process_group_id=100 + retry_index,
                child_process_ids=(200 + retry_index,),
            )
        return ProposerOutcome(
            status="success",
            output="{}",
            effective_backend=route.effective_backend,
        )

    report = run_proposer_canary(
        redacted_input="state=<password:redacted>",
        routes=(
            ProviderRoute("fixture-primary", "fixture:primary"),
            ProviderRoute("fixture-backup", "fixture:backup"),
        ),
        executor=executor,
        iterations=3,
        max_retries_per_route=1,
        process_controller=controller,
    )
    return {
        "kind": "fixture_only_proposer_provider_cleanup_canary",
        "qualifies_as_effective_provider_evidence": False,
        "report": report.to_dict(),
    }


def _experiment_record_summary(
    iteration_report: Mapping[str, Any],
    promotion_report: Mapping[str, Any],
) -> dict[str, Any]:
    iteration = dict(iteration_report.get("iteration") or {})
    decision = dict(iteration_report.get("decision") or {})
    aggregate = dict(iteration_report.get("aggregate") or {})
    live_gate = dict(promotion_report.get("live_promotion_gate") or {})
    return {
        "record_kind": str(iteration_report.get("kind") or ""),
        "record_hash": content_hash(iteration_report),
        "promotion_record_hash": content_hash(promotion_report),
        "iteration_id": str(iteration.get("id") or ""),
        "verifier_hash": str(iteration.get("verifier_hash") or ""),
        "t0_disposition": str(aggregate.get("t0_disposition") or ""),
        "action": str(decision.get("action") or ""),
        "runtime_promotion_authorized": decision.get("runtime_promotion_authorized") is True,
        "promotion_gate_runtime_authorized": live_gate.get("runtime_promotion_authorized") is True,
        "promotion_gate_reason_codes": list(
            (promotion_report.get("typed_verdict") or {}).get("reason_codes") or ()
        ),
        "iteration_passes_gate": iteration_report.get("passes_gate") is True,
        "promotion_gate_passes": promotion_report.get("passes_gate") is True,
    }


def _failed_prerequisites(
    *,
    calibration_failed_gates: Sequence[str],
    surface_reason_code: str,
    paired_identity: PairedIdentityValidation,
    fixture_canary: Mapping[str, Any],
    reset_reason_code: str,
) -> tuple[str, ...]:
    failures = [
        surface_reason_code,
        *paired_identity.failures,
        *tuple(calibration_failed_gates),
        "effective_provider_proposer_canary_not_completed",
        "matching_unattended_reset_attestation_not_returned",
    ]
    if not ((fixture_canary.get("report") or {}).get("passed") is True):
        failures.append("fixture_proposer_provider_cleanup_canary_failed")
    if reset_reason_code:
        failures.append(reset_reason_code)
    return tuple(dict.fromkeys(item for item in failures if str(item or "").strip()))


def build_phase9_readiness_report(
    *,
    repo_root: str | Path | None = None,
    source_head: str | None = None,
    records: Sequence[TransitionRecord] | None = None,
    iteration_report: Mapping[str, Any] | None = None,
    promotion_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else _repo_root()
    head = str(source_head if source_head is not None else _source_head(root)).strip()
    before = _runtime_state_hashes(root)
    rows = tuple(records) if records is not None else _load_records(
        root / "Payload_Type" / "sage" / ".trajectory" / "transitions.jsonl"
    )
    replay_iteration = dict(
        iteration_report
        if iteration_report is not None
        else policy_replay_hillclimb_iteration.run_hillclimb_iteration()
    )
    replay_promotion = dict(
        promotion_report
        if promotion_report is not None
        else policy_replay_promotion_gate.run_promotion_gate(iteration_report=replay_iteration)
    )
    candidate = CandidateArtifact(
        candidate_id="phase9-retrieval-ranking-readiness",
        surface=CANDIDATE_SURFACE,
        surface_class=SURFACE_CLASS_STRUCTURAL,
        single_variable="selector-configuration",
        target_subsystem="policy-replay-selector",
        artifact_hash=content_hash({
            "experiment_id": EXPERIMENT_ID,
            "source_head": head,
            "candidate_surface": CANDIDATE_SURFACE,
        }),
        hypothesis="A structural selector surface can be evaluated only with matching paired live evidence.",
        mechanism="surface-bound readiness validation",
    )
    artifacts = _known_different_artifacts(head)
    t0_known_violation = run_t0_triage(rows, known_regression_count=1)
    t0_unseen_mechanism = run_t0_triage(
        rows,
        claimed_mechanism_requires_unseen_behavior=True,
    )
    binding = bind_candidate_surface(
        candidate.surface,
        surface_class=candidate.surface_class,
        structural_manifest=None,
    )
    paired_identity = PairedIdentityValidation(
        False,
        ("missing_operator_returned_paired_t1_evidence",),
    )
    t2_anchor = build_t2_anchor_report(
        candidate_surface=candidate.surface,
        artifacts=artifacts,
        paired_identity=paired_identity,
        cheap_scores=(),
        t2_scores=(),
        t0_disposition=t0_unseen_mechanism.disposition,
        t0_coverage=t0_unseen_mechanism.coverage,
        unscorable_new_behavior_rate=t0_unseen_mechanism.unscorable_new_behavior_rate,
        t1_substrate_status=binding.t1_substrate_status,
        smallest_relevant_effect=None,
        target_power=None,
        achieved_power=None,
        measured_noise=None,
        mde=None,
        substrate_class=binding.substrate_class,
        t1_reason_code=binding.reason_code,
    )
    reset_workflow = compose_official_reset_workflow(
        ready_payload={"ready": False, "reason": "matching_t1_substrate_not_verified"},
        operator_invoked=False,
        human_steps_remaining=("verify matching structural T1 substrate reset attestation",),
    )
    fixture_canary = _fixture_proposer_canary()
    after = _runtime_state_hashes(root)
    immutable_hashes = _immutable_hashes(root, before=before, after=after)
    authorization = AuthorizationBoundary(
        live_work_authorized=False,
        operator_invocation_required=True,
        controller_halted_before_live=True,
        source_edits_authorized=False,
        product_default_change_authorized=False,
        candidate_mythic_tasks_launched=0,
        candidate_target_connections_opened=0,
        official_workflow_only=True,
    )
    integrity = run_integrity_audit(
        candidate_payload=json.dumps({
            "surface": candidate.surface,
            "artifacts": [artifact.artifact_id for artifact in artifacts],
            "mode": "diagnostic_only",
        }, sort_keys=True),
        forbidden_literals=(),
        immutable_hashes=immutable_hashes,
        authorization_boundary=authorization,
        disposable_overlay_exists_after=False,
        sealed_feedback_fragments=("sealed-feedback-fixture",),
        sealed_feedback_accessed=False,
    )
    readiness = build_provisional_readiness_report(
        candidate=candidate,
        t0=t0_unseen_mechanism,
        surface_binding=binding,
        paired_identity=paired_identity,
        t2_anchor=t2_anchor,
        reset_workflow=reset_workflow,
        integrity=integrity,
        proposer_canary_passed=False,
        provider_canary_passed=False,
        authorization_boundary=authorization,
    )
    readiness = replace(
        readiness,
        cheapest_decisive_next_experiment=(
            "verify one authorized topology/branch-varying real-AD T1 substrate manifest, "
            "reset, and power envelope for retrieval-ranking"
        ),
        notes=(
            "Phase 9 stops before live/provider spend because the named structural surface has no verified matching T1 substrate.",
            "Diagnostic selector configurations validate plumbing only and cannot satisfy paired live ranking evidence.",
        ),
    )
    failed_prerequisites = _failed_prerequisites(
        calibration_failed_gates=t2_anchor.calibration.failed_gates,
        surface_reason_code=binding.reason_code,
        paired_identity=paired_identity,
        fixture_canary=fixture_canary,
        reset_reason_code=reset_workflow.reason_code,
    )
    budget_manifest = {
        "t0_artifact_count": len(artifacts),
        "max_t1_requests": 2,
        "max_t2_requests": 1,
        "proposer_max_proposals": 5,
        "proposer_wall_clock_hours": 4,
        "proposer_model_cost_usd": 50.0,
        "live_work_started": False,
        "provider_work_started": False,
    }
    frozen_t1_request_payload = {
        "experiment_id": EXPERIMENT_ID,
        "candidate_surface": candidate.surface,
        "surface_class": candidate.surface_class,
        "source_head": head,
        "artifact_ids": [artifact.artifact_id for artifact in artifacts],
        "required_substrate_status": "verified",
        "observed_substrate_status": binding.t1_substrate_status,
        "state": "blocked_before_operator_invocation",
        "operator_invocation_authorized": False,
        "live_work_started": False,
        "reason_code": binding.reason_code,
    }
    frozen_t1_request = {
        **frozen_t1_request_payload,
        "request_hash": content_hash(frozen_t1_request_payload),
    }
    readiness_payload = readiness.to_dict()
    readiness_payload["failed_prerequisites"] = list(failed_prerequisites)
    experiment_record = _experiment_record_summary(replay_iteration, replay_promotion)
    frozen_gate_record = {
        "experiment_id": EXPERIMENT_ID,
        "candidate_surface": candidate.surface,
        "surface_class": candidate.surface_class,
        "source_head": head,
        "reward_version": fitness.DENSE_REWARD_VERSION,
        "immutable_hashes": asdict(immutable_hashes),
        "experiment_record": experiment_record,
        "surface_binding": asdict(binding),
        "backend_identity": {
            "configured_backend": None,
            "effective_backend": None,
            "request_schema_hash": content_hash({"state": "halted_before_live"}),
            "status": "not_invoked_due_prerequisite_failure",
        },
        "range_identity": {
            "tier": "T1",
            "substrate_class": binding.substrate_class,
            "validity_envelope": list(binding.validity_envelope),
            "status": binding.t1_substrate_status,
            "manifest_hash": binding.substrate_manifest_hash or None,
            "snapshot_id": binding.snapshot_id or None,
        },
        "reset_identity": asdict(reset_workflow.reset_identity),
        "budget_manifest": {
            **budget_manifest,
            "budget_hash": content_hash(budget_manifest),
        },
    }
    checks = {
        "source_head_frozen": bool(head),
        "one_candidate_surface_frozen": candidate.surface == CANDIDATE_SURFACE,
        "known_different_artifacts_frozen": t2_anchor.artifact_validation.valid_for_instrument_plumbing,
        "known_different_artifacts_not_mislabeled_live": t2_anchor.artifact_validation.valid_for_live_ranking is False,
        "known_violation_rejected_offline": t0_known_violation.disposition == T0_REJECTED_OFFLINE,
        "unseen_mechanism_returns_unscorable": t0_unseen_mechanism.disposition == T0_UNSCORABLE_NEW_BEHAVIOR,
        "structural_substrate_not_silently_substituted": binding.reason_code == T1_STRUCTURAL_UNAVAILABLE,
        "controller_halted_before_live": authorization.controller_halted_before_live is True,
        "no_live_or_provider_spend_started": (
            frozen_t1_request["live_work_started"] is False
            and budget_manifest["provider_work_started"] is False
        ),
        "fixture_canary_is_not_effective_provider_evidence": (
            fixture_canary["qualifies_as_effective_provider_evidence"] is False
        ),
        "runtime_state_remained_byte_identical": immutable_hashes.runtime_state_byte_identical,
        "integrity_and_authorization_canaries_pass": integrity.passed and authorization.passed,
        "exactly_one_typed_surface_verdict": readiness.readiness_decision in {READINESS_NOT_READY, READINESS_ELIGIBLE},
        "negative_verdict_stops_campaign": (
            readiness.readiness_decision == READINESS_NOT_READY
            and frozen_t1_request["operator_invocation_authorized"] is False
        ),
    }
    return {
        "kind": "phase9_auto_harness_readiness_verdict",
        "phase": "phase9",
        "candidate": candidate.to_dict(),
        "frozen_gate_record": frozen_gate_record,
        "known_different_artifacts": [asdict(artifact) for artifact in artifacts],
        "t0": {
            "known_violation": asdict(t0_known_violation),
            "unseen_mechanism": asdict(t0_unseen_mechanism),
        },
        "frozen_t1_request": frozen_t1_request,
        "t2_anchor": {
            "artifact_validation": asdict(t2_anchor.artifact_validation),
            "calibration": t2_anchor.calibration.to_dict(),
            "paired_identity": asdict(t2_anchor.paired_identity),
            "ranking_authorized": t2_anchor.ranking_authorized,
        },
        "canaries": {
            "fixture_proposer_provider_cleanup": fixture_canary,
            "effective_provider_proposer": {
                "passed": False,
                "reason_code": "effective_provider_proposer_canary_not_completed",
                "provider_work_started": False,
            },
            "reset": {
                "workflow_id": reset_workflow.workflow_id,
                "steps": [asdict(step) for step in reset_workflow.steps],
                "ready_attestation_hash": reset_workflow.ready_attestation_hash,
                "ahi22_passed": reset_workflow.ahi22_passed,
                "reason_code": reset_workflow.reason_code,
            },
            "integrity": asdict(integrity),
            "authorization": asdict(authorization),
        },
        "readiness": readiness_payload,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def render_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
