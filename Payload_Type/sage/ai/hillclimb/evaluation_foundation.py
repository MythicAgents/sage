"""Phase 4 verifier-first auto-harness-improvement evaluation foundation.

This module is intentionally pure and fail-closed.  It composes the existing typed
experiment contract, dense reward, trajectory-v2 records, and gate-experiment math
into one provisional readiness report.  It does not run candidates, task Mythic,
mutate runtime state, or authorize a live campaign.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

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


# M2 split metadata stays evaluator-side.  It deliberately identifies semantic
# ancestry explicitly instead of trying to infer it from case prose or live-range
# names.  A producer that cannot supply the ancestry is development-only.
EVALUATION_ROLES = frozenset({"dev", "promotion_validation", "final_audit"})
SPLIT_USE_SCHEMA = "sage-evaluation-split-use-v1"


@dataclass(frozen=True)
class SemanticAncestry:
    source_id: str
    parent_source_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id))
        object.__setattr__(self, "parent_source_id", _text(self.parent_source_id))


@dataclass(frozen=True)
class EvaluationCohort:
    cohort_id: str
    role: str
    topology_cluster_id: str
    source_case_ids: tuple[str, ...]
    semantic_source_ids: tuple[str, ...]
    prospective_budget_id: str = ""
    prospective_case_budget: int = 0
    prospective_look_count: int = 0
    multiplicity_policy_id: str = ""
    multiplicity_alpha: float | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "cohort_id", "role", "topology_cluster_id", "prospective_budget_id", "multiplicity_policy_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(self, "role", self.role.casefold())
        object.__setattr__(
            self, "source_case_ids", tuple(_text(item) for item in self.source_case_ids if _text(item)),
        )
        object.__setattr__(
            self, "semantic_source_ids", tuple(_text(item) for item in self.semantic_source_ids if _text(item)),
        )
        object.__setattr__(self, "prospective_case_budget", max(0, int(self.prospective_case_budget or 0)))
        object.__setattr__(self, "prospective_look_count", max(0, int(self.prospective_look_count or 0)))
        alpha = self.multiplicity_alpha
        object.__setattr__(self, "multiplicity_alpha", float(alpha) if isinstance(alpha, (int, float)) and not isinstance(alpha, bool) else None)


@dataclass(frozen=True)
class FinalAuditReceipt:
    """Append-only metadata binding one final cohort to one computed report."""

    cohort_id: str
    report_id: str
    source_case_ids: tuple[str, ...]
    reducer_name: str
    result_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("cohort_id", "report_id", "reducer_name", "result_sha256"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(
            self, "source_case_ids", tuple(_text(item) for item in self.source_case_ids if _text(item)),
        )


@dataclass(frozen=True)
class EvaluationSplitReport:
    cohorts: tuple[EvaluationCohort, ...]
    ancestry_roots: dict[str, str]
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class SplitUseRecord:
    """One append-only reservation or final-report retirement, with no case content."""

    record_kind: str
    reservation_id: str
    registry_id: str
    release_cohort_id: str
    cohort_id: str
    role: str
    topology_cluster_id: str
    ancestry_roots: tuple[str, ...]
    source_case_ids: tuple[str, ...]
    prospective_budget_id: str = ""
    reducer_name: str = ""
    result_sha256: str = ""
    report_id: str = ""
    multiplicity_alpha_allocation: float | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "record_kind", "reservation_id", "registry_id", "release_cohort_id", "cohort_id", "role",
            "topology_cluster_id", "prospective_budget_id", "reducer_name", "result_sha256", "report_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(self, "record_kind", self.record_kind.casefold())
        object.__setattr__(self, "role", self.role.casefold())
        object.__setattr__(self, "ancestry_roots", tuple(_text(item) for item in self.ancestry_roots if _text(item)))
        object.__setattr__(self, "source_case_ids", tuple(_text(item) for item in self.source_case_ids if _text(item)))
        allocation = self.multiplicity_alpha_allocation
        object.__setattr__(
            self, "multiplicity_alpha_allocation",
            float(allocation) if isinstance(allocation, (int, float)) and not isinstance(allocation, bool) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SPLIT_USE_SCHEMA,
            "record_kind": self.record_kind,
            "reservation_id": self.reservation_id,
            "registry_id": self.registry_id,
            "release_cohort_id": self.release_cohort_id,
            "cohort_id": self.cohort_id,
            "role": self.role,
            "topology_cluster_id": self.topology_cluster_id,
            "ancestry_roots": list(self.ancestry_roots),
            "source_case_ids": list(self.source_case_ids),
            "prospective_budget_id": self.prospective_budget_id or None,
            "reducer_name": self.reducer_name or None,
            "result_sha256": self.result_sha256 or None,
            "report_id": self.report_id or None,
            "multiplicity_alpha_allocation": self.multiplicity_alpha_allocation,
        }


@dataclass(frozen=True)
class HeadlineLiftDecision:
    measurement_scope: str
    headline_lift_allowed: bool
    reason_codes: tuple[str, ...]
    reducer_name: str
    reduced_result_sha256: str
    final_audit_receipt: FinalAuditReceipt | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "measurement_scope": self.measurement_scope,
            "headline_lift_allowed": self.headline_lift_allowed,
            "headline_lift_reason_codes": list(self.reason_codes),
            "headline_lift_reducer": self.reducer_name or None,
            "headline_lift_result_sha256": self.reduced_result_sha256 or None,
            "final_audit_receipt": (
                {
                    "cohort_id": self.final_audit_receipt.cohort_id,
                    "report_id": self.final_audit_receipt.report_id,
                    "source_case_ids": list(self.final_audit_receipt.source_case_ids),
                    "reducer_name": self.final_audit_receipt.reducer_name,
                    "result_sha256": self.final_audit_receipt.result_sha256,
                }
                if self.final_audit_receipt is not None else None
            ),
        }


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


def _ancestry_roots(nodes: Sequence[SemanticAncestry]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Resolve each source to its transitive root, rejecting unknown parents and cycles."""

    parents: dict[str, str] = {}
    failures: list[str] = []
    for node in nodes:
        if not node.source_id:
            failures.append("semantic_ancestry_missing_source_id")
            continue
        if node.source_id in parents:
            failures.append(f"duplicate_semantic_source:{node.source_id}")
            continue
        parents[node.source_id] = node.parent_source_id
    roots: dict[str, str] = {}
    for source_id in parents:
        seen: list[str] = []
        current = source_id
        while current:
            if current in seen:
                failures.append(f"semantic_ancestry_cycle:{source_id}")
                break
            seen.append(current)
            parent = parents.get(current)
            if parent is None:
                failures.append(f"semantic_ancestry_unknown_parent:{current}")
                break
            if not parent:
                roots[source_id] = current
                break
            current = parent
    return roots, tuple(dict.fromkeys(failures))


