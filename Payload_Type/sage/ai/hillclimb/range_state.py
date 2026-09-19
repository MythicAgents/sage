"""Read evaluator-owned, independently observed milestone ground truth.

The engagement ledger is only a replay store here. Sage hop status, effects, and shipping verifier verdicts are
never proof authority. Missing or legacy observations remain explicitly unmeasured/invalid.
"""
from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field
from enum import IntEnum
from time import monotonic
from typing import Callable, Literal

try:  # package import
    from ..langgraph import engagement_ledger
    from . import effect_registry
except Exception:  # script / sys.path import (mirrors the repo's langgraph pattern)
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import engagement_ledger  # type: ignore
    import effect_registry  # type: ignore


class Milestone(IntEnum):
    """The GOAD attack chain, in order. `furthest_milestone` is the max reached."""
    FOOTHOLD = 0
    GRAPH_COLLECTED = 1
    SYSTEM_ON_HOST = 2
    DA_CHILD = 3
    KRBTGT_DUMPED = 4
    CROSS_DOMAIN_CONTEXT = 5
    REPLICATION_RIGHTS_PARENT = 6
    DCSYNC_PARENT = 7
    CERT_FORGED = 8
    OBJECTIVE = 9


@dataclass
class MilestoneSpec:
    """Legacy scenario vocabulary used for objective routing, never proof admission.

    effect_prefixes: an effect proves this milestone if its normalized form starts
        with any of these (e.g. "krbtgt-hash:").
    domain_role: if set, the effect's domain tail must equal scenario.domains[role]
        (binds "child" vs "parent" vs "objective"); None means any domain counts.
    """
    effect_prefixes: tuple[str, ...]
    domain_role: str | None = None


# Default scenario vocabulary. The independent registry, not these prefixes, decides proof admission.
DEFAULT_SPEC: dict[Milestone, MilestoneSpec] = {
    Milestone.GRAPH_COLLECTED: MilestoneSpec(("graph-built:",)),
    Milestone.SYSTEM_ON_HOST: MilestoneSpec(("system:",)),
    Milestone.DA_CHILD: MilestoneSpec(("da:", "ea:"), domain_role="child"),
    Milestone.KRBTGT_DUMPED: MilestoneSpec(("krbtgt-hash:",), domain_role="child"),
    Milestone.CROSS_DOMAIN_CONTEXT: MilestoneSpec(("da:", "ea:", "kerberos-context:"), domain_role="parent"),
    Milestone.REPLICATION_RIGHTS_PARENT: MilestoneSpec(("ds-replication-rights:", "krbtgt-hash:"), domain_role="parent"),
    Milestone.DCSYNC_PARENT: MilestoneSpec(("krbtgt-hash:", "creds:"), domain_role="parent"),
    # CERT_FORGED + OBJECTIVE are scenario-supplied (effect tokens unconfirmed in code).
}


PROBEABLE_MILESTONES: frozenset[Milestone] = frozenset({
    Milestone.GRAPH_COLLECTED,
    Milestone.SYSTEM_ON_HOST,
    Milestone.KRBTGT_DUMPED,
    Milestone.DA_CHILD,
    Milestone.OBJECTIVE,
})


