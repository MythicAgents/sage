"""Shared experiment, attempt, intervention, and lineage contracts for Sage evaluation.

This module is deliberately data-only.  It does not run candidates, choose actions, or
authorize live work.  It gives the existing hillclimb/eval packages one typed contract
for the evidence they already emit so diagnostic replay cannot silently become
promotion evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROMOTION_EVIDENCE_FIELDS = (
    "artifact_integrity_passed",
    "boundary_passed",
    "policy_identity_passed",
    "causal_model_contribution_passed",
    "backend_provenance_passed",
    "candidate_efficacy_passed",
    "non_regression_passed",
    "transfer_passed",
)

OUTCOME_INDEPENDENTLY_OBSERVED = "independently_observed"
OUTCOME_SYNTHETIC_MODELED = "synthetic_modeled_reachability"
OUTCOME_DIAGNOSTIC_ONLY = "diagnostic_only"
OUTCOME_UNSCORABLE_NEW_BEHAVIOR = "unscorable_new_behavior"

READINESS_NOT_READY = "auto_harness_not_ready"
READINESS_ELIGIBLE = "eligible_for_supervised_artifact_campaign"

DECISION_OWNER_KERNEL_SINGLETON = "kernel_singleton"
DECISION_OWNER_MODEL_BRANCH = "model_branch"
DECISION_OWNER_SYMBOLIC_CONTROL = "symbolic_control"
DECISION_OWNER_FORCED_INTERVENTION = "forced_intervention"
DECISION_OWNER_OPERATOR = "operator"

ATTEMPT_SCHEMA_VERSION = 1
SURFACE_CLASS_LEXICAL_IDENTITY_SECRET = "lexical_identity_secret"
SURFACE_CLASS_STRUCTURAL = "structural"
T1_LEXICAL_UNAVAILABLE = "t1_lexical_substrate_unavailable_or_underpowered"
T1_STRUCTURAL_UNAVAILABLE = "t1_structural_substrate_unavailable_or_underpowered"
T1_SUBSTRATE_VERIFIED = "verified"
T0_TRIAGE_ONLY = "triage_only"
T0_SURVIVED_TRIAGE = "survived_triage"
T0_REJECTED_OFFLINE = "rejected_offline"
T0_UNSCORABLE_NEW_BEHAVIOR = OUTCOME_UNSCORABLE_NEW_BEHAVIOR


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class TypedVerdict:
    """Promotion evidence is a computed conjunction, never a caller-owned label."""

    artifact_integrity_passed: bool | None = None
    boundary_passed: bool | None = None
    policy_identity_passed: bool | None = None
    causal_model_contribution_passed: bool | None = None
    backend_provenance_passed: bool | None = None
    candidate_efficacy_passed: bool | None = None
    non_regression_passed: bool | None = None
    transfer_passed: bool | None = None
    operator_approved: bool | None = False
    promotion_applied: bool | None = False
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.promotion_applied is True and self.operator_approved is not True:
            raise ValueError("promotion_applied requires separate operator approval")
        object.__setattr__(
            self,
            "reason_codes",
            tuple(_text(item) for item in self.reason_codes if _text(item)),
        )

    @property
    def promotion_evidence_passed(self) -> bool:
        return all(getattr(self, field_name) is True for field_name in PROMOTION_EVIDENCE_FIELDS)

    @property
    def provenance_passed(self) -> bool | None:
        """Compatibility alias used by the frozen Phase 0 JSON schema."""
        return self.backend_provenance_passed

    @property
    def causal_contribution_passed(self) -> bool | None:
        """Compatibility alias used by the frozen Phase 0 JSON schema."""
        return self.causal_model_contribution_passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_integrity_passed": self.artifact_integrity_passed,
            "boundary_passed": self.boundary_passed,
            "policy_identity_passed": self.policy_identity_passed,
            "causal_model_contribution_passed": self.causal_model_contribution_passed,
            "causal_contribution_passed": self.causal_contribution_passed,
            "backend_provenance_passed": self.backend_provenance_passed,
            "provenance_passed": self.provenance_passed,
            "candidate_efficacy_passed": self.candidate_efficacy_passed,
            "non_regression_passed": self.non_regression_passed,
            "transfer_passed": self.transfer_passed,
            "promotion_evidence_passed": self.promotion_evidence_passed,
            "operator_approved": self.operator_approved,
            "promotion_applied": self.promotion_applied,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "TypedVerdict":
        value = value if isinstance(value, Mapping) else {}
        return cls(
            artifact_integrity_passed=_optional_bool(value.get("artifact_integrity_passed")),
            boundary_passed=_optional_bool(value.get("boundary_passed")),
            policy_identity_passed=_optional_bool(value.get("policy_identity_passed")),
            causal_model_contribution_passed=_optional_bool(
                value.get("causal_model_contribution_passed", value.get("causal_contribution_passed"))
            ),
            backend_provenance_passed=_optional_bool(
                value.get("backend_provenance_passed", value.get("provenance_passed"))
            ),
            candidate_efficacy_passed=_optional_bool(value.get("candidate_efficacy_passed")),
            non_regression_passed=_optional_bool(value.get("non_regression_passed")),
            transfer_passed=_optional_bool(value.get("transfer_passed")),
            operator_approved=_optional_bool(value.get("operator_approved")),
            promotion_applied=_optional_bool(value.get("promotion_applied")),
            reason_codes=tuple(value.get("reason_codes") or ()),
        )


@dataclass(frozen=True)
class ChildTaskLineage:
    task_id: str
    command: str
    terminal_status: str
    artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id))
        object.__setattr__(self, "command", _text(self.command))
        object.__setattr__(self, "terminal_status", _text(self.terminal_status).casefold())
        object.__setattr__(self, "artifact_ids", tuple(_text(item) for item in self.artifact_ids if _text(item)))


@dataclass(frozen=True)
class ProofLineage:
    proof_envelope_id: str
    transaction_id: str
    task_id: str
    verifier_id: str
    verifier_hash: str
    scope: str
    origin: str
    admissible_for_runtime_achievement: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "proof_envelope_id", _text(self.proof_envelope_id))
        object.__setattr__(self, "transaction_id", _text(self.transaction_id))
        object.__setattr__(self, "task_id", _text(self.task_id))
        object.__setattr__(self, "verifier_id", _text(self.verifier_id))
        object.__setattr__(self, "verifier_hash", _text(self.verifier_hash))
        object.__setattr__(self, "scope", _text(self.scope).casefold())
        object.__setattr__(self, "origin", _text(self.origin).casefold())


@dataclass(frozen=True)
class DecisionLineage:
    decision_id: str
    selected_candidate_id: str
    decision_owner: str
    effective_backend: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _text(self.decision_id))
        object.__setattr__(self, "selected_candidate_id", _text(self.selected_candidate_id))
        object.__setattr__(self, "decision_owner", _text(self.decision_owner))
        object.__setattr__(self, "effective_backend", _text(self.effective_backend))
        object.__setattr__(self, "request_id", _text(self.request_id))


@dataclass(frozen=True)
class SemanticTransactionLineage:
    transaction_id: str
    decision_id: str
    capability_id: str
    callback_id: str
    child_tasks: tuple[ChildTaskLineage, ...] = field(default_factory=tuple)
    verifier_ids: tuple[str, ...] = field(default_factory=tuple)
    proof_envelope_ids: tuple[str, ...] = field(default_factory=tuple)
    parent_transaction_id: str = ""
    wait_count: int = 0
    retry_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _text(self.transaction_id))
        object.__setattr__(self, "decision_id", _text(self.decision_id))
        object.__setattr__(self, "capability_id", _text(self.capability_id))
        object.__setattr__(self, "callback_id", _text(self.callback_id))
        object.__setattr__(self, "parent_transaction_id", _text(self.parent_transaction_id))
        object.__setattr__(self, "verifier_ids", tuple(_text(item) for item in self.verifier_ids if _text(item)))
        object.__setattr__(
            self,
            "proof_envelope_ids",
            tuple(_text(item) for item in self.proof_envelope_ids if _text(item)),
        )
        object.__setattr__(self, "wait_count", max(0, int(self.wait_count or 0)))
        object.__setattr__(self, "retry_count", max(0, int(self.retry_count or 0)))


@dataclass(frozen=True)
class LineageValidation:
    valid: bool
    failures: tuple[str, ...] = field(default_factory=tuple)


def validate_live_lineage(
    decisions: Iterable[DecisionLineage],
    transactions: Iterable[SemanticTransactionLineage],
    proofs: Iterable[ProofLineage],
) -> LineageValidation:
    """Require an exact decision -> transaction -> task -> verifier -> proof join."""

    decisions = tuple(decisions)
    transactions = tuple(transactions)
    proofs = tuple(proofs)
    decision_map = {item.decision_id: item for item in decisions if item.decision_id}
    transaction_map = {item.transaction_id: item for item in transactions if item.transaction_id}
    proof_map = {item.proof_envelope_id: item for item in proofs if item.proof_envelope_id}
    failures: list[str] = []

    if len(decision_map) != len([item for item in decisions if item.decision_id]):
        failures.append("duplicate_decision_id")
    if len(transaction_map) != len([item for item in transactions if item.transaction_id]):
        failures.append("duplicate_transaction_id")
    if len(proof_map) != len([item for item in proofs if item.proof_envelope_id]):
        failures.append("duplicate_proof_envelope_id")

    for transaction in transactions:
        tid = transaction.transaction_id or "<missing>"
        if not transaction.transaction_id:
            failures.append("transaction_missing_id")
        if not transaction.decision_id or transaction.decision_id not in decision_map:
            failures.append(f"{tid}:missing_decision_join")
        if not transaction.child_tasks:
            failures.append(f"{tid}:missing_child_task")
        if not transaction.verifier_ids:
            failures.append(f"{tid}:missing_verifier")
        if not transaction.proof_envelope_ids:
            failures.append(f"{tid}:missing_proof")
        child_task_ids = {task.task_id for task in transaction.child_tasks if task.task_id}
        if len(child_task_ids) != len(transaction.child_tasks):
            failures.append(f"{tid}:child_task_missing_or_duplicate_id")
        for proof_id in transaction.proof_envelope_ids:
            proof = proof_map.get(proof_id)
            if proof is None:
                failures.append(f"{tid}:unknown_proof:{proof_id}")
                continue
            if proof.transaction_id != transaction.transaction_id:
                failures.append(f"{tid}:proof_transaction_mismatch:{proof_id}")
            if proof.task_id not in child_task_ids:
                failures.append(f"{tid}:proof_task_mismatch:{proof_id}")
            if proof.verifier_id not in set(transaction.verifier_ids):
                failures.append(f"{tid}:proof_verifier_mismatch:{proof_id}")
            if proof.scope != "runtime" or proof.admissible_for_runtime_achievement is not True:
                failures.append(f"{tid}:inadmissible_runtime_proof:{proof_id}")

    return LineageValidation(valid=not failures, failures=tuple(failures))


@dataclass(frozen=True)
class ForcedIntervention:
    """An exact-target intervention emits labels, never policy-win credit."""

    intervention_id: str
    capability: str
    exact_target: str
    candidate_id: str = ""
    reason: str = ""
    decision_owner: str = DECISION_OWNER_FORCED_INTERVENTION
    credit_policy_win: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "intervention_id", _text(self.intervention_id))
        object.__setattr__(self, "capability", _text(self.capability).casefold())
        object.__setattr__(self, "exact_target", _text(self.exact_target))
        object.__setattr__(self, "candidate_id", _text(self.candidate_id))
        object.__setattr__(self, "reason", _text(self.reason))
        if not self.intervention_id or not self.capability or not self.exact_target:
            raise ValueError("forced interventions require intervention_id, capability, and exact_target")
        if self.credit_policy_win is not False:
            raise ValueError("forced interventions cannot receive policy-win credit")

    def matches(self, capability: Any, target: Any, candidate_id: Any = "") -> bool:
        if _text(capability).casefold() != self.capability:
            return False
        if _text(target) != self.exact_target:
            return False
        return not self.candidate_id or _text(candidate_id) == self.candidate_id


@dataclass
class TrainingExposure:
    exercised_case_ids: set[str] = field(default_factory=set)
    exercised_family_ids: set[str] = field(default_factory=set)
    exercised_topology_families: set[str] = field(default_factory=set)
    tie_count: int = 0
    loss_count: int = 0
    provider_failure_count: int = 0

    def record(
        self,
        *,
        case_id: str = "",
        family_id: str = "",
        topology_family: str = "",
        relation: str = "",
        provider_failure: bool = False,
    ) -> None:
        if _text(case_id):
            self.exercised_case_ids.add(_text(case_id))
        if _text(family_id):
            self.exercised_family_ids.add(_text(family_id))
        if _text(topology_family):
            self.exercised_topology_families.add(_text(topology_family))
        if _text(relation).casefold() == "tie":
            self.tie_count += 1
        if _text(relation).casefold() in {"loss", "regressed", "failed"}:
            self.loss_count += 1
        if provider_failure:
            self.provider_failure_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "exercised_case_ids": sorted(self.exercised_case_ids),
            "exercised_family_ids": sorted(self.exercised_family_ids),
            "exercised_topology_families": sorted(self.exercised_topology_families),
            "ties_counted": self.tie_count > 0,
            "losses_counted": self.loss_count > 0,
            "provider_failures_counted": self.provider_failure_count > 0,
            "tie_count": self.tie_count,
            "loss_count": self.loss_count,
            "provider_failure_count": self.provider_failure_count,
        }


@dataclass(frozen=True)
class CandidateArtifact:
    candidate_id: str
    surface: str
    single_variable: str
    target_subsystem: str
    artifact_hash: str
    hypothesis: str
    mechanism: str
    parent_id: str = ""
    artifact_payload_ref: str = ""
    surface_class: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_id",
            "surface",
            "single_variable",
            "target_subsystem",
            "artifact_hash",
            "hypothesis",
            "mechanism",
            "parent_id",
            "artifact_payload_ref",
            "surface_class",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))

    @property
    def valid(self) -> bool:
        return all(
            (
                self.candidate_id,
                self.surface,
                self.single_variable,
                self.target_subsystem,
                self.artifact_hash,
                self.hypothesis,
                self.mechanism,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "parent_id": self.parent_id or None,
            "surface": self.surface,
            "surface_class": self.surface_class or None,
            "single_variable": self.single_variable,
            "target_subsystem": self.target_subsystem,
            "artifact_payload_ref": self.artifact_payload_ref or None,
            "artifact_hash": self.artifact_hash,
            "hypothesis": self.hypothesis,
            "mechanism": self.mechanism,
        }


@dataclass(frozen=True)
class CandidateInvariants:
    no_source_write: bool = True
    no_live_without_operator: bool = True
    secret_redaction_required: bool = True
    no_lab_literals: bool = True
    proof_surface_unchanged: bool = True
    safety_surface_unchanged: bool = True
    evaluator_surface_unchanged: bool = True
    frontier_construction_unchanged: bool = True
    active_runtime_config_unchanged: bool = True

    @property
    def failed_invariants(self) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name, value in asdict(self).items()
            if value is not True
        )

    @property
    def passed(self) -> bool:
        return not self.failed_invariants


@dataclass(frozen=True)
class ImmutableHashes:
    evaluator_hash: str = ""
    reward_hash: str = ""
    test_hash: str = ""
    dataset_hash: str = ""
    sealed_hash: str = ""
    prompt_hash: str = ""
    policy_hash: str = ""
    active_sage_db_hash_before: str = ""
    active_phoenix_db_hash_before: str = ""
    active_ledger_hash_before: str = ""
    active_sage_db_hash_after: str = ""
    active_phoenix_db_hash_after: str = ""
    active_ledger_hash_after: str = ""

    def __post_init__(self) -> None:
        for field_name in asdict(self):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))

    @property
    def immutable_inputs_present(self) -> bool:
        return all(
            _text(getattr(self, field_name))
            for field_name in (
                "evaluator_hash",
                "reward_hash",
                "test_hash",
                "dataset_hash",
                "sealed_hash",
                "prompt_hash",
                "policy_hash",
            )
        )

    @property
    def runtime_state_byte_identical(self) -> bool:
        pairs = (
            (self.active_sage_db_hash_before, self.active_sage_db_hash_after),
            (self.active_phoenix_db_hash_before, self.active_phoenix_db_hash_after),
            (self.active_ledger_hash_before, self.active_ledger_hash_after),
        )
        present = [(before, after) for before, after in pairs if before or after]
        return bool(present) and all(before and after and before == after for before, after in present)


@dataclass(frozen=True)
class RangeIdentity:
    tier: str
    substrate_class: str
    range_id: str
    topology_family: str
    objective_id: str
    seed: str
    validity_envelope: tuple[str, ...]
    paired_instance_id: str
    presentation_order: str = ""
    budget_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in (
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
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(
            self,
            "validity_envelope",
            tuple(_text(item) for item in self.validity_envelope if _text(item)),
        )


@dataclass(frozen=True)
class ResetIdentity:
    workflow_id: str
    snapshot_id: str = ""
    ready_attestation_hash: str = ""
    operator_invoked: bool = False
    unattended_after_invocation: bool = False
    human_steps_remaining: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workflow_id", _text(self.workflow_id))
        object.__setattr__(self, "snapshot_id", _text(self.snapshot_id))
        object.__setattr__(self, "ready_attestation_hash", _text(self.ready_attestation_hash))
        object.__setattr__(
            self,
            "human_steps_remaining",
            tuple(_text(item) for item in self.human_steps_remaining if _text(item)),
        )

    @property
    def ahi22_passed(self) -> bool:
        return bool(
            self.workflow_id
            and self.ready_attestation_hash
            and self.operator_invoked is True
            and self.unattended_after_invocation is True
            and not self.human_steps_remaining
        )


@dataclass(frozen=True)
class PolicyIdentity:
    mode: str
    version: str
    selection_contract_hash: str
    decision_owner: str
    null_treatment: bool = False
    forced_intervention: bool = False

    def __post_init__(self) -> None:
        for field_name in ("mode", "version", "selection_contract_hash", "decision_owner"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))


@dataclass(frozen=True)
class ModelIdentity:
    configured_backend: str
    effective_backend: str
    request_id: str
    prompt_hash: str
    request_schema_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "configured_backend",
            "effective_backend",
            "request_id",
            "prompt_hash",
            "request_schema_hash",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))


@dataclass(frozen=True)
class FrontierIdentity:
    raw_candidate_count: int
    admissible_candidate_count: int
    candidate_ids: tuple[str, ...]
    candidate_set_hash: str
    ordered_frontier_hash: str
    selected_candidate_id: str = ""
    symbolic_counterfactual_candidate_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_candidate_count", max(0, int(self.raw_candidate_count or 0)))
        object.__setattr__(self, "admissible_candidate_count", max(0, int(self.admissible_candidate_count or 0)))
        object.__setattr__(
            self,
            "candidate_ids",
            tuple(_text(item) for item in self.candidate_ids if _text(item)),
        )
        for field_name in (
            "candidate_set_hash",
            "ordered_frontier_hash",
            "selected_candidate_id",
            "symbolic_counterfactual_candidate_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))


@dataclass(frozen=True)
class ObservedOutcome:
    objective_proven: bool
    clean_stop: bool
    proof_envelope_ids: tuple[str, ...]
    score_source: str = OUTCOME_INDEPENDENTLY_OBSERVED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proof_envelope_ids",
            tuple(_text(item) for item in self.proof_envelope_ids if _text(item)),
        )
        object.__setattr__(self, "score_source", _text(self.score_source))

    @property
    def independently_verified(self) -> bool:
        return self.score_source == OUTCOME_INDEPENDENTLY_OBSERVED and bool(self.proof_envelope_ids)


@dataclass(frozen=True)
class Measurement:
    reward_version: str = ""
    paired_instance_count: int | None = None
    t0_coverage: float | None = None
    unscorable_new_behavior_rate: float | None = None
    smallest_relevant_effect: float | None = None
    target_power: float | None = None
    achieved_power: float | None = None
    measured_noise: float | None = None
    mde: float | None = None
    paired_effect_mean: float | None = None
    paired_effect_ci95: dict[str, float] | None = None
    rank_correlation: float | None = None
    rank_correlation_ci95: dict[str, float] | None = None
    inversion_count: int | None = None
    proposer_canary_passed: bool | None = None
    provider_canary_passed: bool | None = None
    reset_automation_passed: bool | None = None
    t0_disposition: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reward_version", _text(self.reward_version))
        object.__setattr__(self, "t0_disposition", _text(self.t0_disposition))


@dataclass(frozen=True)
class RepairEvidence:
    proposed_repair: str = ""
    repair_applied: bool = False
    independently_verified_repair_outcome: bool | None = None
    label_source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposed_repair", _text(self.proposed_repair))
        object.__setattr__(self, "label_source", _text(self.label_source))


@dataclass(frozen=True)
class ProviderFailure:
    attempt_id: str
    provider: str
    effective_backend: str
    category: str
    retriable: bool
    terminal: bool
    retry_index: int = 0
    route_index: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        for field_name in ("attempt_id", "provider", "effective_backend", "category", "detail"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(self, "retry_index", max(0, int(self.retry_index or 0)))
        object.__setattr__(self, "route_index", max(0, int(self.route_index or 0)))


@dataclass(frozen=True)
class OperationalRisk:
    boundary_violation_count: int = 0
    secret_exposure_count: int = 0
    lab_literal_count: int = 0
    unauthorized_live_request_count: int = 0
    active_runtime_config_mutation_count: int = 0

    @property
    def risk_level(self) -> str:
        total = sum(int(value or 0) for value in asdict(self).values())
        if total == 0:
            return "none"
        if total == 1:
            return "low"
        if total <= 3:
            return "medium"
        return "high"

    @property
    def passed(self) -> bool:
        return all(int(value or 0) == 0 for value in asdict(self).values())


@dataclass(frozen=True)
class AuthorizationBoundary:
    live_work_authorized: bool = False
    operator_invocation_required: bool = True
    controller_halted_before_live: bool = True
    source_edits_authorized: bool = False
    product_default_change_authorized: bool = False
    candidate_mythic_tasks_launched: int = 0
    candidate_target_connections_opened: int = 0
    official_workflow_only: bool = True

    @property
    def passed(self) -> bool:
        return bool(
            self.operator_invocation_required is True
            and self.controller_halted_before_live is True
            and self.product_default_change_authorized is False
            and int(self.candidate_mythic_tasks_launched or 0) == 0
            and int(self.candidate_target_connections_opened or 0) == 0
            and self.official_workflow_only is True
        )


@dataclass(frozen=True)
class CostMeasurement:
    semantic_transaction_count: int = 0
    child_task_count: int = 0
    wait_count: int = 0
    retry_count: int = 0
    wall_time_seconds: float = 0.0
    model_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    monetary_cost_usd: float = 0.0
    termination_reason: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "semantic_transaction_count",
            "child_task_count",
            "wait_count",
            "retry_count",
            "model_call_count",
            "input_tokens",
            "output_tokens",
        ):
            object.__setattr__(self, field_name, max(0, int(getattr(self, field_name) or 0)))
        for field_name in ("wall_time_seconds", "monetary_cost_usd"):
            object.__setattr__(self, field_name, max(0.0, float(getattr(self, field_name) or 0.0)))
        object.__setattr__(self, "termination_reason", _text(self.termination_reason))


@dataclass(frozen=True)
class EvalResult:
    candidate_id: str
    tier: str
    mode: str
    disposition: str
    outcome_source: str
    measurement: Measurement
    typed_verdict: TypedVerdict = field(default_factory=TypedVerdict)
    dense_reward: dict[str, Any] = field(default_factory=dict)
    range_identity: RangeIdentity | None = None
    reset_identity: ResetIdentity | None = None
    model_identity: ModelIdentity | None = None
    authorization_boundary: AuthorizationBoundary = field(default_factory=AuthorizationBoundary)
    independently_observed_outcome: ObservedOutcome | None = None
    lineage_validation: LineageValidation | None = None
    provider_failures: tuple[ProviderFailure, ...] = field(default_factory=tuple)
    operational_risk: OperationalRisk = field(default_factory=OperationalRisk)
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("candidate_id", "tier", "mode", "disposition", "outcome_source"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(
            self,
            "reason_codes",
            tuple(_text(item) for item in self.reason_codes if _text(item)),
        )

    @property
    def independently_observed(self) -> bool:
        return bool(
            self.outcome_source == OUTCOME_INDEPENDENTLY_OBSERVED
            and self.independently_observed_outcome is not None
            and self.independently_observed_outcome.independently_verified
            and self.lineage_validation is not None
            and self.lineage_validation.valid
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "tier": self.tier,
            "mode": self.mode,
            "disposition": self.disposition,
            "outcome_source": self.outcome_source,
            "measurement": asdict(self.measurement),
            "typed_verdict": self.typed_verdict.to_dict(),
            "dense_reward": dict(self.dense_reward),
            "range_identity": asdict(self.range_identity) if self.range_identity else None,
            "reset_identity": asdict(self.reset_identity) if self.reset_identity else None,
            "model_identity": asdict(self.model_identity) if self.model_identity else None,
            "authorization_boundary": asdict(self.authorization_boundary),
            "independently_observed_outcome": (
                asdict(self.independently_observed_outcome) if self.independently_observed_outcome else None
            ),
            "lineage_validation": (
                asdict(self.lineage_validation) if self.lineage_validation else None
            ),
            "provider_failures": [asdict(item) for item in self.provider_failures],
            "operational_risk": asdict(self.operational_risk),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class ReadinessGateResult:
    gate_id: str
    passed: bool
    reason_code: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_id", _text(self.gate_id))
        object.__setattr__(self, "reason_code", _text(self.reason_code))


@dataclass(frozen=True)
class ReadinessReport:
    candidate_surface: str
    gate_results: tuple[ReadinessGateResult, ...]
    cheapest_decisive_next_experiment: str
    readiness_decision: str = READINESS_NOT_READY
    failed_gates: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_surface", _text(self.candidate_surface))
        object.__setattr__(
            self,
            "failed_gates",
            tuple(_text(item) for item in self.failed_gates if _text(item)),
        )
        object.__setattr__(
            self,
            "notes",
            tuple(_text(item) for item in self.notes if _text(item)),
        )
        object.__setattr__(
            self,
            "cheapest_decisive_next_experiment",
            _text(self.cheapest_decisive_next_experiment),
        )

    @classmethod
    def from_gates(
        cls,
        *,
        candidate_surface: str,
        gate_results: Sequence[ReadinessGateResult],
        cheapest_decisive_next_experiment: str,
        notes: Sequence[str] = (),
    ) -> "ReadinessReport":
        gates = tuple(gate_results)
        failed = tuple(
            dict.fromkeys(
                gate.reason_code or gate.gate_id
                for gate in gates
                if gate.passed is not True
            )
        )
        decision = READINESS_ELIGIBLE if not failed else READINESS_NOT_READY
        return cls(
            candidate_surface=candidate_surface,
            gate_results=gates,
            cheapest_decisive_next_experiment=cheapest_decisive_next_experiment,
            readiness_decision=decision,
            failed_gates=failed,
            notes=tuple(notes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_surface": self.candidate_surface,
            "readiness_decision": self.readiness_decision,
            "failed_gates": list(self.failed_gates),
            "cheapest_decisive_next_experiment": self.cheapest_decisive_next_experiment,
            "gate_results": [asdict(item) for item in self.gate_results],
            "notes": list(self.notes),
        }


@dataclass
class AttemptContract:
    attempt_id: str
    experiment_id: str
    treatment_id: str
    campaign_id: str = ""
    outcome_source: str = OUTCOME_DIAGNOSTIC_ONLY
    disposition: str = "triage_only"
    typed_verdict: TypedVerdict = field(default_factory=TypedVerdict)
    forced_intervention: ForcedIntervention | None = None
    decision_lineage: tuple[DecisionLineage, ...] = field(default_factory=tuple)
    semantic_transactions: tuple[SemanticTransactionLineage, ...] = field(default_factory=tuple)
    proof_lineage: tuple[ProofLineage, ...] = field(default_factory=tuple)
    training_exposure: TrainingExposure = field(default_factory=TrainingExposure)
    independently_observed_outcome: dict[str, Any] | None = None
    modeled_score: dict[str, Any] | None = None
    schema_version: int = ATTEMPT_SCHEMA_VERSION
    supersedes_attempt_id: str = ""
    created_at: str = field(default_factory=utc_now)
    started_at: str = ""
    completed_at: str = ""
    phase: str = "phase4"
    status: str = "planned"
    source: dict[str, Any] = field(default_factory=dict)
    immutable_hashes: ImmutableHashes | None = None
    candidate: CandidateArtifact | None = None
    candidate_invariants: CandidateInvariants = field(default_factory=CandidateInvariants)
    range_identity: RangeIdentity | None = None
    reset_identity: ResetIdentity | None = None
    policy_identity: PolicyIdentity | None = None
    model_identity: ModelIdentity | None = None
    frontier_identity: FrontierIdentity | None = None
    authorization_boundary: AuthorizationBoundary = field(default_factory=AuthorizationBoundary)
    measurement: Measurement = field(default_factory=Measurement)
    repair_evidence: RepairEvidence = field(default_factory=RepairEvidence)
    cost: CostMeasurement = field(default_factory=CostMeasurement)
    provider_failures: tuple[ProviderFailure, ...] = field(default_factory=tuple)
    operational_risk: OperationalRisk = field(default_factory=OperationalRisk)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in (
            "attempt_id",
            "experiment_id",
            "treatment_id",
            "campaign_id",
            "supersedes_attempt_id",
            "created_at",
            "started_at",
            "completed_at",
            "phase",
            "status",
        ):
            setattr(self, field_name, _text(getattr(self, field_name)))
        self.notes = tuple(_text(item) for item in self.notes if _text(item))
        self.provider_failures = tuple(self.provider_failures)

    @property
    def lineage_validation(self) -> LineageValidation:
        return validate_live_lineage(self.decision_lineage, self.semantic_transactions, self.proof_lineage)

    @property
    def policy_win_eligible(self) -> bool:
        return (
            self.forced_intervention is None
            and self.outcome_source == OUTCOME_INDEPENDENTLY_OBSERVED
            and self.typed_verdict.candidate_efficacy_passed is True
        )

    @property
    def retain_artifact_for_review(self) -> bool:
        return (
            self.policy_win_eligible
            and self.typed_verdict.promotion_evidence_passed
            and self.lineage_validation.valid
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "supersedes_attempt_id": self.supersedes_attempt_id or None,
            "experiment_id": self.experiment_id,
            "campaign_id": self.campaign_id or None,
            "treatment_id": self.treatment_id,
            "created_at": self.created_at,
            "started_at": self.started_at or None,
            "completed_at": self.completed_at or None,
            "phase": self.phase,
            "status": self.status,
            "outcome_source": self.outcome_source,
            "disposition": self.disposition,
            "source": dict(self.source),
            "immutable_hashes": asdict(self.immutable_hashes) if self.immutable_hashes else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "candidate_invariants": asdict(self.candidate_invariants),
            "typed_verdict": self.typed_verdict.to_dict(),
            "forced_intervention": asdict(self.forced_intervention) if self.forced_intervention else None,
            "range_identity": asdict(self.range_identity) if self.range_identity else None,
            "reset_identity": asdict(self.reset_identity) if self.reset_identity else None,
            "policy_identity": asdict(self.policy_identity) if self.policy_identity else None,
            "model_identity": asdict(self.model_identity) if self.model_identity else None,
            "frontier_identity": asdict(self.frontier_identity) if self.frontier_identity else None,
            "decision_lineage": [asdict(item) for item in self.decision_lineage],
            "semantic_transactions": [asdict(item) for item in self.semantic_transactions],
            "proof_lineage": [asdict(item) for item in self.proof_lineage],
            "training_exposure": self.training_exposure.to_dict(),
            "independently_observed_outcome": self.independently_observed_outcome,
            "modeled_score": self.modeled_score,
            "authorization_boundary": asdict(self.authorization_boundary),
            "measurement": asdict(self.measurement),
            "repair": asdict(self.repair_evidence),
            "cost": asdict(self.cost),
            "provider_failures": [asdict(item) for item in self.provider_failures],
            "operational_risk": {
                "risk_level": self.operational_risk.risk_level,
                **asdict(self.operational_risk),
            },
            "notes": list(self.notes),
            "lineage_validation": {
                "valid": self.lineage_validation.valid,
                "failures": list(self.lineage_validation.failures),
            },
            "policy_win_eligible": self.policy_win_eligible,
            "retain_artifact_for_review": self.retain_artifact_for_review,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_json_line(self) -> str:
        return canonical_json(self.to_dict())


@dataclass
class CampaignContract:
    campaign_id: str
    experiment_id: str
    attempts: list[AttemptContract] = field(default_factory=list)

    def append_attempt(self, attempt: AttemptContract) -> None:
        if attempt.experiment_id != self.experiment_id:
            raise ValueError("attempt experiment_id does not match campaign")
        if attempt.campaign_id and attempt.campaign_id != self.campaign_id:
            raise ValueError("attempt campaign_id does not match campaign")
        self.attempts.append(attempt)

    @property
    def training_exposure(self) -> TrainingExposure:
        exposure = TrainingExposure()
        for attempt in self.attempts:
            source = attempt.training_exposure
            exposure.exercised_case_ids.update(source.exercised_case_ids)
            exposure.exercised_family_ids.update(source.exercised_family_ids)
            exposure.exercised_topology_families.update(source.exercised_topology_families)
            exposure.tie_count += source.tie_count
            exposure.loss_count += source.loss_count
            exposure.provider_failure_count += source.provider_failure_count
        return exposure

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "experiment_id": self.experiment_id,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "training_exposure": self.training_exposure.to_dict(),
            "content_hash": content_hash([attempt.to_dict() for attempt in self.attempts]),
        }


def blocked_action_remains_blocked(
    *,
    prior_status: str,
    transition_status: str,
    removes_blocker: bool = False,
) -> bool:
    """Return whether a blocked action must remain blocked after one transition."""

    if _text(prior_status).casefold() != "blocked":
        return True
    if removes_blocker:
        return True
    return _text(transition_status).casefold() == "blocked"


def append_attempt_record(path: str | Path, attempt: AttemptContract) -> str:
    """Append one immutable attempt row; corrections are new superseding rows."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    row = {"record_hash": attempt.content_hash, **attempt.to_dict()}
    with output.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(row))
        handle.write("\n")
    return str(output)