def validate_evaluation_split(
    cohorts: Sequence[EvaluationCohort],
    ancestry: Sequence[SemanticAncestry],
) -> EvaluationSplitReport:
    """Validate M2 roles over complete topology clusters and semantic relatives.

    Semantic similarity is an explicit producer assertion.  This validator protects
    known reconstructed/renamed descendants transitively; it does not claim to
    discover an unrecorded relationship from a case name or outcome packet.
    """

    roots, ancestry_failures = _ancestry_roots(ancestry)
    failures: list[str] = list(ancestry_failures)
    cohort_ids: set[str] = set()
    case_owners: dict[str, str] = {}
    cluster_roles: dict[str, set[str]] = {}
    ancestry_roles: dict[str, set[str]] = {}
    for cohort in cohorts:
        if not cohort.cohort_id or cohort.cohort_id in cohort_ids:
            failures.append(f"duplicate_or_missing_cohort_id:{cohort.cohort_id or '<missing>'}")
        cohort_ids.add(cohort.cohort_id)
        if cohort.role not in EVALUATION_ROLES:
            failures.append(f"unknown_evaluation_role:{cohort.role or '<missing>'}")
        if not cohort.topology_cluster_id:
            failures.append(f"{cohort.cohort_id}:missing_topology_cluster_id")
        if not cohort.source_case_ids:
            failures.append(f"{cohort.cohort_id}:missing_source_case_ids")
        if not cohort.semantic_source_ids:
            failures.append(f"{cohort.cohort_id}:missing_semantic_source_ids")
        if cohort.role in {"promotion_validation", "final_audit"} and (
            not cohort.prospective_budget_id
            or cohort.prospective_case_budget <= 0
            or len(cohort.source_case_ids) > cohort.prospective_case_budget
        ):
            failures.append(f"{cohort.cohort_id}:validation_role_missing_or_exceeding_prospective_case_budget")
        if cohort.role == "promotion_validation" and (
            not cohort.multiplicity_policy_id
            or cohort.multiplicity_policy_id.casefold() != "bonferroni"
            or cohort.multiplicity_alpha is None
            or not 0.0 < cohort.multiplicity_alpha <= 1.0
            or cohort.prospective_look_count <= 0
        ):
            failures.append(f"{cohort.cohort_id}:promotion_validation_missing_multiplicity_policy")
        for case_id in cohort.source_case_ids:
            owner = case_owners.setdefault(case_id, cohort.cohort_id)
            if owner != cohort.cohort_id:
                failures.append(f"source_case_cross_role_or_cohort:{case_id}")
        cluster_roles.setdefault(cohort.topology_cluster_id, set()).add(cohort.role)
        for source_id in cohort.semantic_source_ids:
            root = roots.get(source_id)
            if root is None:
                failures.append(f"{cohort.cohort_id}:semantic_source_not_in_ancestry:{source_id}")
                continue
            ancestry_roles.setdefault(root, set()).add(cohort.role)
    for cluster_id, roles in cluster_roles.items():
        if len(roles) > 1:
            failures.append(f"topology_cluster_cross_role:{cluster_id}")
    for root, roles in ancestry_roles.items():
        if len(roles) > 1:
            failures.append(f"semantic_ancestry_cross_role:{root}")
    return EvaluationSplitReport(
        cohorts=tuple(cohorts),
        ancestry_roots=roots,
        failures=tuple(dict.fromkeys(failures)),
    )


