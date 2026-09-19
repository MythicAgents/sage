"""Typed direct-probe adapters for the independent eval-gauge verifier."""
from __future__ import annotations

from time import monotonic
from typing import Callable
import uuid

try:  # package import
    from . import effect_registry
    from .range_state import Milestone, GroundTruth, VerifierCost, local_start_state_verifier_cost
except Exception:  # script / sys.path import
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import effect_registry  # type: ignore
    from range_state import Milestone, GroundTruth, VerifierCost, local_start_state_verifier_cost  # type: ignore

DirectProbe = Callable[[], bool]
TypedProbe = Callable[[], effect_registry.OracleObservation]


class ProbeUnavailable(RuntimeError):
    """The declared oracle could not produce an observation."""


class _CostTracker:
    """Mutable accounting attached to a probe closure, never to an observation."""
    def __init__(self, *, children: tuple["_CostTracker", ...] = ()):
        self.children = children
        self.raw_probe_invocations = 0
        self.wall_seconds = 0.0
        self.failed_invocations = 0
        self.timeout_invocations = 0
        self.control_plane_reads_per_invocation: int | None = None
        self.declared_wall_seconds: float | None = None
        self.declared_control_plane_reads: int | None = None
        self.budget_tracker: _CostTracker | None = None
        self.effect = ""
        self.oracle_id = ""

    def configure(
        self,
        *,
        declared_wall_seconds: float | None = None,
        declared_control_plane_reads: int | None = None,
        control_plane_reads_per_invocation: int | None = None,
    ) -> None:
        if declared_wall_seconds is not None:
            self.declared_wall_seconds = declared_wall_seconds
        if declared_control_plane_reads is not None:
            self.declared_control_plane_reads = declared_control_plane_reads
        if control_plane_reads_per_invocation is not None:
            if control_plane_reads_per_invocation < 0:
                raise ValueError("control_plane_reads_per_invocation must be non-negative")
            self.control_plane_reads_per_invocation = control_plane_reads_per_invocation

    def snapshot(self, *, effect: str = "") -> VerifierCost:
        children = [child.snapshot() for child in self.children]
        raw_calls = self.raw_probe_invocations + sum(child.raw_probe_invocations for child in children)
        wall = self.wall_seconds + sum(child.wall_seconds for child in children)
        failures = self.failed_invocations + sum(child.failed_invocations for child in children)
        timeouts = self.timeout_invocations + sum(child.timeout_invocations for child in children)
        own_reads = (self.raw_probe_invocations * self.control_plane_reads_per_invocation
                     if self.control_plane_reads_per_invocation is not None
                     else (0 if self.raw_probe_invocations == 0 else None))
        all_reads = [own_reads, *(child.control_plane_reads for child in children)]
        reads_known = all(value is not None for value in all_reads)
        reads = sum(int(value) for value in all_reads) if reads_known else None
        declared_wall = self.declared_wall_seconds
        declared_reads = self.declared_control_plane_reads
        exceeds = ((declared_wall is not None and wall > declared_wall)
                   or (declared_reads is not None and reads is not None and reads > declared_reads))
        if declared_wall is None and declared_reads is None:
            disposition = "not_declared"
        elif exceeds:
            disposition = "exceeded"
        elif declared_reads is not None and reads is None:
            disposition = "unavailable"
        else:
            disposition = "within"
        if reads_known:
            completeness = "complete"
            reason = ""
            read_reason = ""
        else:
            completeness = "incomplete"
            reason = "one or more raw callables did not declare control-plane read counts"
            read_reason = reason
        child_oracles = {child.oracle_id for child in children if child.oracle_id}
        oracle_id = self.oracle_id or (child_oracles.pop() if len(child_oracles) == 1 else "")
        return VerifierCost(
            effect=effect or self.effect,
            oracle_id=oracle_id,
            raw_probe_invocations=raw_calls,
            control_plane_reads=reads,
            control_plane_reads_reason=read_reason,
            wall_seconds=wall,
            declared_wall_seconds=declared_wall,
            declared_control_plane_reads=declared_reads,
            limit_disposition=disposition,
            monetary_cost_usd=None,
            monetary_cost_status="unavailable",
            price_provenance=None,
            completeness=completeness,
            completeness_reason=reason,
            failed_invocations=failures,
            timeout_invocations=timeouts,
        )