def load_attempt_records(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def validate_attempt_contract(attempt: AttemptContract) -> tuple[str, ...]:
    """Validate a countable attempt against the frozen Phase 0/Phase 4 contract."""

    failures: list[str] = []
    if int(attempt.schema_version or 0) != ATTEMPT_SCHEMA_VERSION:
        failures.append("schema_version_mismatch")
    for field_name in ("attempt_id", "experiment_id", "treatment_id", "created_at", "phase", "status"):
        if not _text(getattr(attempt, field_name)):
            failures.append(f"missing_{field_name}")
    if attempt.candidate is None or not attempt.candidate.valid:
        failures.append("candidate_contract_invalid")
    if attempt.candidate_invariants.passed is not True:
        failures.extend(f"candidate_invariant_failed:{item}" for item in attempt.candidate_invariants.failed_invariants)
    if attempt.immutable_hashes is None or attempt.immutable_hashes.immutable_inputs_present is not True:
        failures.append("immutable_hashes_missing")
    if attempt.authorization_boundary.passed is not True:
        failures.append("authorization_boundary_failed")
    if attempt.outcome_source == OUTCOME_INDEPENDENTLY_OBSERVED and attempt.lineage_validation.valid is not True:
        failures.append("independently_observed_outcome_missing_exact_lineage")
    return tuple(dict.fromkeys(failures))


def validate_attempt_records(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Validate append-only record hashes and supersession links without rewriting rows."""

    failures: list[str] = []
    prior_attempt_ids: set[str] = set()
    for index, row in enumerate(rows):
        payload = dict(row)
        record_hash = _text(payload.pop("record_hash", ""))
        if not record_hash:
            failures.append(f"row_{index}:missing_record_hash")
        elif record_hash != content_hash(payload):
            failures.append(f"row_{index}:record_hash_mismatch")
        attempt_id = _text(payload.get("attempt_id"))
        if not attempt_id:
            failures.append(f"row_{index}:missing_attempt_id")
        elif attempt_id in prior_attempt_ids:
            failures.append(f"row_{index}:duplicate_attempt_id")
        supersedes = _text(payload.get("supersedes_attempt_id"))
        if supersedes and supersedes not in prior_attempt_ids:
            failures.append(f"row_{index}:unknown_supersedes_attempt_id")
        prior_attempt_ids.add(attempt_id)
    return tuple(dict.fromkeys(failures))