@dataclass
class Scenario:
    """A fixed evaluation scenario: which engagement to read and how to bind domains."""
    name: str
    engagement_id: str
    objective: str = ""          # the prompt given to BOTH Sage and the bare model (fair comparison)
    # role -> domain, e.g. {"child": "north.sevenkingdoms.local",
    #                       "parent": "sevenkingdoms.local",
    #                       "objective": "essos.local"}
    domains: dict[str, str] = field(default_factory=dict)
    # Per-milestone spec overrides (supply CERT_FORGED / OBJECTIVE here).
    spec_overrides: dict[Milestone, MilestoneSpec] = field(default_factory=dict)
    # Which milestones this scenario can prove; default = all keys in the merged spec + FOOTHOLD.
    milestone_subset: tuple[Milestone, ...] | None = None
    # Independent cross-checks (ISC-3): milestone -> callable returning real-world truth.
    # e.g. {Milestone.GRAPH_COLLECTED: lambda: bloodhound_domain_info_nonempty()}
    direct_probes: dict[Milestone, Callable[[], bool]] = field(default_factory=dict)
    # Probe-able milestones deliberately scored from ledger self-report only.
    self_report_exempt: frozenset[Milestone] = field(default_factory=frozenset)
    # Probe-able milestones whose live referee result is captured once at run time and replayed from the
    # ledger later. This covers probes that need run-context inputs (referee baselines, Mythic loot readers)
    # and therefore cannot be construction-time zero-arg `direct_probes`, while keeping offline re-score
    # grounded in observed range state instead of Sage self-report.
    recorded_probe_milestones: frozenset[Milestone] = field(default_factory=frozenset)

    def spec(self) -> dict[Milestone, MilestoneSpec]:
        merged = dict(DEFAULT_SPEC)
        merged.update(self.spec_overrides)
        return merged