def _attach_tracker(probe: Callable, tracker: _CostTracker) -> Callable:
    setattr(probe, "_verifier_cost_tracker", tracker)
    return probe


def _tracker_for(probe: Callable) -> _CostTracker | None:
    tracker = getattr(probe, "_verifier_cost_tracker", None)
    return tracker if isinstance(tracker, _CostTracker) else None


def _tracked_raw_probe(probe: DirectProbe, tracker: _CostTracker) -> DirectProbe:
    def read() -> bool:
        budget = tracker.budget_tracker or tracker
        if budget.declared_wall_seconds is not None and budget.snapshot().wall_seconds >= budget.declared_wall_seconds:
            raise ProbeUnavailable("declared verifier wall budget exhausted before raw call")
        started = monotonic()
        try:
            return probe()
        except Exception as exc:
            tracker.failed_invocations += 1
            if isinstance(exc, TimeoutError):
                tracker.timeout_invocations += 1
            raise
        finally:
            tracker.raw_probe_invocations += 1
            tracker.wall_seconds += monotonic() - started
    return read


def verifier_cost_for(probe: Callable, *, effect: str = "") -> VerifierCost:
    """Return an aggregate without invoking the probe or inferring missing reads."""
    tracker = _tracker_for(probe)
    return tracker.snapshot(effect=effect) if tracker else VerifierCost.legacy_unknown(effect)


def fresh_record_probe(milestone, read_records: Callable, parse_records: Callable) -> TypedProbe:
    """Capture a task-ID baseline and parse fresh raw records through a named oracle.

    The independent parser owns exact operation/callback/target checks. A failed
    baseline cannot be repaired by silently taking a later baseline after a run.
    """
    tracker = _CostTracker()
    tracker.effect = milestone.name
    tracker.oracle_id = effect_registry.contract_for(milestone).oracle_id
    tracker.configure(declared_wall_seconds=120.0)
    source = _tracked_raw_probe(read_records, tracker)
    try:
        baseline = source()
        if not isinstance(baseline, list) or any(
            not isinstance(row, dict) or type(row.get("display_id")) is not int
            or row["display_id"] <= 0 for row in baseline
        ):
            raise ProbeUnavailable("task baseline is malformed")
        baseline_ids = {row["display_id"] for row in baseline}
        if len(baseline_ids) != len(baseline):
            raise ProbeUnavailable("task baseline has duplicate identities")
    except Exception:
        baseline_ids = None

    def read():
        if baseline_ids is None:
            raise ProbeUnavailable("pre-arm task baseline is unavailable")
        rows = source()
        started = monotonic()
        try:
            result = parse_records(rows, baseline_ids)
        finally:
            tracker.wall_seconds += monotonic() - started
        if result is None:
            raise ProbeUnavailable("no fresh independently proven task sequence")
        return result

    return _attach_tracker(read, tracker)


def bind_verifier_cost(
    probe: Callable,
    *,
    declared_wall_seconds: float | None = None,
    declared_control_plane_reads: int | None = None,
    control_plane_reads_per_invocation: int | None = None,
) -> Callable:
    """Bind assembly-known bounds/read measurement to an existing tracked probe.

    This is intentionally explicit: absent adapter knowledge stays unavailable,
    rather than treating callable count as a control-plane read count.
    """
    tracker = _tracker_for(probe)
    if tracker is None:
        tracker = _CostTracker()
        _attach_tracker(probe, tracker)
    tracker.configure(
        declared_wall_seconds=declared_wall_seconds,
        declared_control_plane_reads=declared_control_plane_reads,
        control_plane_reads_per_invocation=control_plane_reads_per_invocation,
    )
    def bind_budget(node: _CostTracker) -> None:
        node.budget_tracker = tracker
        for child in node.children:
            bind_budget(child)
    bind_budget(tracker)
    return probe


