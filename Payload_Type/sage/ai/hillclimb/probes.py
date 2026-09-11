"""Typed direct-probe adapters for the independent eval-gauge verifier."""
from __future__ import annotations

from typing import Callable
import uuid

try:  # package import
    from . import effect_registry
    from .range_state import Milestone, GroundTruth
except Exception:  # script / sys.path import
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import effect_registry  # type: ignore
    from range_state import Milestone, GroundTruth  # type: ignore

DirectProbe = Callable[[], bool]
TypedProbe = Callable[[], effect_registry.OracleObservation]


class ProbeUnavailable(RuntimeError):
    """The declared oracle could not produce an observation."""


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

    def read() -> effect_registry.OracleObservation:
        value = probe()
        return effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION,
            oracle_id=contract.oracle_id,
            origin=origin,
            proof_kind=proof_kind,
            outcome=value,
            observation_id=uuid.uuid4().hex,
            target=target,
        )

    return read


def unavailable_probe(reason: str) -> TypedProbe:
    def read() -> effect_registry.OracleObservation:
        raise ProbeUnavailable(reason)

    return read


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
    try:
        baseline = probe()
    except Exception as exc:
        baseline_error = exc
        baseline = None
    else:
        baseline_error = None

    def read() -> bool:
        if baseline_error is not None or type(baseline) is not bool:
            raise ProbeUnavailable(f"{reason}; pre-arm baseline unavailable")
        current = probe()
        if type(current) is not bool:
            raise ProbeUnavailable(f"{reason}; observation was not Boolean")
        if current and not baseline:
            return True
        raise ProbeUnavailable(f"{reason}; no fresh positive transition")

    return read


def any_registered_probe(*candidates: TypedProbe) -> TypedProbe:
    """Preserve the first independently successful path and its exact origin."""
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

    return read


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

    def read() -> effect_registry.OracleObservation:
        return effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION,
            oracle_id=contract.oracle_id,
            origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT,
            proof_kind="domain_admin_differential",
            outcome=admin_probe(),
            observation_id=uuid.uuid4().hex,
            target=target,
            subject=admin_subject,
            envelope_id=envelope_id,
            negative_control_denied=non_admin_denied_probe(),
            control_target=target,
            control_subject=non_admin_subject,
            control_envelope_id=envelope_id,
        )

    return read


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