@dataclass
class VerifierCost:
    """Non-secret accounting for one independently observed effect.

    A verifier adapter is allowed to report a read count only when it knows that
    count.  Invocation count is deliberately kept separate: a callable is not
    assumed to make one control-plane read merely because it was invoked.
    """
    effect: str = ""
    oracle_id: str = ""
    raw_probe_invocations: int | None = 0
    control_plane_reads: int | None = None
    control_plane_reads_reason: str = "raw callable did not declare control-plane reads"
    wall_seconds: float | None = 0.0
    declared_wall_seconds: float | None = None
    declared_control_plane_reads: int | None = None
    limit_disposition: Literal["not_declared", "within", "exceeded", "unavailable"] = "not_declared"
    monetary_cost_usd: float | None = None
    monetary_cost_status: Literal["unavailable", "measured", "not_applicable", "legacy_unknown"] = "unavailable"
    price_provenance: dict | None = None
    not_applicable_reason: str = ""
    completeness: Literal["complete", "incomplete", "legacy_unknown"] = "incomplete"
    completeness_reason: str = "control-plane read count unavailable"
    failed_invocations: int | None = 0
    timeout_invocations: int | None = 0

    def to_record(self) -> dict:
        return {
            "effect": self.effect,
            "oracle_id": self.oracle_id,
            "raw_probe_invocations": self.raw_probe_invocations,
            "control_plane_reads": self.control_plane_reads,
            "control_plane_reads_reason": self.control_plane_reads_reason,
            "wall_seconds": self.wall_seconds,
            "declared_wall_seconds": self.declared_wall_seconds,
            "declared_control_plane_reads": self.declared_control_plane_reads,
            "limit_disposition": self.limit_disposition,
            "monetary_cost_usd": self.monetary_cost_usd,
            "monetary_cost_status": self.monetary_cost_status,
            "price_provenance": self.price_provenance,
            "not_applicable_reason": self.not_applicable_reason,
            "completeness": self.completeness,
            "completeness_reason": self.completeness_reason,
            "failed_invocations": self.failed_invocations,
            "timeout_invocations": self.timeout_invocations,
        }

    @classmethod
    def legacy_unknown(cls, effect: str = "") -> "VerifierCost":
        return cls(
            effect=effect,
            raw_probe_invocations=None,
            wall_seconds=None,
            failed_invocations=None,
            timeout_invocations=None,
            monetary_cost_status="legacy_unknown",
            completeness="legacy_unknown",
            completeness_reason="historical verifier cost was not recorded",
            control_plane_reads_reason="historical verifier reads were not recorded",
        )

    @classmethod
    def from_record(cls, raw: object, *, effect: str = "") -> "VerifierCost":
        if not isinstance(raw, dict):
            return cls.legacy_unknown(effect)
        # Do not turn partial or old records into measured zero cost.
        status = raw.get("monetary_cost_status")
        completeness = raw.get("completeness")
        if status not in {"unavailable", "measured", "not_applicable", "legacy_unknown"}:
            return cls.legacy_unknown(effect)
        if completeness not in {"complete", "incomplete", "legacy_unknown"}:
            return cls.legacy_unknown(effect)
        for name in ("raw_probe_invocations", "control_plane_reads", "declared_control_plane_reads",
                     "failed_invocations", "timeout_invocations", "wall_seconds", "declared_wall_seconds"):
            value = raw.get(name)
            allowed = {int, float} if "seconds" in name else {int}
            if value is not None and (type(value) not in allowed or not math.isfinite(value) or value < 0):
                return cls.legacy_unknown(effect)
        try:
            from .experiment_contracts import CostMeasurement
        except ImportError:
            from experiment_contracts import CostMeasurement
        # Apply the same price qualification as campaign accounting. A bare
        # numeric price in an old verifier record cannot become measured USD.
        try:
            money = CostMeasurement(
                monetary_cost_usd=raw.get("monetary_cost_usd"), monetary_cost_status=status,
                price_provenance=raw.get("price_provenance"),
                not_applicable_reason=str(raw.get("not_applicable_reason") or ""))
        except (TypeError, ValueError):
            money = CostMeasurement()
        if any(raw.get(name) is None for name in (
            "raw_probe_invocations", "wall_seconds", "control_plane_reads", "failed_invocations", "timeout_invocations"
        )) and completeness == "complete":
            completeness = "incomplete"
        return cls(
            effect=str(raw.get("effect") or effect),
            oracle_id=str(raw.get("oracle_id") or ""),
            raw_probe_invocations=(int(raw["raw_probe_invocations"])
                                   if type(raw.get("raw_probe_invocations")) is int else None),
            control_plane_reads=(int(raw["control_plane_reads"])
                                 if isinstance(raw.get("control_plane_reads"), int) else None),
            control_plane_reads_reason=str(raw.get("control_plane_reads_reason") or
                                              "control-plane read count unavailable"),
            wall_seconds=(float(raw["wall_seconds"])
                          if type(raw.get("wall_seconds")) in {int, float} else None),
            declared_wall_seconds=(float(raw["declared_wall_seconds"])
                                   if isinstance(raw.get("declared_wall_seconds"), (int, float)) else None),
            declared_control_plane_reads=(int(raw["declared_control_plane_reads"])
                                          if isinstance(raw.get("declared_control_plane_reads"), int) else None),
            limit_disposition=(raw.get("limit_disposition")
                               if raw.get("limit_disposition") in {"not_declared", "within", "exceeded", "unavailable"}
                               else "not_declared"),
            monetary_cost_usd=money.monetary_cost_usd,
            monetary_cost_status=money.monetary_cost_status,
            price_provenance=raw.get("price_provenance") if isinstance(raw.get("price_provenance"), dict) else None,
            not_applicable_reason=money.not_applicable_reason,
            completeness=completeness,
            completeness_reason=str(raw.get("completeness_reason") or "cost completeness not recorded"),
            failed_invocations=(int(raw["failed_invocations"])
                                if type(raw.get("failed_invocations")) is int else None),
            timeout_invocations=(int(raw["timeout_invocations"])
                                 if type(raw.get("timeout_invocations")) is int else None),
        )


@dataclass
class GroundTruth:
    scenario: str
    milestones: dict[Milestone, bool]
    furthest: Milestone
    # Milestones where an independent direct probe DISAGREED with the ledger.
    # A non-empty list is a gauge-validity alarm ("who verifies the verifier?").
    probe_disagreements: list[Milestone] = field(default_factory=list)
    proof_results: dict[Milestone, effect_registry.ProofResult] = field(default_factory=dict)
    verifier_costs: dict[Milestone, VerifierCost] = field(default_factory=dict)