def registered_probe(
    milestone: Milestone,
    probe: DirectProbe,
    *,
    origin: effect_registry.ProofOrigin,
    proof_kind: str,
    target: str = "",
) -> TypedProbe:
    """Bind a trusted evaluator adapter to its immutable registry identity."""
    contract = effect_registry.contract_for(milestone)

    tracker = _tracker_for(probe) or _CostTracker()
    tracker.effect = milestone.name
    tracker.oracle_id = contract.oracle_id
    raw = probe if _tracker_for(probe) else _tracked_raw_probe(probe, tracker)

    def read() -> effect_registry.OracleObservation:
        value = raw()
        return effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION,
            oracle_id=contract.oracle_id,
            origin=origin,
            proof_kind=proof_kind,
            outcome=value,
            observation_id=uuid.uuid4().hex,
            target=target,
        )

    return _attach_tracker(read, tracker)


def unavailable_probe(reason: str) -> TypedProbe:
    def read() -> effect_registry.OracleObservation:
        raise ProbeUnavailable(reason)

    return _attach_tracker(read, _CostTracker())


def positive_only(probe: DirectProbe, reason: str) -> DirectProbe:
    """Adapt a legacy fail-soft reader without misclassifying its False as measured evidence."""
    def read() -> bool:
        value = probe()
        if value is True:
            return True
        raise ProbeUnavailable(reason)

    return read


def fresh_positive(probe: DirectProbe, reason: str) -> DirectProbe:
    """Require a False pre-arm baseline and a later True observation."""
    tracker = _tracker_for(probe) or _CostTracker()
    tracker.raw_probe_invocations += int(getattr(probe, "_verifier_factory_invocations", 0) or 0)
    tracker.wall_seconds += float(getattr(probe, "_verifier_factory_wall_seconds", 0.0) or 0.0)
    raw = probe if _tracker_for(probe) else _tracked_raw_probe(probe, tracker)
    try:
        baseline = raw()
    except Exception as exc:
        baseline_error = exc
        baseline = None
    else:
        baseline_error = None

    def read() -> bool:
        if baseline_error is not None or type(baseline) is not bool:
            raise ProbeUnavailable(f"{reason}; pre-arm baseline unavailable")
        current = raw()
        if type(current) is not bool:
            raise ProbeUnavailable(f"{reason}; observation was not Boolean")
        if current and not baseline:
            return True
        raise ProbeUnavailable(f"{reason}; no fresh positive transition")

    return _attach_tracker(read, tracker)


def any_registered_probe(*candidates: TypedProbe) -> TypedProbe:
    """Preserve the first independently successful path and its exact origin."""
    tracker = _CostTracker(children=tuple(t for candidate in candidates if (t := _tracker_for(candidate))))

    def read() -> effect_registry.OracleObservation:
        first_negative = None
        errors: list[Exception] = []
        for candidate in candidates:
            try:
                observation = candidate()
            except Exception as exc:
                errors.append(exc)
                continue
            if not isinstance(observation, effect_registry.OracleObservation):
                return observation
            if type(observation.outcome) is bool and observation.outcome:
                return observation
            if first_negative is None:
                first_negative = observation
        if first_negative is not None:
            return first_negative
        if errors:
            raise errors[0]
        raise ProbeUnavailable("no independent proof path configured")

    return _attach_tracker(read, tracker)


def domain_admin_differential_probe(
    admin_probe: DirectProbe,
    non_admin_denied_probe: DirectProbe,
    *,
    target: str,
    envelope_id: str,
    admin_subject: str,
    non_admin_subject: str,
) -> TypedProbe:
    """Build the matched allow/deny observation required for Domain Administrator proof."""
    contract = effect_registry.contract_for(Milestone.DA_CHILD)

    tracker = _CostTracker()
    admin = _tracked_raw_probe(admin_probe, tracker)
    denied = _tracked_raw_probe(non_admin_denied_probe, tracker)

    def read() -> effect_registry.OracleObservation:
        return effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION,
            oracle_id=contract.oracle_id,
            origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT,
            proof_kind="domain_admin_differential",
            outcome=admin(),
            observation_id=uuid.uuid4().hex,
            target=target,
            subject=admin_subject,
            envelope_id=envelope_id,
            negative_control_denied=denied(),
            control_target=target,
            control_subject=non_admin_subject,
            control_envelope_id=envelope_id,
        )

    tracker.effect = Milestone.DA_CHILD.name
    tracker.oracle_id = contract.oracle_id
    return _attach_tracker(read, tracker)


