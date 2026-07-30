"""Phase 4 verifier-first auto-harness-improvement evaluation foundation.

This module is intentionally pure and fail-closed.  It composes the existing typed
experiment contract, dense reward, trajectory-v2 records, and gate-experiment math
into one provisional readiness report.  It does not run candidates, task Mythic,
mutate runtime state, or authorize a live campaign.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .experiment_contracts import (
    AuthorizationBoundary,
    CandidateArtifact,
    EvalResult,
    ImmutableHashes,
    Measurement,
    ObservedOutcome,
    RangeIdentity,
    ReadinessGateResult,
    ReadinessReport,
    ResetIdentity,
    SURFACE_CLASS_LEXICAL_IDENTITY_SECRET,
    SURFACE_CLASS_STRUCTURAL,
    T0_REJECTED_OFFLINE,
    T0_SURVIVED_TRIAGE,
    T0_TRIAGE_ONLY,
    T0_UNSCORABLE_NEW_BEHAVIOR,
    T1_LEXICAL_UNAVAILABLE,
    T1_STRUCTURAL_UNAVAILABLE,
    T1_SUBSTRATE_VERIFIED,
    content_hash,
)
from .fitness import DENSE_REWARD_VERSION
from .gate_experiment import CalibrationProtocolReport, run_calibration_protocol
from ..trajectory.schema import SCHEMA_VERSION, TransitionRecord, redact_text


OFFICIAL_RESET_WORKFLOW_ID = "sage-official-reset-and-ready-v1"
OFFICIAL_RESET_STEPS = (
    ("archive_runtime_dbs", "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"),
    ("reset_mythic", "skills/sage-goad-reset/scripts/mythic_reset.sh"),
    ("reset_goad", "skills/sage-goad-reset/scripts/ludus.py"),
    ("wipe_bloodhound", "skills/sage-goad-reset/scripts/bh_reset.py"),
    ("restart_sage", "skills/sage-goad-reset/scripts/sage_restart.sh"),
    ("bootstrap_payloads", "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py"),
    ("deploy_foothold", "skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py"),
    ("callback_preflight", "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py"),
    ("runtime_readiness", "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py"),
)
DEFAULT_LAB_LITERAL_PATTERNS = (
    re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    re.compile(r"\b(?:callback|cb)[-_ :=#]*\d+\b", re.IGNORECASE),
    re.compile(r"/home/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\", re.IGNORECASE),
)


@dataclass(frozen=True)
class SurfaceBinding:
    candidate_surface: str
    surface_class: str
    substrate_class: str
    validity_envelope: tuple[str, ...]
    t1_substrate_status: str
    substrate_manifest_hash: str = ""
    substrate_entrypoint: str = ""
    snapshot_id: str = ""
    reason_code: str = ""

    @property
    def verified(self) -> bool:
        return self.t1_substrate_status == T1_SUBSTRATE_VERIFIED


@dataclass(frozen=True)
class T0TriageReport:
    total_records: int
    schema_v2_records: int
    scorable_records: int
    diagnostic_records: int
    known_regression_count: int
    unscorable_new_behavior_count: int
    coverage: float
    unscorable_new_behavior_rate: float
    disposition: str
    retain_artifact_for_review: bool = False
    promotion_evidence_passed: bool = False


@dataclass(frozen=True)
class PairedIdentityValidation:
    valid: bool
    failures: tuple[str, ...] = field(default_factory=tuple)
    paired_instance_id: str = ""


@dataclass(frozen=True)
class FrozenArtifactConfiguration:
    artifact_id: str
    source_head: str
    candidate_surface: str
    artifact_hash: str
    role: str
    evidence_kind: str
    evidence_backed: bool


@dataclass(frozen=True)
class FrozenArtifactValidation:
    valid_for_instrument_plumbing: bool
    valid_for_live_ranking: bool
    failures: tuple[str, ...]
    live_ranking_evidence_count: int
    candidate_surface: str = ""
    source_head: str = ""


@dataclass(frozen=True)
class ResetWorkflowStep:
    step_id: str
    entrypoint: str


@dataclass(frozen=True)
class ResetWorkflowReport:
    workflow_id: str
    steps: tuple[ResetWorkflowStep, ...]
    ready_payload: dict[str, Any]
    ready_attestation_hash: str
    operator_invoked: bool
    controller_invoked: bool
    human_steps_remaining: tuple[str, ...]
    reset_identity: ResetIdentity
    ahi22_passed: bool
    reason_code: str = ""


@dataclass(frozen=True)
class IntegrityAuditReport:
    no_lab_literals: bool
    immutable_hashes_present: bool
    runtime_state_byte_identical: bool
    leakage_free: bool
    holdout_leakage_free: bool
    authorization_boundary_passed: bool
    offline_side_effects_absent: bool
    disposable_overlay_cleaned: bool
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class T2AnchorReport:
    candidate_surface: str
    artifact_validation: FrozenArtifactValidation
    calibration: CalibrationProtocolReport
    paired_identity: PairedIdentityValidation

    @property
    def ranking_authorized(self) -> bool:
        return bool(
            self.artifact_validation.valid_for_live_ranking
            and self.paired_identity.valid
            and self.calibration.ranking_authorized
        )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_byte_hashes(paths: Iterable[str | Path]) -> dict[str, str]:
    return {
        str(Path(path)): _sha256_file(path)
        for path in paths
        if Path(path).exists() and Path(path).is_file()
    }


def byte_identity_matches(before: Mapping[str, str], after: Mapping[str, str]) -> bool:
    return dict(before) == dict(after)


def bind_candidate_surface(
    candidate_surface: str,
    *,
    surface_class: str,
    dreadgoad_authorized: bool = False,
    dreadgoad_seeded_identity_verified: bool = False,
    dreadgoad_reset_verified: bool = False,
    dreadgoad_power_verified: bool = False,
    dreadgoad_manifest_hash: str = "",
    dreadgoad_entrypoint: str = "",
    dreadgoad_snapshot_id: str = "",
    structural_manifest: Mapping[str, Any] | None = None,
) -> SurfaceBinding:
    """Bind one candidate surface to the only admissible T1 substrate class."""

    surface = _text(candidate_surface)
    kind = _text(surface_class)
    if kind == SURFACE_CLASS_LEXICAL_IDENTITY_SECRET:
        verified = all(
            (
                dreadgoad_authorized,
                dreadgoad_seeded_identity_verified,
                dreadgoad_reset_verified,
                dreadgoad_power_verified,
                bool(_text(dreadgoad_manifest_hash)),
                bool(_text(dreadgoad_entrypoint)),
                bool(_text(dreadgoad_snapshot_id)),
            )
        )
        return SurfaceBinding(
            candidate_surface=surface,
            surface_class=kind,
            substrate_class="dreadgoad_sage_variant",
            validity_envelope=("lexical", "identity", "secret"),
            t1_substrate_status=T1_SUBSTRATE_VERIFIED if verified else T1_LEXICAL_UNAVAILABLE,
            substrate_manifest_hash=_text(dreadgoad_manifest_hash),
            substrate_entrypoint=_text(dreadgoad_entrypoint),
            snapshot_id=_text(dreadgoad_snapshot_id),
            reason_code="" if verified else T1_LEXICAL_UNAVAILABLE,
        )
    if kind == SURFACE_CLASS_STRUCTURAL:
        manifest = dict(structural_manifest or {})
        verified = all(
            manifest.get(key) is True
            for key in (
                "authorized",
                "real_ad",
                "topology_branch_varying",
                "reset_verified",
                "power_verified",
            )
        ) and all(
            _text(manifest.get(key))
            for key in ("manifest_hash", "entrypoint", "snapshot_id")
        )
        return SurfaceBinding(
            candidate_surface=surface,
            surface_class=kind,
            substrate_class="real_ad_topology_branch_varying",
            validity_envelope=("strategy", "routing", "retrieval", "frontier_structure"),
            t1_substrate_status=T1_SUBSTRATE_VERIFIED if verified else T1_STRUCTURAL_UNAVAILABLE,
            substrate_manifest_hash=_text(manifest.get("manifest_hash")),
            substrate_entrypoint=_text(manifest.get("entrypoint")),
            snapshot_id=_text(manifest.get("snapshot_id")),
            reason_code="" if verified else T1_STRUCTURAL_UNAVAILABLE,
        )
    raise ValueError(f"unsupported candidate surface class: {kind or '<missing>'}")


def run_t0_triage(
    records: Iterable[TransitionRecord],
    *,
    claimed_mechanism_requires_unseen_behavior: bool = False,
    known_regression_count: int = 0,
) -> T0TriageReport:
    """T0 is triage only.  It cannot retain an artifact or invent outcomes."""

    rows = tuple(records)
    schema_v2 = sum(1 for record in rows if int(record.schema_version or 0) >= SCHEMA_VERSION)
    diagnostic = sum(1 for record in rows if record.is_diagnostic_only)
    scorable = sum(
        1
        for record in rows
        if int(record.schema_version or 0) >= SCHEMA_VERSION and not record.is_diagnostic_only
    )
    total = len(rows)
    coverage = scorable / total if total else 0.0
    unscorable_count = 1 if claimed_mechanism_requires_unseen_behavior else 0
    unscorable_rate = unscorable_count / max(1, total)
    if claimed_mechanism_requires_unseen_behavior:
        disposition = T0_UNSCORABLE_NEW_BEHAVIOR
    elif int(known_regression_count or 0) > 0:
        disposition = T0_REJECTED_OFFLINE
    elif scorable > 0:
        disposition = T0_SURVIVED_TRIAGE
    else:
        disposition = T0_TRIAGE_ONLY
    return T0TriageReport(
        total_records=total,
        schema_v2_records=schema_v2,
        scorable_records=scorable,
        diagnostic_records=diagnostic,
        known_regression_count=max(0, int(known_regression_count or 0)),
        unscorable_new_behavior_count=unscorable_count,
        coverage=coverage,
        unscorable_new_behavior_rate=unscorable_rate,
        disposition=disposition,
    )


def _compare_field(
    failures: list[str],
    label: str,
    candidate_value: Any,
    incumbent_value: Any,
) -> None:
    if candidate_value != incumbent_value:
        failures.append(f"paired_{label}_mismatch")


def validate_paired_identity(candidate: EvalResult, incumbent: EvalResult) -> PairedIdentityValidation:
    """Require T1/T2 candidate and incumbent rows to be the same paired experiment."""

    failures: list[str] = []
    if candidate.range_identity is None or incumbent.range_identity is None:
        return PairedIdentityValidation(False, ("missing_range_identity",))
    if candidate.reset_identity is None or incumbent.reset_identity is None:
        failures.append("missing_reset_identity")
    if candidate.model_identity is None or incumbent.model_identity is None:
        failures.append("missing_model_identity")
    c_range = candidate.range_identity
    i_range = incumbent.range_identity
    for label in (
        "tier",
        "substrate_class",
        "range_id",
        "topology_family",
        "objective_id",
        "seed",
        "paired_instance_id",
        "presentation_order",
        "budget_hash",
    ):
        _compare_field(failures, label, getattr(c_range, label), getattr(i_range, label))
    _compare_field(failures, "validity_envelope", c_range.validity_envelope, i_range.validity_envelope)
    if candidate.reset_identity is not None and incumbent.reset_identity is not None:
        for label in ("workflow_id", "snapshot_id", "ready_attestation_hash"):
            _compare_field(
                failures,
                f"reset_{label}",
                getattr(candidate.reset_identity, label),
                getattr(incumbent.reset_identity, label),
            )
        if candidate.reset_identity.ahi22_passed is not True or incumbent.reset_identity.ahi22_passed is not True:
            failures.append("reset_not_unattended_ready")
    if candidate.model_identity is not None and incumbent.model_identity is not None:
        for label in ("configured_backend", "effective_backend", "request_schema_hash"):
            _compare_field(
                failures,
                f"model_{label}",
                getattr(candidate.model_identity, label),
                getattr(incumbent.model_identity, label),
            )
    if candidate.independently_observed is not True:
        failures.append("candidate_outcome_not_independently_observed")
    if incumbent.independently_observed is not True:
        failures.append("incumbent_outcome_not_independently_observed")
    return PairedIdentityValidation(
        valid=not failures,
        failures=tuple(dict.fromkeys(failures)),
        paired_instance_id=c_range.paired_instance_id,
    )


def validate_known_different_artifacts(
    artifacts: Sequence[FrozenArtifactConfiguration],
    *,
    expected_surface: str = "",
) -> FrozenArtifactValidation:
    rows = tuple(artifacts)
    failures: list[str] = []
    if not 3 <= len(rows) <= 5:
        failures.append("requires_3_to_5_frozen_artifacts")
    surfaces = {_text(item.candidate_surface) for item in rows if _text(item.candidate_surface)}
    heads = {_text(item.source_head) for item in rows if _text(item.source_head)}
    if len(surfaces) != 1:
        failures.append("artifact_surface_mismatch")
    if _text(expected_surface) and surfaces != {_text(expected_surface)}:
        failures.append("candidate_surface_scope_mismatch")
    if len(heads) != 1:
        failures.append("source_head_mismatch")
    roles = {_text(item.role) for item in rows}
    if "incumbent" not in roles:
        failures.append("missing_incumbent")
    if "deliberate_degradation" not in roles:
        failures.append("missing_deliberate_degradation")
    if any(item.role == "positive_control" and item.evidence_backed is not True for item in rows):
        failures.append("unsupported_positive_control")
    if any(not _text(item.artifact_hash) for item in rows):
        failures.append("missing_artifact_hash")
    live_ranking_rows = [
        item
        for item in rows
        if item.evidence_backed is True and item.evidence_kind != "fixture_only"
    ]
    return FrozenArtifactValidation(
        valid_for_instrument_plumbing=not failures,
        valid_for_live_ranking=not failures and len(live_ranking_rows) == len(rows),
        failures=tuple(dict.fromkeys(failures)),
        live_ranking_evidence_count=len(live_ranking_rows),
        candidate_surface=next(iter(surfaces), ""),
        source_head=next(iter(heads), ""),
    )


def build_fixture_known_different_artifacts(
    *,
    source_head: str,
    candidate_surface: str,
) -> tuple[FrozenArtifactConfiguration, ...]:
    """Freeze 3 same-surface fixture controls for instrument plumbing only.

    These controls deliberately do not claim live ranking evidence.  They let the
    Phase 4 instrument prove same-head/same-surface handling while preserving the
    boundary that only later operator-returned T2 evidence can authorize ranking.
    """

    descriptors = (
        ("fixture-incumbent", "incumbent", "incumbent generic overlay"),
        ("fixture-degradation", "deliberate_degradation", "deliberate no-op degradation"),
        ("fixture-degradation-2", "deliberate_degradation", "second deliberate no-op degradation"),
    )
    return tuple(
        FrozenArtifactConfiguration(
            artifact_id=artifact_id,
            source_head=_text(source_head),
            candidate_surface=_text(candidate_surface),
            artifact_hash=hashlib.sha256(
                json.dumps(
                    {
                        "artifact_id": artifact_id,
                        "source_head": _text(source_head),
                        "candidate_surface": _text(candidate_surface),
                        "descriptor": descriptor,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            role=role,
            evidence_kind="fixture_only",
            evidence_backed=False,
        )
        for artifact_id, role, descriptor in descriptors
    )


def compose_official_reset_workflow(
    *,
    ready_payload: Mapping[str, Any] | None,
    operator_invoked: bool,
    controller_invoked: bool = False,
    snapshot_id: str = "",
    human_steps_remaining: Sequence[str] = (),
) -> ResetWorkflowReport:
    """Represent the official reset workflow without duplicating its implementation."""

    payload = dict(ready_payload or {})
    ready_hash = content_hash(payload) if payload.get("ready") is True else ""
    human_steps = tuple(_text(item) for item in human_steps_remaining if _text(item))
    reset_identity = ResetIdentity(
        workflow_id=OFFICIAL_RESET_WORKFLOW_ID,
        snapshot_id=snapshot_id,
        ready_attestation_hash=ready_hash,
        operator_invoked=operator_invoked,
        unattended_after_invocation=(
            bool(payload.get("ready") is True)
            and operator_invoked is True
            and controller_invoked is False
            and not human_steps
        ),
        human_steps_remaining=human_steps,
    )
    ahi22_passed = reset_identity.ahi22_passed
    return ResetWorkflowReport(
        workflow_id=OFFICIAL_RESET_WORKFLOW_ID,
        steps=tuple(ResetWorkflowStep(step_id, entrypoint) for step_id, entrypoint in OFFICIAL_RESET_STEPS),
        ready_payload=payload,
        ready_attestation_hash=ready_hash,
        operator_invoked=operator_invoked,
        controller_invoked=controller_invoked,
        human_steps_remaining=human_steps,
        reset_identity=reset_identity,
        ahi22_passed=ahi22_passed,
        reason_code="" if ahi22_passed else "ahi22_reset_not_unattended_ready",
    )


def lint_candidate_text(
    text: str,
    *,
    forbidden_literals: Sequence[str] = (),
    extra_patterns: Sequence[re.Pattern[str]] = (),
) -> tuple[str, ...]:
    """Static candidate lint for lab-specific literals and secret-bearing path fragments."""

    value = str(text or "")
    folded = value.casefold()
    hits = [
        f"literal:{literal}"
        for literal in forbidden_literals
        if _text(literal) and _text(literal).casefold() in folded
    ]
    for pattern in (*DEFAULT_LAB_LITERAL_PATTERNS, *tuple(extra_patterns)):
        if pattern.search(value):
            hits.append(f"pattern:{pattern.pattern}")
    return tuple(dict.fromkeys(hits))


def run_integrity_audit(
    *,
    candidate_payload: str,
    forbidden_literals: Sequence[str],
    immutable_hashes: ImmutableHashes,
    authorization_boundary: AuthorizationBoundary,
    disposable_overlay_exists_after: bool,
    sealed_feedback_fragments: Sequence[str] = (),
    sealed_feedback_accessed: bool = False,
) -> IntegrityAuditReport:
    lab_literal_hits = lint_candidate_text(candidate_payload, forbidden_literals=forbidden_literals)
    leakage_free = redact_text(candidate_payload) == candidate_payload
    folded_payload = candidate_payload.casefold()
    holdout_leakage_hits = tuple(
        fragment
        for fragment in sealed_feedback_fragments
        if _text(fragment) and _text(fragment).casefold() in folded_payload
    )
    holdout_leakage_free = not sealed_feedback_accessed and not holdout_leakage_hits
    offline_side_effects_absent = bool(
        int(authorization_boundary.candidate_mythic_tasks_launched or 0) == 0
        and int(authorization_boundary.candidate_target_connections_opened or 0) == 0
    )
    failures: list[str] = []
    if lab_literal_hits:
        failures.append("no_lab_literals")
    if not immutable_hashes.immutable_inputs_present:
        failures.append("immutable_hashes_missing")
    if not immutable_hashes.runtime_state_byte_identical:
        failures.append("active_database_or_ledger_hash_drift")
    if not leakage_free:
        failures.append("leakage_detected")
    if not holdout_leakage_free:
        failures.append("holdout_leakage_detected")
    if not authorization_boundary.passed:
        failures.append("authorization_boundary_failed")
    if not offline_side_effects_absent:
        failures.append("offline_candidate_side_effect_detected")
    if disposable_overlay_exists_after:
        failures.append("disposable_overlay_not_cleaned")
    return IntegrityAuditReport(
        no_lab_literals=not lab_literal_hits,
        immutable_hashes_present=immutable_hashes.immutable_inputs_present,
        runtime_state_byte_identical=immutable_hashes.runtime_state_byte_identical,
        leakage_free=leakage_free,
        holdout_leakage_free=holdout_leakage_free,
        authorization_boundary_passed=authorization_boundary.passed,
        offline_side_effects_absent=offline_side_effects_absent,
        disposable_overlay_cleaned=not disposable_overlay_exists_after,
        failures=tuple(failures),
    )


def build_t2_anchor_report(
    *,
    candidate_surface: str,
    artifacts: Sequence[FrozenArtifactConfiguration],
    paired_identity: PairedIdentityValidation,
    cheap_scores: Sequence[float],
    t2_scores: Sequence[float],
    t0_disposition: str,
    t0_coverage: float | None = None,
    unscorable_new_behavior_rate: float | None = None,
    t1_substrate_status: str,
    smallest_relevant_effect: float | None,
    target_power: float | None,
    achieved_power: float | None = None,
    measured_noise: float | None,
    mde: float | None,
    bootstrap_samples: int = 1000,
    substrate_class: str = "",
    t1_reason_code: str = "",
    paired_candidate_scores: Sequence[float] | None = None,
    paired_incumbent_scores: Sequence[float] | None = None,
) -> T2AnchorReport:
    artifact_validation = validate_known_different_artifacts(
        artifacts,
        expected_surface=candidate_surface,
    )
    if (cheap_scores or t2_scores) and (
        len(cheap_scores) != len(artifacts) or len(t2_scores) != len(artifacts)
    ):
        artifact_validation = replace(
            artifact_validation,
            valid_for_instrument_plumbing=False,
            valid_for_live_ranking=False,
            failures=tuple(
                dict.fromkeys(
                    (*artifact_validation.failures, "anchor_score_artifact_count_mismatch")
                )
            ),
        )
    calibration = run_calibration_protocol(
        candidate_surface=candidate_surface,
        cheap_scores=cheap_scores,
        t2_scores=t2_scores,
        t0_disposition=t0_disposition,
        t0_coverage=t0_coverage,
        unscorable_new_behavior_rate=unscorable_new_behavior_rate,
        t1_substrate_status=t1_substrate_status,
        t2_anchor_present=artifact_validation.valid_for_live_ranking,
        smallest_relevant_effect=smallest_relevant_effect,
        target_power=target_power,
        achieved_power=achieved_power,
        measured_noise=measured_noise,
        mde=mde,
        bootstrap_samples=bootstrap_samples,
        substrate_class=substrate_class,
        t1_reason_code=t1_reason_code,
        paired_instance_count=(
            len(paired_candidate_scores)
            if paired_candidate_scores is not None
            else None
        ),
        paired_candidate_scores=paired_candidate_scores,
        paired_incumbent_scores=paired_incumbent_scores,
    )
    return T2AnchorReport(
        candidate_surface=candidate_surface,
        artifact_validation=artifact_validation,
        calibration=calibration,
        paired_identity=paired_identity,
    )


def build_provisional_readiness_report(
    *,
    candidate: CandidateArtifact,
    t0: T0TriageReport,
    surface_binding: SurfaceBinding,
    paired_identity: PairedIdentityValidation,
    t2_anchor: T2AnchorReport,
    reset_workflow: ResetWorkflowReport,
    integrity: IntegrityAuditReport,
    proposer_canary_passed: bool,
    provider_canary_passed: bool,
    authorization_boundary: AuthorizationBoundary,
) -> ReadinessReport:
    """Return the one binary typed readiness verdict for a named surface."""

    gates = (
        ReadinessGateResult(
            "AHI-04",
            candidate.valid,
            "candidate_contract_invalid" if not candidate.valid else "",
            {"candidate_id": candidate.candidate_id, "surface": candidate.surface},
        ),
        ReadinessGateResult(
            "AHI-11/AHI-12",
            (
                t0.disposition in {T0_TRIAGE_ONLY, T0_SURVIVED_TRIAGE, T0_UNSCORABLE_NEW_BEHAVIOR}
                and t0.retain_artifact_for_review is False
                and t0.promotion_evidence_passed is False
            ),
            (
                ""
                if (
                    t0.disposition in {T0_TRIAGE_ONLY, T0_SURVIVED_TRIAGE, T0_UNSCORABLE_NEW_BEHAVIOR}
                    and t0.retain_artifact_for_review is False
                    and t0.promotion_evidence_passed is False
                )
                else "t0_triage_contract_failed"
            ),
            asdict(t0),
        ),
        ReadinessGateResult(
            "AHI-13/AHI-14/AHI-39/AHI-40",
            surface_binding.verified and paired_identity.valid,
            surface_binding.reason_code or (
                "paired_identity_or_outcome_join_failed" if not paired_identity.valid else ""
            ),
            {
                "surface_binding": asdict(surface_binding),
                "paired_identity": asdict(paired_identity),
            },
        ),
        ReadinessGateResult(
            "AHI-15/AHI-16/AHI-17/AHI-33",
            t2_anchor.ranking_authorized,
            (
                "t2_anchor_or_correlation_not_ready"
                if not t2_anchor.ranking_authorized
                else ""
            ),
            {
                "artifact_validation": asdict(t2_anchor.artifact_validation),
                "calibration": t2_anchor.calibration.to_dict(),
            },
        ),
        ReadinessGateResult(
            "AHI-20/AHI-21/AHI-34",
            proposer_canary_passed is True and provider_canary_passed is True,
            "proposer_or_provider_canary_not_ready",
            {
                "proposer_canary_passed": proposer_canary_passed,
                "provider_canary_passed": provider_canary_passed,
            },
        ),
        ReadinessGateResult(
            "AHI-22",
            reset_workflow.ahi22_passed,
            reset_workflow.reason_code,
            {
                "workflow_id": reset_workflow.workflow_id,
                "ready_attestation_hash": reset_workflow.ready_attestation_hash,
                "human_steps_remaining": list(reset_workflow.human_steps_remaining),
            },
        ),
        ReadinessGateResult(
            "AHI-23/AHI-24/AHI-27/AHI-35/AHI-36/AHI-37/AHI-38",
            integrity.passed and authorization_boundary.passed,
            "integrity_or_authorization_failed",
            {
                "integrity": asdict(integrity),
                "authorization_boundary": asdict(authorization_boundary),
            },
        ),
    )
    if not surface_binding.verified:
        cheapest = "verify the named T1 substrate authorization/reset/power envelope for this candidate surface"
    elif not reset_workflow.ahi22_passed:
        cheapest = "prove one official reset-and-ready path reaches hashed ready:true without post-invocation human steps"
    elif not proposer_canary_passed or not provider_canary_passed:
        cheapest = "run the redacted multi-iteration proposer/provider canary under the effective backend"
    else:
        cheapest = "collect frozen paired T1/T2 evidence for the named candidate surface"
    return ReadinessReport.from_gates(
        candidate_surface=candidate.surface,
        gate_results=gates,
        cheapest_decisive_next_experiment=cheapest,
        notes=(
            "Phase 4 is instrumentation only; no candidate campaign is authorized.",
            "Fixture-only controls validate plumbing but cannot satisfy live ranking evidence.",
        ),
    )


def build_phase4_provisional_report() -> dict[str, Any]:
    """Emit the current fail-closed Phase 4 report from the known Phase 0 substrate facts."""

    candidate = CandidateArtifact(
        candidate_id="phase4-foundation-placeholder",
        surface="retrieval-ranking",
        surface_class=SURFACE_CLASS_STRUCTURAL,
        single_variable="artifact-only",
        target_subsystem="policy-retrieval",
        artifact_hash=content_hash({"surface": "retrieval-ranking", "phase": "phase4"}),
        hypothesis="A typed readiness foundation can reject unsupported structural ranking work.",
        mechanism="surface-bound readiness validation",
    )
    t0 = run_t0_triage((), claimed_mechanism_requires_unseen_behavior=True)
    binding = bind_candidate_surface(
        candidate.surface,
        surface_class=candidate.surface_class,
        structural_manifest=None,
    )
    empty_pair = PairedIdentityValidation(False, ("missing_live_paired_t1_t2_evidence",))
    t2 = build_t2_anchor_report(
        candidate_surface=candidate.surface,
        artifacts=build_fixture_known_different_artifacts(
            source_head="ef42c02e588ac9004c9e239b423ff1c12fde84cf",
            candidate_surface=candidate.surface,
        ),
        paired_identity=empty_pair,
        cheap_scores=(),
        t2_scores=(),
        t0_disposition=t0.disposition,
        t0_coverage=t0.coverage,
        unscorable_new_behavior_rate=t0.unscorable_new_behavior_rate,
        t1_substrate_status=binding.t1_substrate_status,
        smallest_relevant_effect=None,
        target_power=None,
        achieved_power=None,
        measured_noise=None,
        mde=None,
        substrate_class=binding.substrate_class,
        t1_reason_code=binding.reason_code,
    )
    reset = compose_official_reset_workflow(
        ready_payload={"ready": False},
        operator_invoked=False,
        human_steps_remaining=(
            "interactive foothold establishment remains unverified for repeated live tiers",
        ),
    )
    integrity = IntegrityAuditReport(
        no_lab_literals=True,
        immutable_hashes_present=False,
        runtime_state_byte_identical=False,
        leakage_free=True,
        holdout_leakage_free=True,
        authorization_boundary_passed=True,
        offline_side_effects_absent=True,
        disposable_overlay_cleaned=True,
        failures=("immutable_hashes_missing", "active_database_or_ledger_hash_drift"),
    )
    authorization = AuthorizationBoundary(
        live_work_authorized=False,
        operator_invocation_required=True,
        controller_halted_before_live=True,
        source_edits_authorized=False,
        product_default_change_authorized=False,
    )
    report = build_provisional_readiness_report(
        candidate=candidate,
        t0=t0,
        surface_binding=binding,
        paired_identity=empty_pair,
        t2_anchor=t2,
        reset_workflow=reset,
        integrity=integrity,
        proposer_canary_passed=False,
        provider_canary_passed=False,
        authorization_boundary=authorization,
    )
    return {
        "kind": "phase4_provisional_readiness_report",
        "reward_version": DENSE_REWARD_VERSION,
        "candidate": candidate.to_dict(),
        "t0": asdict(t0),
        "surface_binding": asdict(binding),
        "t2_anchor": {
            "artifact_validation": asdict(t2.artifact_validation),
            "calibration": t2.calibration.to_dict(),
            "paired_identity": asdict(t2.paired_identity),
            "ranking_authorized": t2.ranking_authorized,
        },
        "reset_workflow": {
            "workflow_id": reset.workflow_id,
            "steps": [asdict(item) for item in reset.steps],
            "ready_attestation_hash": reset.ready_attestation_hash,
            "ahi22_passed": reset.ahi22_passed,
            "reason_code": reset.reason_code,
        },
        "readiness": report.to_dict(),
    }