def local_start_state_verifier_cost(milestone: Milestone, *, oracle_id: str) -> VerifierCost:
    """Account only the local adapter for caller-supplied starting-state evidence.

    The callback/acquisition that produced this evidence happened before this
    reader.  Its historical resource use is not inferred as zero here.
    """
    return VerifierCost(
        effect=milestone.name,
        oracle_id=oracle_id,
        raw_probe_invocations=0,
        control_plane_reads=0,
        control_plane_reads_reason="caller-supplied start-state evidence; local adapter performed zero reads",
        wall_seconds=0.0,
        monetary_cost_usd=0.0,
        monetary_cost_status="not_applicable",
        not_applicable_reason="starting callback acquisition occurred outside this local verifier",
        completeness="complete",
        completeness_reason="local start-state adapter has no reads or resource use",
        failed_invocations=0,
        timeout_invocations=0,
    )


def _target_for(scenario: Scenario, milestone: Milestone) -> str:
    contract = effect_registry.contract_for(milestone)
    return str(scenario.domains.get(contract.target_role, "")) if contract.target_role else ""


def _recorded_result(data: dict, scenario: Scenario, milestone: Milestone) -> effect_registry.ProofResult:
    raw = data.get("ground_truth_probes")
    if not isinstance(raw, dict) or milestone.name not in raw:
        return effect_registry.unavailable_result(milestone, "no recorded independent observation")
    if raw.get("_source") != "independent_evaluator":
        return effect_registry.invalid_result(milestone, "recorded proof source is not independent")
    if raw.get("_registry_version") != effect_registry.REGISTRY_VERSION:
        return effect_registry.invalid_result(milestone, "recorded registry version mismatch")
    if not str(raw.get("_captured_at") or "").strip():
        return effect_registry.invalid_result(milestone, "recorded capture identity is missing")
    return effect_registry.admit_record(
        milestone,
        raw[milestone.name],
        expected_target=_target_for(scenario, milestone),
    )


def _recorded_verifier_cost(data: dict, milestone: Milestone) -> VerifierCost:
    raw = data.get("ground_truth_probes")
    if not isinstance(raw, dict):
        return VerifierCost.legacy_unknown(milestone.name)
    costs = raw.get("_verifier_costs")
    if not isinstance(costs, dict):
        return VerifierCost.legacy_unknown(milestone.name)
    return VerifierCost.from_record(costs.get(milestone.name), effect=milestone.name)


def _probe_result(scenario: Scenario, milestone: Milestone, probe: Callable[[], object]) -> effect_registry.ProofResult:
    try:
        observation = probe()
    except Exception as exc:
        return effect_registry.unavailable_result(milestone, f"independent probe unavailable: {type(exc).__name__}")
    return effect_registry.admit_observation(
        milestone,
        observation,
        expected_target=_target_for(scenario, milestone),
    )


def _live_probe_cost(milestone: Milestone, probe: Callable[[], object], started: float) -> VerifierCost:
    """Read tracked costs when available, otherwise retain only outer wall time.

    Scenario-local legacy callables predate the typed wrapper.  Their callable
    count does not establish a control-plane read count, so the fallback remains
    explicitly incomplete.
    """
    try:
        from .probes import verifier_cost_for
    except ImportError:  # script import path
        from probes import verifier_cost_for  # type: ignore
    tracked = verifier_cost_for(probe, effect=milestone.name)
    if tracked.completeness != "legacy_unknown":
        return tracked
    return VerifierCost(
        effect=milestone.name,
        raw_probe_invocations=1,
        wall_seconds=monotonic() - started,
        completeness="incomplete",
        completeness_reason="legacy callable has only outer wall timing",
        control_plane_reads_reason="legacy callable did not declare control-plane read count",
    )


# --- the reader ---------------------------------------------------------------------------------------