def read_ground_truth_from_probes(
    scenario,
    probes: dict,
    *,
    foothold_seen: bool | None = None,
) -> GroundTruth:
    """Build ground truth from typed independent probes, never raw Boolean claims."""
    candidates = scenario.milestone_subset or tuple(m for m in Milestone)
    milestones: dict = {}
    results: dict = {}
    verifier_costs: dict = {}
    for m in candidates:
        if m == Milestone.FOOTHOLD and foothold_seen is not None:
            contract = effect_registry.contract_for(m)
            result = effect_registry.admit_observation(
                m,
                effect_registry.OracleObservation(
                    registry_version=effect_registry.REGISTRY_VERSION,
                    oracle_id=contract.oracle_id,
                    origin=effect_registry.ProofOrigin.MYTHIC_ARTIFACT,
                    proof_kind="starting_callback",
                    outcome=foothold_seen,
                    observation_id=f"starting:{getattr(scenario, 'name', 'scenario')}",
                    target=str(getattr(scenario, "domains", {}).get(contract.target_role, "")),
                ),
                expected_target=str(getattr(scenario, "domains", {}).get(contract.target_role, "")),
            )
            verifier_costs[m] = local_start_state_verifier_cost(
                m, oracle_id=contract.oracle_id,
            )
        else:
            probe = probes.get(m)
            if probe is None:
                result = effect_registry.unavailable_result(m, "no independent probe configured")
            else:
                try:
                    observation = probe()
                except Exception as exc:
                    result = effect_registry.unavailable_result(
                        m, f"independent probe unavailable: {type(exc).__name__}"
                    )
                else:
                    contract = effect_registry.contract_for(m)
                    expected = str(getattr(scenario, "domains", {}).get(contract.target_role, ""))
                    result = effect_registry.admit_observation(m, observation, expected_target=expected)
                verifier_costs[m] = verifier_cost_for(probe, effect=m.name)
        results[m] = result
        milestones[m] = result.proven

    furthest = Milestone.FOOTHOLD
    for m in sorted(milestones):
        if milestones[m]:
            furthest = m

    return GroundTruth(
        scenario=getattr(scenario, "name", "scn"),
        milestones=milestones,            # enum-keyed, like range_state.read_ground_truth
        furthest=furthest,
        probe_disagreements=[
            milestone for milestone, result in results.items()
            if result.disposition in {effect_registry.ProofDisposition.UNMEASURED,
                                      effect_registry.ProofDisposition.INVALID}
        ],
        proof_results=results,
        verifier_costs=verifier_costs,
    )


# --- probe factories (operator injects `cypher_run`) --------------------------------------------------

def cypher_nonempty_probe(cypher_run: Callable[[str], list], query: str) -> DirectProbe:
    """A probe that is True iff `cypher_run(query)` returns ≥1 row.

    `cypher_run` is the injected BloodHound MCP runner: it takes a single-column Cypher RETURN and
    yields the list of values (read from the MCP 'run' response's data.literals, per graph_reconciler)."""
    def probe() -> bool:
        return bool(cypher_run(query))
    return probe


def graph_collected_probe(cypher_run: Callable[[str], list]) -> DirectProbe:
    """GRAPH_COLLECTED ground truth: BloodHound has ≥1 Domain node (domain_info non-empty).
    This is the exact false-positive the collection ping-pong hit — a real, ledger-independent check."""
    return cypher_nonempty_probe(cypher_run, "MATCH (d:Domain) RETURN d.name")


def domain_admin_probe(cypher_run: Callable[[str], list], domain: str) -> DirectProbe:
    """DA ground truth for `domain`: ≥1 principal effectively in its Domain Admins (RID-512).
    Illustrative cypher — verify against your BloodHound CE schema."""
    safe = domain.replace("'", "")
    query = (
        "MATCH (g:Group)<-[:MemberOf*1..]-(p:Base) "
        f"WHERE toUpper(g.objectid) ENDS WITH '-512' AND toUpper(g.domain) = toUpper('{safe}') "
        "RETURN p.name"
    )
    return cypher_nonempty_probe(cypher_run, query)