@contextmanager
def _split_use_lock(path: str | Path):
    """Serialize local evaluator reservations; the history has one writer at a time."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(f"{output.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield output
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_split_use_record(output: Path, record: SplitUseRecord) -> str:
    payload = record.to_dict()
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"record_hash": content_hash(payload), **payload}, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return str(output)


def append_split_use_record(path: str | Path, record: SplitUseRecord) -> str:
    """Durably append evaluator metadata before any protected-case opener runs."""

    with _split_use_lock(path) as output:
        return _append_split_use_record(output, record)


def _load_split_use_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with Path(path).open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_split_use_history(path: str | Path) -> list[dict[str, Any]]:
    with _split_use_lock(path) as output:
        return _load_split_use_history(output)


def validate_split_use_history(rows: Sequence[Mapping[str, Any]]) -> tuple[SplitUseRecord, ...]:
    """Validate an append-only split-use history without reading case contents."""

    records: list[SplitUseRecord] = []
    reservations: dict[str, SplitUseRecord] = {}
    retired: set[str] = set()
    for index, row in enumerate(rows):
        payload = dict(row)
        record_hash = _text(payload.pop("record_hash", ""))
        if record_hash != content_hash(payload):
            raise ValueError(f"split_use_row_{index}:record_hash_mismatch")
        if payload.pop("schema", "") != SPLIT_USE_SCHEMA:
            raise ValueError(f"split_use_row_{index}:schema_mismatch")
        record = SplitUseRecord(**payload)
        if record.record_kind not in {"reservation", "retirement"}:
            raise ValueError(f"split_use_row_{index}:unknown_record_kind")
        if not all((record.reservation_id, record.registry_id, record.release_cohort_id, record.cohort_id, record.role)):
            raise ValueError(f"split_use_row_{index}:missing_identity")
        if record.record_kind == "reservation":
            if record.reservation_id in reservations:
                raise ValueError(f"split_use_row_{index}:duplicate_reservation")
            if not record.source_case_ids or not record.ancestry_roots or not record.topology_cluster_id:
                raise ValueError(f"split_use_row_{index}:reservation_metadata_missing")
            reservations[record.reservation_id] = record
        else:
            reservation = reservations.get(record.reservation_id)
            if reservation is None:
                raise ValueError(f"split_use_row_{index}:retirement_without_reservation")
            if record.reservation_id in retired:
                raise ValueError(f"split_use_row_{index}:duplicate_retirement")
            if (record.role != "final_audit" or not record.reducer_name or not record.result_sha256
                    or not record.report_id):
                raise ValueError(f"split_use_row_{index}:retirement_binding_missing")
            if any(
                getattr(record, field_name) != getattr(reservation, field_name)
                for field_name in ("registry_id", "release_cohort_id", "cohort_id", "role", "topology_cluster_id", "ancestry_roots", "source_case_ids", "prospective_budget_id", "multiplicity_alpha_allocation")
            ):
                raise ValueError(f"split_use_row_{index}:retirement_identity_mismatch")
            retired.add(record.reservation_id)
        if record.role == "promotion_validation" and (
            record.record_kind != "reservation" or record.multiplicity_alpha_allocation is None
            or not 0.0 < record.multiplicity_alpha_allocation <= 1.0
        ):
            raise ValueError(f"split_use_row_{index}:promotion_multiplicity_allocation_missing")
        records.append(record)
    return tuple(records)


def reserve_evaluation_cohort(
    history_path: str | Path,
    *,
    registry_id: str,
    release_cohort_id: str,
    cohort_id: str,
    cohorts: Sequence[EvaluationCohort],
    ancestry: Sequence[SemanticAncestry],
    reservation_id: str,
    opener: Callable[[tuple[str, ...]], Any],
) -> Any:
    """Reserve first, then invoke the supplied case opener.

    The reservation itself consumes a final use, including when ``opener`` raises
    or the process stops before retirement is written.  Only registered case IDs
    reach the opener; this helper never reads an outcome packet or path itself.
    """

    split = validate_evaluation_split(cohorts, ancestry)
    if not split.valid:
        raise ValueError("evaluation_split_invalid:" + ";".join(split.failures))
    registry = _text(registry_id)
    release = _text(release_cohort_id)
    reservation = _text(reservation_id)
    cohort = next((item for item in cohorts if item.cohort_id == _text(cohort_id)), None)
    if not registry or not release or not reservation or cohort is None:
        raise ValueError("split_reservation_identity_missing")
    roots = tuple(sorted({split.ancestry_roots[source] for source in cohort.semantic_source_ids}))
    record = SplitUseRecord(
        record_kind="reservation", reservation_id=reservation, registry_id=registry,
        release_cohort_id=release, cohort_id=cohort.cohort_id, role=cohort.role,
        topology_cluster_id=cohort.topology_cluster_id, ancestry_roots=roots,
        source_case_ids=cohort.source_case_ids,
        prospective_budget_id=cohort.prospective_budget_id,
        multiplicity_alpha_allocation=(
            cohort.multiplicity_alpha / cohort.prospective_look_count
            if cohort.role == "promotion_validation" and cohort.multiplicity_alpha is not None
            else None
        ),
    )
    # The read/check/append sequence is one serialized local operation.  The
    # opener runs only after its durable reservation is released from the lock.
    with _split_use_lock(history_path) as output:
        existing = validate_split_use_history(_load_split_use_history(output))
        if any(item.record_kind == "reservation" and item.reservation_id == reservation for item in existing):
            raise ValueError("split_reservation_id_already_used")
        for prior in existing:
            if prior.record_kind != "reservation" or prior.registry_id != registry:
                continue
            same_family = prior.topology_cluster_id == cohort.topology_cluster_id or bool(set(prior.ancestry_roots) & set(roots))
            if cohort.role in {"promotion_validation", "final_audit"} and prior.role == "dev" and same_family:
                raise ValueError("protected_cohort_previously_exposed_to_dev")
            if cohort.role in {"promotion_validation", "final_audit"} and prior.role in {"promotion_validation", "final_audit"} and prior.role != cohort.role and same_family:
                raise ValueError("protected_cohort_previously_assigned_to_different_role")
            if cohort.role == "final_audit" and prior.role == "final_audit" and same_family:
                raise ValueError("final_audit_cohort_already_reserved")
        if cohort.role == "promotion_validation":
            used = sum(
                item.record_kind == "reservation" and item.registry_id == registry
                and item.role == "promotion_validation"
                and (item.topology_cluster_id == cohort.topology_cluster_id
                     or bool(set(item.ancestry_roots) & set(roots)))
                for item in existing
            )
            if used >= cohort.prospective_look_count:
                raise ValueError("promotion_validation_look_budget_exhausted")
        _append_split_use_record(output, record)
    return opener(cohort.source_case_ids)


def retire_final_audit_reservation(
    history_path: str | Path,
    *,
    reservation_id: str,
    report_id: str,
    reducer_name: str,
    reduced_result: Mapping[str, Any],
) -> SplitUseRecord:
    """Append a report binding for a previously consumed final-audit reservation."""

    with _split_use_lock(history_path) as output:
        records = validate_split_use_history(_load_split_use_history(output))
        reservation = next((item for item in records if item.record_kind == "reservation" and item.reservation_id == _text(reservation_id)), None)
        if reservation is None or reservation.role != "final_audit":
            raise ValueError("final_audit_reservation_not_found")
        if any(item.record_kind == "retirement" and item.reservation_id == reservation.reservation_id for item in records):
            raise ValueError("final_audit_reservation_already_retired")
        record = SplitUseRecord(
            record_kind="retirement", reservation_id=reservation.reservation_id, registry_id=reservation.registry_id,
            release_cohort_id=reservation.release_cohort_id, cohort_id=reservation.cohort_id, role=reservation.role,
            topology_cluster_id=reservation.topology_cluster_id, ancestry_roots=reservation.ancestry_roots,
            source_case_ids=reservation.source_case_ids, prospective_budget_id=reservation.prospective_budget_id,
            reducer_name=_text(reducer_name),
            result_sha256=content_hash({"reducer_name": _text(reducer_name), "result": dict(reduced_result)}),
            report_id=_text(report_id), multiplicity_alpha_allocation=reservation.multiplicity_alpha_allocation,
        )
        _append_split_use_record(output, record)
        return record


def _headline_request(value: Mapping[str, Any]) -> tuple[EvaluationSplitReport, str, str, str, str, tuple[Mapping[str, Any], ...]]:
    """Decode public metadata only; final-audit contents never enter this schema."""

    cohort_rows = value.get("cohorts")
    ancestry_rows = value.get("semantic_ancestry")
    history_rows = value.get("split_use_history")
    if not isinstance(cohort_rows, list) or not isinstance(ancestry_rows, list) or not isinstance(history_rows, list):
        raise ValueError("headline request requires cohorts, semantic_ancestry, and split_use_history lists")
    cohorts = tuple(
        EvaluationCohort(
            cohort_id=row.get("cohort_id", ""),
            role=row.get("role", ""),
            topology_cluster_id=row.get("topology_cluster_id", ""),
            source_case_ids=tuple(row.get("source_case_ids") or ()),
            semantic_source_ids=tuple(row.get("semantic_source_ids") or ()),
            prospective_budget_id=row.get("prospective_budget_id", ""),
            prospective_case_budget=row.get("prospective_case_budget", 0),
            prospective_look_count=row.get("prospective_look_count", 0),
            multiplicity_policy_id=row.get("multiplicity_policy_id", ""),
            multiplicity_alpha=row.get("multiplicity_alpha"),
        )
        for row in cohort_rows
        if isinstance(row, Mapping)
    )
    if len(cohorts) != len(cohort_rows):
        raise ValueError("headline cohort metadata must be objects")
    ancestry = tuple(
        SemanticAncestry(row.get("source_id", ""), row.get("parent_source_id", ""))
        for row in ancestry_rows
        if isinstance(row, Mapping)
    )
    if len(ancestry) != len(ancestry_rows):
        raise ValueError("headline ancestry metadata must be objects")
    if any(not isinstance(row, Mapping) for row in history_rows):
        raise ValueError("split-use history metadata must be objects")
    return (
        validate_evaluation_split(cohorts, ancestry),
        _text(value.get("split_registry_id")),
        _text(value.get("release_cohort_id")),
        _text(value.get("cohort_id")),
        _text(value.get("report_id")),
        tuple(history_rows),
    )


def headline_lift_decision(
    reduced_result: Mapping[str, Any],
    *,
    reducer_name: str,
    headline_request: Mapping[str, Any] | None = None,
    producer_case_ids: Sequence[str] | None = None,
    producer_origins: Sequence[Mapping[str, Any]] | None = None,
    history_source_bound: bool = False,
) -> HeadlineLiftDecision:
    """Allow an opt-in headline only when a final cohort has one exact receipt.

    The receipt binds the actual reducer output, so a caller-owned summary flag
    cannot promote development numbers.  Replaying the same receipt and same
    bytes stays valid; a second report for that cohort is refused as reuse.
    """

    reducer = _text(reducer_name)
    result_hash = content_hash({"reducer_name": reducer, "result": dict(reduced_result)}) if reducer else ""
    if headline_request is None:
        return HeadlineLiftDecision("development", False, ("headline_not_requested",), reducer, result_hash)
    try:
        split, registry_id, release_cohort_id, cohort_id, report_id, history_rows = _headline_request(headline_request)
    except (TypeError, ValueError, AttributeError) as exc:
        return HeadlineLiftDecision("headline_request_refused", False, (f"invalid_headline_request:{type(exc).__name__}",), reducer, result_hash)
    failures = list(split.failures)
    cohort = next((item for item in split.cohorts if item.cohort_id == cohort_id), None)
    if cohort is None:
        failures.append("headline_cohort_not_found")
    elif cohort.role != "final_audit":
        failures.append("headline_requires_final_audit_cohort")
    if not report_id:
        failures.append("headline_report_id_missing")
    if not registry_id:
        failures.append("headline_split_registry_missing")
    if not release_cohort_id:
        failures.append("headline_release_cohort_missing")
    derived_case_ids = tuple(_text(item) for item in (producer_case_ids or ()) if _text(item))
    if not derived_case_ids or len(set(derived_case_ids)) != len(derived_case_ids):
        failures.append("headline_producer_case_ids_unbound")
    elif cohort is not None and tuple(derived_case_ids) != tuple(cohort.source_case_ids):
        failures.append("headline_producer_case_ids_mismatch")
    origins = tuple(producer_origins or ())
    if not origins:
        failures.append("headline_producer_origin_unbound")
    else:
        origin_case_ids: list[str] = []
        for origin in origins:
            if not isinstance(origin, Mapping):
                failures.append("headline_producer_origin_malformed")
                continue
            origin_case_id = _text(origin.get("source_case_id"))
            origin_case_ids.append(origin_case_id)
            if not all(_text(origin.get(key)) for key in ("split_registry_id", "cohort_id", "role", "source_case_id")):
                failures.append("headline_producer_origin_unbound")
            if _text(origin.get("split_registry_id")) != registry_id:
                failures.append("headline_producer_registry_mismatch")
            if _text(origin.get("cohort_id")) != cohort_id:
                failures.append("headline_producer_cohort_mismatch")
            if _text(origin.get("role")).casefold() != "final_audit":
                failures.append("headline_producer_role_not_final_audit")
        if not all(origin_case_ids) or set(origin_case_ids) != set(derived_case_ids):
            failures.append("headline_producer_origin_case_ids_mismatch")
    if history_source_bound is not True:
        failures.append("headline_receipt_history_not_source_bound")
    if headline_request.get("receipt_history_complete") is not True:
        failures.append("headline_receipt_history_unknown")
    receipt = None
    try:
        history = validate_split_use_history(history_rows)
    except (TypeError, ValueError, AttributeError) as exc:
        failures.append(f"split_use_history_invalid:{type(exc).__name__}")
        history = ()
    reservations = [
        item for item in history
        if item.record_kind == "reservation" and item.registry_id == registry_id
        and item.release_cohort_id == release_cohort_id and item.cohort_id == cohort_id
    ]
    if len(reservations) != 1:
        failures.append("final_audit_reservation_missing_or_reused")
    else:
        reservation = reservations[0]
        if reservation.role != "final_audit" or (
            cohort is not None and (
                reservation.topology_cluster_id != cohort.topology_cluster_id
                or tuple(reservation.source_case_ids) != tuple(cohort.source_case_ids)
            )
        ):
            failures.append("final_audit_reservation_metadata_mismatch")
        if cohort is not None:
            cohort_roots = {
                split.ancestry_roots[source]
                for source in cohort.semantic_source_ids
                if source in split.ancestry_roots
            }
            family_reservations = [
                item for item in history
                if item.record_kind == "reservation" and item.registry_id == registry_id
                and item.role == "final_audit"
                and (item.topology_cluster_id == cohort.topology_cluster_id
                     or bool(set(item.ancestry_roots) & cohort_roots))
            ]
            if len(family_reservations) != 1:
                failures.append("final_audit_reservation_missing_or_reused")
        retirements = [item for item in history if item.record_kind == "retirement" and item.reservation_id == reservation.reservation_id]
        if len(retirements) != 1:
            failures.append("final_audit_retirement_missing_or_reused")
        else:
            retirement = retirements[0]
            receipt = FinalAuditReceipt(
                cohort_id=retirement.cohort_id, report_id=retirement.report_id,
                source_case_ids=retirement.source_case_ids, reducer_name=retirement.reducer_name,
                result_sha256=retirement.result_sha256,
            )
            if receipt.report_id != report_id:
                failures.append("final_audit_retirement_report_id_mismatch")
            if receipt.reducer_name != reducer:
                failures.append("final_audit_retirement_reducer_mismatch")
            if receipt.result_sha256 != result_hash:
                failures.append("final_audit_retirement_result_mismatch")
    return HeadlineLiftDecision(
        measurement_scope="final_audit" if not failures else "headline_request_refused",
        headline_lift_allowed=not failures,
        reason_codes=tuple(dict.fromkeys(failures or ["final_audit_receipt_bound"])),
        reducer_name=reducer,
        reduced_result_sha256=result_hash,
        final_audit_receipt=receipt if not failures else None,
    )


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
