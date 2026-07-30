"""Offline census for same-capability target-value surfaces.

This module answers a narrow architecture question before any live benchmark
spend: does the current capability model already express target-specific
downstream value, or is there enough repeated evidence to justify a new generic
target-value abstraction?

The census is synthetic and eval-only. Every surface is evaluated through the
existing ``actions_from_state`` frontier plus ``modeled_branch_reachability``.
For equal-cost surfaces, the diagnosis is intentionally explicit and curated:
the report separates the mechanical modeled observation from the interpretation
of why the current facts/effects collapse.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable

try:  # package import
    from . import frontier_census
    from ..langgraph import capabilities, engagement_state, policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import policy  # type: ignore


NATURALLY_ASYMMETRIC = "naturally_asymmetric"
COLLAPSED_EQUAL_COST = "collapsed_equal_cost"
INDISTINGUISHABLE_FROM_CURRENT_FACTS = "indistinguishable_from_current_facts"
INVALID_SURFACE = "invalid_surface"
UNRESOLVED_EQUAL_COST = "unresolved_equal_cost"

TARGET_DOMAIN = "target.local"
AUXILIARY_DOMAIN = "decoy.local"
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_NODES = 120


class TargetValueCensusError(ValueError):
    """Raised when a declared census surface cannot be constructed or scored."""


@dataclass(frozen=True)
class SurfaceSpec:
    id: str
    capability_name: str
    family: str
    purpose: str
    expected_target_count: int
    state_factory: Callable[[], engagement_state.EngagementState]
    expected_classification: str
    equal_cost_interpretation: str
    interpretation_basis: str
    candidate_for_live_contract: bool = False
    model_note: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="target_value_census",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def _foothold(domain: str = TARGET_DOMAIN, *, callback_id: str = "1") -> engagement_state.Foothold:
    return engagement_state.Foothold(
        callback_id=callback_id,
        agent="apollo",
        host="ws01",
        forest=domain,
        identity=f"{domain.split('.', 1)[0].upper()}\\user1",
        integrity="medium",
        alive=True,
        source="target_value_census",
        timestamp=_now(),
    )


def _state(
    *,
    objective_domain: str = TARGET_DOMAIN,
    graph_facts: list[str] | None = None,
    hops: list[engagement_state.Hop] | None = None,
) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=f"From the current foothold, achieve administrative control of {objective_domain}.",
        footholds=[_foothold(objective_domain)],
        graph_facts=[_fact(predicate) for predicate in list(graph_facts or [])],
        hops=list(hops or []),
    )


def _blocked_ca_export_hop() -> engagement_state.Hop:
    effect = f"adcs-ca-private-key:ca01@{TARGET_DOMAIN}"
    return engagement_state.Hop(
        id="target-value-census-blocked-ca-export",
        technique="capability:adcs-ca-private-key-export",
        target=f"target=ca01;target_domain={TARGET_DOMAIN};callback=1",
        effect=effect,
        status="blocked",
        evidence={
            "source": "target_value_census",
            "verify_reason": "key not exportable",
            "key_not_exportable": True,
            "target_host": "ca01",
            "target_domain": TARGET_DOMAIN,
            "terminal_failure": True,
        },
        preconditions=[],
        satisfied_effects=[],
        source="target_value_census",
        timestamp=_now(),
    )


def _multi_domain_dcsync_state() -> engagement_state.EngagementState:
    return _state(graph_facts=[
        f"ds-replication-rights:{AUXILIARY_DOMAIN}",
        f"ds-replication-rights:{TARGET_DOMAIN}",
    ])


def _same_domain_gpo_dc_scope_state() -> engagement_state.EngagementState:
    return _state(graph_facts=[
        "generic-write:gpo:a-policy",
        f"gpo-domain:a-policy:{TARGET_DOMAIN}",
        f"gpo-affects-computer:a-policy:srv01:{TARGET_DOMAIN}",
        "generic-write:gpo:b-policy",
        f"gpo-domain:b-policy:{TARGET_DOMAIN}",
        f"gpo-affects-dc:b-policy:dc01:{TARGET_DOMAIN}",
    ])


def _adcs_enrollment_account_state() -> engagement_state.EngagementState:
    return _state(
        graph_facts=[
            f"certificate-auth-target:administrator@{TARGET_DOMAIN}",
            f"certificate-auth-target:helpdesk@{TARGET_DOMAIN}",
        ],
        hops=[_blocked_ca_export_hop()],
    )


def _same_domain_gpo_unscoped_state() -> engagement_state.EngagementState:
    return _state(graph_facts=[
        "generic-write:gpo:a-policy",
        f"gpo-domain:a-policy:{TARGET_DOMAIN}",
        "generic-write:gpo:b-policy",
        f"gpo-domain:b-policy:{TARGET_DOMAIN}",
    ])


def _managed_local_admin_host_state() -> engagement_state.EngagementState:
    return _state(graph_facts=[
        (
            "can-read-managed-local-admin-secret:"
            f"account=user1;account_domain={TARGET_DOMAIN};target=host-a;target_domain={TARGET_DOMAIN}"
        ),
        (
            "can-read-managed-local-admin-secret:"
            f"account=user1;account_domain={TARGET_DOMAIN};target=host-b;target_domain={TARGET_DOMAIN}"
        ),
    ])


SURFACES = (
    SurfaceSpec(
        id="multi-domain-dcsync-krbtgt",
        capability_name="dcsync-krbtgt",
        family="replication-kerberos",
        purpose="Measure whether objective-domain alignment already separates equal-cost DCSync targets.",
        expected_target_count=2,
        state_factory=_multi_domain_dcsync_state,
        expected_classification=NATURALLY_ASYMMETRIC,
        equal_cost_interpretation="",
        interpretation_basis=(
            "The objective-domain DCSync target reaches the objective without first consuming the "
            "auxiliary-domain branch."
        ),
        candidate_for_live_contract=True,
    ),
    SurfaceSpec(
        id="same-domain-gpo-dc-scope",
        capability_name="gpo-controlled-system-exec",
        family="gpo-directory",
        purpose="Measure whether existing GPO scope facts already separate equal-cost same-domain targets.",
        expected_target_count=2,
        state_factory=_same_domain_gpo_dc_scope_state,
        expected_classification=NATURALLY_ASYMMETRIC,
        equal_cost_interpretation="",
        interpretation_basis=(
            "The GPO with a declared DC scope emits an immediate objective-domain admin effect while the "
            "other GPO does not."
        ),
        candidate_for_live_contract=True,
    ),
    SurfaceSpec(
        id="adcs-enrollment-account-targets",
        capability_name="adcs-esc-certificate-enroll",
        family="adcs",
        purpose="Measure whether account-distinct ADCS enrollment targets retain downstream value differences.",
        expected_target_count=2,
        state_factory=_adcs_enrollment_account_state,
        expected_classification=COLLAPSED_EQUAL_COST,
        equal_cost_interpretation=COLLAPSED_EQUAL_COST,
        interpretation_basis=(
            "Account-distinct enrollment targets converge on the same modeled certificate-auth path and "
            "the same objective-domain admin proof."
        ),
        model_note=(
            "Current adcs-certificate-auth effects record da:<domain> for every enrolled account target."
        ),
    ),
    SurfaceSpec(
        id="same-domain-gpo-unscoped-targets",
        capability_name="gpo-controlled-system-exec",
        family="gpo-directory",
        purpose="Measure whether same-domain GPO targets without scope facts are distinguishable at all.",
        expected_target_count=2,
        state_factory=_same_domain_gpo_unscoped_state,
        expected_classification=INDISTINGUISHABLE_FROM_CURRENT_FACTS,
        equal_cost_interpretation=INDISTINGUISHABLE_FROM_CURRENT_FACTS,
        interpretation_basis=(
            "Both GPO targets have the same current generic-write and domain facts; no existing fact gives "
            "one a shorter or more objective-relevant route."
        ),
    ),
    SurfaceSpec(
        id="managed-local-admin-host-targets",
        capability_name="read-managed-local-admin-secret",
        family="managed-local-admin",
        purpose="Measure whether same-domain managed-local-admin host targets are distinguishable from current facts.",
        expected_target_count=2,
        state_factory=_managed_local_admin_host_state,
        expected_classification=INDISTINGUISHABLE_FROM_CURRENT_FACTS,
        equal_cost_interpretation=INDISTINGUISHABLE_FROM_CURRENT_FACTS,
        interpretation_basis=(
            "Both readable host targets have the same current account, domain, and managed-secret facts; "
            "no current fact marks one host as a better objective route."
        ),
        model_note=(
            "The current modeled continuation exposes the same remote-exec and ADCS path from either host; "
            "that is a narrower host-role semantics question, not evidence for a generic target-value field."
        ),
    ),
)


def _spec_payload(spec: SurfaceSpec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "capability_name": spec.capability_name,
        "family": spec.family,
        "purpose": spec.purpose,
        "expected_target_count": spec.expected_target_count,
        "expected_classification": spec.expected_classification,
        "equal_cost_interpretation": spec.equal_cost_interpretation,
        "interpretation_basis": spec.interpretation_basis,
        "candidate_for_live_contract": spec.candidate_for_live_contract,
        "model_note": spec.model_note,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _target_fields(value: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in str(value or "").split(";"):
        key, sep, raw = item.partition("=")
        if not sep:
            continue
        key = key.strip()
        raw = raw.strip()
        if key:
            fields[key] = raw
    return fields


def _distinct_target_fields(candidates: list[dict[str, Any]]) -> dict[str, list[str]]:
    values_by_key: dict[str, set[str]] = {}
    for candidate in candidates:
        for key, value in _target_fields(candidate.get("target")).items():
            values_by_key.setdefault(key, set()).add(value)
    return {
        key: sorted(values)
        for key, values in sorted(values_by_key.items())
        if len(values) > 1
    }


def _action_payload(action: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": str(getattr(action, "name", "") or ""),
        "target": str(getattr(action, "target", "") or ""),
        "family": policy.capability_family(getattr(action, "name", "")),
        "preconditions": list(getattr(action, "preconditions", None) or []),
        "effects": list(getattr(action, "effects", None) or []),
        "operational_cost": capabilities.operational_cost_for_action(action),
        "reason": str(getattr(action, "reason", "") or ""),
    }


def _same_capability_actions(
    state: engagement_state.EngagementState,
    capability_name: str,
) -> list[Any]:
    return [
        action
        for action in capabilities.actions_from_state(state)
        if str(getattr(action, "name", "") or "") == capability_name
    ]


def _modeled_reachability(
    state: engagement_state.EngagementState,
    action: Any,
) -> dict[str, Any]:
    return asyncio.run(
        frontier_census.modeled_branch_reachability(
            state,
            action,
            max_depth=DEFAULT_MAX_DEPTH,
            max_nodes=DEFAULT_MAX_NODES,
        )
    )


def _objective_effects(path: list[dict[str, Any]], objective: str) -> list[str]:
    targets = list(engagement_state._objective_target_domains(objective))
    objective_effects: set[str] = set()
    for step in path:
        for effect in list(step.get("effects") or []):
            prefix, sep, domain = str(effect).partition(":")
            if not sep or prefix not in {"da", "ea"}:
                continue
            if any(engagement_state._domains_equivalent(domain, target) for target in targets):
                objective_effects.add(str(effect))
    return sorted(objective_effects)


def _diagnose_surface(
    spec: SurfaceSpec,
    *,
    modeled_cost_relation: str,
    same_path_shape: bool,
    shared_objective_effects: list[str],
    base_checks_pass: bool,
) -> dict[str, Any]:
    if not base_checks_pass:
        return {
            "classification": INVALID_SURFACE,
            "supported": False,
            "reason": "surface did not satisfy the same-capability equal-visible-cost reachability contract",
            "interpretation_source": "mechanical_gate",
        }
    if modeled_cost_relation == "asymmetric":
        return {
            "classification": NATURALLY_ASYMMETRIC,
            "supported": True,
            "reason": spec.interpretation_basis,
            "interpretation_source": "modeled_transaction_cost_variance",
        }
    if modeled_cost_relation != "equal":
        return {
            "classification": UNRESOLVED_EQUAL_COST,
            "supported": False,
            "reason": "surface did not produce a complete comparable modeled cost set",
            "interpretation_source": "mechanical_gate",
        }
    supported = bool(
        spec.equal_cost_interpretation
        and same_path_shape
        and shared_objective_effects
    )
    return {
        "classification": spec.equal_cost_interpretation or UNRESOLVED_EQUAL_COST,
        "supported": supported,
        "reason": spec.interpretation_basis,
        "interpretation_source": "curated_equal_cost_interpretation",
    }


def _surface_report(spec: SurfaceSpec) -> dict[str, Any]:
    state = spec.state_factory()
    actions = _same_capability_actions(state, spec.capability_name)
    if not actions:
        raise TargetValueCensusError(
            f"{spec.id}: no {spec.capability_name!r} actions were generated from the declared state"
        )
    candidates = [_action_payload(action, index) for index, action in enumerate(actions)]
    reachability: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        branch = _modeled_reachability(state, action)
        path = list(branch.get("path") or [])
        reachability.append({
            "index": index,
            "target": candidates[index]["target"],
            "reachable": branch["reachable"],
            "modeled_transactions": branch["modeled_transactions"],
            "path": path,
            "path_names": [str(item.get("name") or "") for item in path],
            "objective_effects": _objective_effects(path, state.objective),
            "reason": branch["reason"],
        })
    reachable_costs = [
        int(item["modeled_transactions"])
        for item in reachability
        if item["reachable"] and item["modeled_transactions"] is not None
    ]
    cost_profiles = {
        _canonical_json(candidate["operational_cost"])
        for candidate in candidates
    }
    target_names = {candidate["target"] for candidate in candidates}
    capability_names = {candidate["name"] for candidate in candidates}
    path_shapes = {
        tuple(item["path_names"])
        for item in reachability
        if item["reachable"]
    }
    objective_effect_sets = [
        set(item["objective_effects"])
        for item in reachability
        if item["reachable"]
    ]
    shared_objective_effects = sorted(
        set.intersection(*objective_effect_sets)
        if objective_effect_sets
        else set()
    )
    modeled_cost_relation = (
        "asymmetric"
        if len(set(reachable_costs)) > 1
        else "equal"
        if reachable_costs
        else "unresolved"
    )
    base_checks = {
        "same_capability_multi_target_frontier": (
            len(candidates) == spec.expected_target_count
            and len(capability_names) == 1
            and spec.capability_name in capability_names
            and len(target_names) == len(candidates)
        ),
        "family_matches_contract": all(candidate["family"] == spec.family for candidate in candidates),
        "equal_visible_cost": len(cost_profiles) == 1,
        "all_targets_reach_objective": bool(reachability) and all(item["reachable"] for item in reachability),
    }
    diagnosis = _diagnose_surface(
        spec,
        modeled_cost_relation=modeled_cost_relation,
        same_path_shape=len(path_shapes) == 1,
        shared_objective_effects=shared_objective_effects,
        base_checks_pass=all(base_checks.values()),
    )
    checks = {
        **base_checks,
        "classification_matches_contract": diagnosis["classification"] == spec.expected_classification,
        "diagnosis_supported_by_modeled_evidence": diagnosis["supported"] is True,
    }
    best_cost = min(reachable_costs) if reachable_costs else None
    best_indices = [
        int(item["index"])
        for item in reachability
        if best_cost is not None and item["modeled_transactions"] == best_cost
    ]
    return {
        "id": spec.id,
        "spec": _spec_payload(spec),
        "objective": state.objective,
        "candidates": candidates,
        "distinct_target_fields": _distinct_target_fields(candidates),
        "reachability": reachability,
        "modeled_transaction_costs": reachable_costs,
        "modeled_cost_relation": modeled_cost_relation,
        "best_modeled_transaction_cost": best_cost,
        "best_indices": best_indices,
        "same_path_shape": len(path_shapes) == 1,
        "shared_objective_effects": shared_objective_effects,
        "diagnosis": diagnosis,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def run_target_value_census() -> dict[str, Any]:
    """Return a bounded offline census across current same-capability target surfaces."""

    surfaces = [_surface_report(spec) for spec in SURFACES]
    classifications: dict[str, list[str]] = {}
    for surface in surfaces:
        classification = str((surface.get("diagnosis") or {}).get("classification") or "")
        classifications.setdefault(classification, []).append(str(surface["id"]))
    families = sorted({str(surface["spec"]["family"]) for surface in surfaces})
    natural_surfaces = classifications.get(NATURALLY_ASYMMETRIC, [])
    collapsed_surfaces = classifications.get(COLLAPSED_EQUAL_COST, [])
    indistinguishable_surfaces = classifications.get(INDISTINGUISHABLE_FROM_CURRENT_FACTS, [])
    natural_families = sorted({
        str(surface["spec"]["family"])
        for surface in surfaces
        if surface["id"] in natural_surfaces
    })
    collapsed_families = sorted({
        str(surface["spec"]["family"])
        for surface in surfaces
        if surface["id"] in collapsed_surfaces
    })
    prefer_naturally_asymmetric_surface = bool(natural_surfaces)
    generic_target_value_abstraction_supported = bool(
        not prefer_naturally_asymmetric_surface
        and len(collapsed_families) >= 2
        and not indistinguishable_surfaces
    )
    if prefer_naturally_asymmetric_surface:
        recommended_next_move = "choose_naturally_asymmetric_live_surface_first"
        recommendation_reason = (
            "The current capability model already expresses target-specific downstream value in multiple "
            "families. Equal-cost cases are either narrow effect collapses or surfaces without current "
            "preference facts, so a generic runtime target-value field is not justified yet."
        )
    elif generic_target_value_abstraction_supported:
        recommended_next_move = "investigate_generic_target_value_abstraction"
        recommendation_reason = (
            "No naturally asymmetric surface remains, and multiple unrelated capability families collapse "
            "despite modeled evidence that should distinguish targets."
        )
    else:
        recommended_next_move = "collect_more_modeling_evidence"
        recommendation_reason = (
            "The census does not yet separate a reusable target-value gap from local effect/fact modeling gaps."
        )
    checks = {
        "surfaces_pass": bool(surfaces) and all(surface["passes_gate"] for surface in surfaces),
        "covers_required_capability_families": {
            "adcs",
            "gpo-directory",
            "managed-local-admin",
            "replication-kerberos",
        } <= set(families),
        "all_surfaces_are_same_capability_equal_visible_cost": all(
            surface["checks"]["same_capability_multi_target_frontier"]
            and surface["checks"]["equal_visible_cost"]
            for surface in surfaces
        ),
        "has_naturally_asymmetric_surface": bool(natural_surfaces),
        "has_collapsed_equal_cost_surface": bool(collapsed_surfaces),
        "has_indistinguishable_surface": bool(indistinguishable_surfaces),
        "recommendation_is_conservative": (
            prefer_naturally_asymmetric_surface
            and generic_target_value_abstraction_supported is False
            and recommended_next_move == "choose_naturally_asymmetric_live_surface_first"
        ),
    }
    return {
        "kind": "target_value_census",
        "generated_at": _now(),
        "surfaces": surfaces,
        "aggregate": {
            "surface_count": len(surfaces),
            "families": families,
            "classifications": {
                key: sorted(value)
                for key, value in sorted(classifications.items())
            },
            "naturally_asymmetric_families": natural_families,
            "collapsed_equal_cost_families": collapsed_families,
        },
        "recommendation": {
            "generic_target_value_abstraction_supported": generic_target_value_abstraction_supported,
            "prefer_naturally_asymmetric_surface": prefer_naturally_asymmetric_surface,
            "recommended_next_move": recommended_next_move,
            "candidate_surface_ids": sorted(natural_surfaces),
            "live_benchmark_authorized": False,
            "reason": recommendation_reason,
            "next_requirement": (
                "Convert one naturally asymmetric synthetic shape into a resettable, proofable live contract "
                "and rerun an offline contract audit before authorizing live spend."
            ),
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "target-value-census",
        help="run the bounded offline census for same-capability target-value surfaces",
    )
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_target_value_census)


def _cmd_target_value_census(args: Any) -> int:
    try:
        report = run_target_value_census()
    except TargetValueCensusError as exc:
        print(f"target-value-census: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(recommended_next_move={report['recommendation']['recommended_next_move']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