def read_ground_truth(
    scenario: Scenario,
    *,
    engagement_id: str | None = None,
    foothold_seen: bool | None = None,
    require_run_provenance: bool = False,
    run_live_probes: bool = False,
) -> GroundTruth:
    """Read independently observed milestones from typed live or recorded evidence. READ-ONLY.

    `engagement_id` overrides `scenario.engagement_id` — the live runner pins a FRESH per-run id via
    SAGE_ENGAGEMENT_ID and passes the same id here, so C1 reads that run's clean ledger rather than a
    stale static one (the per-reset-UUID staleness fix)."""
    eid = engagement_id if engagement_id is not None else scenario.engagement_id
    data = engagement_ledger.load(eid)  # never raises; {} skeleton if missing
    spec = scenario.spec()
    candidates = scenario.milestone_subset or tuple(
        m for m in Milestone if m == Milestone.FOOTHOLD or m in spec
    )

    milestones: dict[Milestone, bool] = {}
    results: dict[Milestone, effect_registry.ProofResult] = {}
    verifier_costs: dict[Milestone, VerifierCost] = {}
    for m in candidates:
        if m == Milestone.FOOTHOLD:
            if foothold_seen is None:
                result = effect_registry.unavailable_result(m, "starting callback observation unavailable")
            else:
                contract = effect_registry.contract_for(m)
                result = effect_registry.admit_observation(
                    m,
                    effect_registry.OracleObservation(
                        registry_version=effect_registry.REGISTRY_VERSION,
                        oracle_id=contract.oracle_id,
                        origin=effect_registry.ProofOrigin.MYTHIC_ARTIFACT,
                        proof_kind="starting_callback",
                        outcome=foothold_seen,
                        observation_id=f"starting:{scenario.name}",
                        target=_target_for(scenario, m),
                    ),
                    expected_target=_target_for(scenario, m),
                )
                verifier_costs[m] = local_start_state_verifier_cost(
                    m, oracle_id=contract.oracle_id,
                )
        elif run_live_probes and m in scenario.direct_probes:
            started = monotonic()
            result = _probe_result(scenario, m, scenario.direct_probes[m])
            verifier_costs[m] = _live_probe_cost(m, scenario.direct_probes[m], started)
        else:
            result = _recorded_result(data, scenario, m)
            if m in PROBEABLE_MILESTONES:
                verifier_costs[m] = _recorded_verifier_cost(data, m)
        results[m] = result
        milestones[m] = result.proven

    furthest = Milestone.FOOTHOLD
    for m in sorted(milestones):
        if milestones[m]:
            furthest = m

    disagreements = [
        milestone for milestone, result in results.items()
        if result.disposition in {effect_registry.ProofDisposition.UNMEASURED,
                                  effect_registry.ProofDisposition.INVALID}
    ]

    return GroundTruth(
        scenario=scenario.name,
        milestones=milestones,
        furthest=furthest,
        probe_disagreements=disagreements,
        proof_results=results,
        verifier_costs=verifier_costs,
    )


# --- no-mutation guard (ISC-4) ------------------------------------------------------------------------

def ledger_state_hash(engagement_id: str | None = None) -> str:
    """SHA-256 over the on-disk ledger bytes for an engagement (or the whole state dir).

    Tests assert this is byte-identical before/after a C1 call: a reader that mutates
    the state it measures is a corrupted gauge.
    """
    h = hashlib.sha256()
    if engagement_id is not None:
        path = engagement_ledger.ledger_path(engagement_id)
        paths = [path] if os.path.exists(path) else []
    else:
        state_dir = engagement_ledger.state_dir()
        paths = []
        if os.path.isdir(state_dir):
            for name in sorted(os.listdir(state_dir)):
                p = os.path.join(state_dir, name)
                if os.path.isfile(p):
                    paths.append(p)
    for p in paths:
        h.update(p.encode("utf-8"))
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()
