"""Read-only frontier census for policy discriminator selection.

The census never tasks Mythic or mutates BloodHound. It projects graph facts through
the existing reconciler, renders the existing capability frontier, then runs a
bounded modeled search by applying declared capability effects only. The modeled
search answers "can the current capability surface express a route?" rather than
"will live tradecraft succeed?"
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from . import bare_bloodhound
    from ..langgraph import access_reconciler, capabilities, engagement_state, graph_reconciler, mythic_tools, policy
except Exception:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import bare_bloodhound  # type: ignore
    import access_reconciler  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import graph_reconciler  # type: ignore
    import mythic_tools  # type: ignore
    import policy  # type: ignore


ESSOS_OBJECTIVE = "From the current foothold, achieve administrative control of essos.local."
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_DEPTH = 14
DEFAULT_MAX_NODES = 240


@dataclass(frozen=True)
class StartSpec:
    name: str
    host: str
    forest: str
    identity: str
    objective: str = ESSOS_OBJECTIVE
    proof_scenario: str = "direct-laps-objective"
    bootstrap_evidence: str = ""


DEFAULT_STARTS = (
    StartSpec(
        name="samwell-castelblack",
        host="CASTELBLACK",
        forest="north.sevenkingdoms.local",
        identity=r"NORTH\samwell.tarly",
        proof_scenario="cross-forest-objective",
        bootstrap_evidence="fresh Apollo Samwell bootstrap workflow",
    ),
    StartSpec(
        name="jorah-meereen",
        host="MEEREEN",
        forest="essos.local",
        identity=r"ESSOS\jorah.mormont",
        proof_scenario="direct-laps-objective",
        bootstrap_evidence="retained Apollo Jorah callback config",
    ),
    StartSpec(
        name="khal-braavos",
        host="BRAAVOS",
        forest="essos.local",
        identity=r"ESSOS\khal.drogo",
        proof_scenario="direct-laps-objective",
        bootstrap_evidence="",
    ),
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _identity_key(value: Any) -> str:
    return _text(value).casefold()


def _forest_for_domain(value: Any) -> str:
    domain = _text(value).casefold()
    known = {
        "north": "north.sevenkingdoms.local",
        "sevenkingdoms": "sevenkingdoms.local",
        "essos": "essos.local",
    }.get(domain)
    if known:
        return known
    return access_reconciler.normalize_forest(
        domain,
        netbios_to_fqdn=access_reconciler._engagement_netbios_map(),
    )


def _retained_objective_for_forest(forest: str) -> str:
    if forest in {"north.sevenkingdoms.local", "sevenkingdoms.local", "essos.local"}:
        return ESSOS_OBJECTIVE
    return f"From the current foothold, achieve administrative control of {forest}."


def _retained_proof_scenario_for_forest(forest: str) -> str:
    if forest == "essos.local":
        return "direct-laps-objective"
    if forest in {"north.sevenkingdoms.local", "sevenkingdoms.local"}:
        return "cross-forest-objective"
    if forest == "replication.local":
        return "replication-purpose-range-visible-cost"
    return "purpose-range-visible-cost"


def discover_retained_starts(root: Path | None = None) -> list[StartSpec]:
    """Return additional bootstrap-able principals represented by retained configs."""
    root = root or Path(__file__).resolve().parents[4] / "skills" / "sage-callback-bootstrap"
    starts: list[StartSpec] = []
    seen = {_identity_key(item.identity) for item in DEFAULT_STARTS}
    for path in sorted(root.glob("*callback_config.json")):
        try:
            payload = json.loads(path.read_text())
            callback = ((payload.get("config") or {}).get("callback") or {})
            host = _text(callback.get("host"))
            domain = _text(callback.get("domain"))
            user = _text(callback.get("user"))
            if not host or not user:
                continue
            identity = user if "\\" in user else f"{domain}\\{user}" if domain else user
            forest = _forest_for_domain(domain)
            if not forest or _identity_key(identity) in seen:
                continue
            seen.add(_identity_key(identity))
            starts.append(StartSpec(
                name=f"retained-{path.stem.replace('_callback_config', '')}",
                host=host,
                forest=forest,
                identity=identity,
                objective=_retained_objective_for_forest(forest),
                proof_scenario=_retained_proof_scenario_for_forest(forest),
                bootstrap_evidence=f"retained callback config {path.name}",
            ))
        except Exception:
            continue
    return starts


def candidate_starts() -> list[StartSpec]:
    return [*DEFAULT_STARTS, *discover_retained_starts()]


def _state_for_start(spec: StartSpec) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=spec.objective,
        footholds=[engagement_state.Foothold(
            callback_id=f"census-{spec.name}",
            agent="apollo",
            host=spec.host,
            forest=spec.forest,
            identity=spec.identity,
            integrity="medium",
            alive=True,
            source="frontier_census",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )],
    )


def _replace_graph_facts(
    state: engagement_state.EngagementState,
    facts: list[Any],
) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=state.objective,
        footholds=list(state.footholds),
        hops=list(state.hops),
        graph_facts=list(facts),
        probed_effect_prefixes=set(getattr(state, "probed_effect_prefixes", set()) or set()),
    )


def _action_payload(action: Any, index: int | None = None) -> dict[str, Any]:
    payload = {
        "name": _text(getattr(action, "name", "")),
        "target": _text(getattr(action, "target", "")),
        "family": policy.capability_family(getattr(action, "name", "")),
        "preconditions": list(getattr(action, "preconditions", None) or []),
        "effects": list(getattr(action, "effects", None) or []),
        "reason": _text(getattr(action, "reason", "")),
    }
    if index is not None:
        payload["index"] = index
    return payload


def _modeled_objective_met(state: engagement_state.EngagementState) -> bool:
    if engagement_state.objective_effects_complete(state):
        return True
    targets = list(engagement_state._objective_target_domains(getattr(state, "objective", "") or ""))
    if not targets:
        return False
    for effect in state.achieved_effects():
        prefix, sep, domain = effect.partition(":")
        if sep and prefix in {"da", "ea"} and any(
            engagement_state._domains_equivalent(domain, target)
            for target in targets
        ):
            return True
    return False


def _apply_modeled_action(
    state: engagement_state.EngagementState,
    action: Any,
    now: str,
) -> engagement_state.EngagementState:
    effects = list(getattr(action, "effects", None) or [])
    effect = effects[0] if effects else f"modeled:{_text(getattr(action, 'name', 'action'))}"
    return engagement_state.record_effect_result(
        state,
        f"frontier-census:{_text(getattr(action, 'name', 'action'))}",
        _text(getattr(action, "target", "")),
        effect,
        "achieved",
        {
            "source": "frontier_census",
            "provenance": "modeled_declared_effect",
            "modeled_only": True,
        },
        now,
        preconditions=list(getattr(action, "preconditions", None) or []),
        satisfied_effects=effects or [effect],
    )


def _state_key(state: engagement_state.EngagementState) -> tuple[str, ...]:
    return tuple(sorted(state.achieved_effects()))


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def modeled_branch_reachability(
    state: engagement_state.EngagementState,
    first_action: Any,
    *,
    refresh_fn: Callable[[engagement_state.EngagementState], Any] | None = None,
    frontier_fn: Callable[[Any], list[Any]] = capabilities.actions_from_state,
    objective_fn: Callable[[engagement_state.EngagementState], bool] = _modeled_objective_met,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    """Return bounded modeled reachability for one initial branch."""
    now = datetime.now(timezone.utc).isoformat()
    initial = _apply_modeled_action(state, first_action, now)
    queue: list[tuple[engagement_state.EngagementState, list[dict[str, Any]]]] = [
        (initial, [_action_payload(first_action)])
    ]
    visited: set[tuple[str, ...]] = set()
    explored = 0
    while queue and explored < max_nodes:
        current, path = queue.pop(0)
        if refresh_fn is not None:
            current = await _await_if_needed(refresh_fn(current))
        key = _state_key(current)
        if key in visited:
            continue
        visited.add(key)
        explored += 1
        if objective_fn(current):
            return {
                "reachable": True,
                "modeled_transactions": len(path),
                "path": path,
                "explored_nodes": explored,
                "reason": "declared capability effects reach the objective",
            }
        if len(path) >= max_depth:
            continue
        for action in list(frontier_fn(current) or []):
            queue.append((_apply_modeled_action(current, action, now), [*path, _action_payload(action)]))
    return {
        "reachable": False,
        "modeled_transactions": None,
        "path": [],
        "explored_nodes": explored,
        "reason": (
            "modeled node budget exhausted"
            if explored >= max_nodes
            else "no declared-effect route reached the objective"
        ),
    }


def _proofability(spec: StartSpec) -> dict[str, Any]:
    scenario_supported = spec.proof_scenario in {
        "cross-forest-objective",
        "direct-laps-objective",
        "purpose-range-visible-cost",
        "purpose-range-recovery",
        "purpose-range-ca-export-replanning",
        "replication-purpose-range-visible-cost",
    }
    stable_reset = bool(spec.bootstrap_evidence)
    return {
        "scenario": spec.proof_scenario,
        "objective_probe": scenario_supported,
        "clean_stop_probe": scenario_supported,
        "stable_reset": stable_reset,
        "bootstrap_evidence": spec.bootstrap_evidence,
        "proofable": scenario_supported and stable_reset,
        "reason": (
            "existing gauge scenario and bootstrap evidence cover reset, objective proof, and clean stop"
            if scenario_supported and stable_reset
            else "missing stable bootstrap evidence for this start"
            if scenario_supported
            else "no existing gauge scenario covers this objective"
        ),
    }


def _recommended_discriminator_for_report(report: dict[str, Any]) -> str:
    proof_scenario = _text((report.get("start") or {}).get("proof_scenario"))
    if proof_scenario in {"cross-forest-objective", "direct-laps-objective"}:
        return "goad"
    return proof_scenario or "minimal-two-lane-purpose-range"


def _graph_input_report(spec: StartSpec, collected_domains: list[str] | None) -> dict[str, Any] | None:
    if collected_domains is None:
        return None
    required_domain = _text(spec.forest).casefold()
    observed = sorted({_text(item).casefold() for item in collected_domains if _text(item)})
    complete = required_domain in observed
    return {
        "required_domain": required_domain,
        "collected_domains": observed,
        "complete_for_start": complete,
        "reason": (
            f"BloodHound contains collected input for {required_domain}"
            if complete
            else f"BloodHound does not contain collected input for {required_domain}"
        ),
    }


def _decision_quality(branches: list[dict[str, Any]]) -> dict[str, Any]:
    best_by_family: dict[str, dict[str, Any]] = {}
    for branch in branches:
        if not branch.get("reachable"):
            continue
        family = _text(branch.get("family"))
        current = best_by_family.get(family)
        if current is None or int(branch["modeled_transactions"]) < int(current["modeled_transactions"]):
            best_by_family[family] = branch
    reachable = list(best_by_family.values())
    pair = None
    for left, right in combinations(reachable, 2):
        if left.get("modeled_transactions") != right.get("modeled_transactions"):
            pair = (left, right)
            break
    if pair is None:
        return {
            "visible_tradeoff": False,
            "reason": (
                "fewer than two supported families reach the objective"
                if len(reachable) < 2
                else "reachable families have no visible modeled transaction-count difference"
            ),
            "best_branch_by_family": best_by_family,
        }
    left, right = pair
    return {
        "visible_tradeoff": True,
        "reason": (
            f"{left['family']} reaches the objective in {left['modeled_transactions']} modeled transactions "
            f"while {right['family']} needs {right['modeled_transactions']}"
        ),
        "best_branch_by_family": best_by_family,
    }


def _gate_start(report: dict[str, Any]) -> tuple[bool, str]:
    graph_input = report.get("graph_input")
    if isinstance(graph_input, dict) and graph_input.get("complete_for_start") is not True:
        return False, "current BloodHound snapshot is incomplete for this start"
    families = list(report.get("distinct_families") or [])
    quality = report.get("decision_quality") or {}
    reachable_families = {
        _text(item.get("family"))
        for item in report.get("reachability") or []
        if item.get("reachable")
    }
    proofable = bool((report.get("proofability") or {}).get("proofable"))
    if len(families) < 2:
        return False, "frontier has fewer than two offensive capability families"
    if len(reachable_families) < 2:
        return False, "fewer than two frontier families reach the same objective"
    if quality.get("visible_tradeoff") is not True:
        return False, "no visible strategic tradeoff between reachable families"
    if not proofable:
        return False, "reset/proof path is not stable with the existing gauge"
    return True, "two reachable offensive families expose a visible tradeoff with stable proof"


async def analyze_start(
    spec: StartSpec,
    *,
    refresh_fn: Callable[[engagement_state.EngagementState], Any],
    frontier_fn: Callable[[Any], list[Any]] = capabilities.actions_from_state,
    collected_domains: list[str] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    state = await _await_if_needed(refresh_fn(_state_for_start(spec)))
    frontier = list(frontier_fn(state) or [])
    offensive_families = sorted({
        policy.capability_family(getattr(action, "name", ""))
        for action in frontier
        if policy.capability_family(getattr(action, "name", "")) != "collection"
    })
    branches: list[dict[str, Any]] = []
    for index, action in enumerate(frontier):
        if len(offensive_families) < 2:
            reachability = {
                "reachable": None,
                "modeled_transactions": None,
                "path": [],
                "explored_nodes": 0,
                "reason": "not evaluated because the frontier already fails the two-family gate",
            }
        else:
            reachability = await modeled_branch_reachability(
                state,
                action,
                refresh_fn=refresh_fn,
                frontier_fn=frontier_fn,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        branches.append({
            "index": index,
            "name": _text(getattr(action, "name", "")),
            "target": _text(getattr(action, "target", "")),
            "family": policy.capability_family(getattr(action, "name", "")),
            **reachability,
        })
    report = {
        "start": asdict(spec),
        "objective": spec.objective,
        "controlled_principals": graph_reconciler.controlled_principals_from_state(state),
        "graph_fact_count": len(getattr(state, "graph_facts", []) or []),
        "frontier": [_action_payload(action, index) for index, action in enumerate(frontier)],
        "distinct_families": offensive_families,
        "distinct_family_count": len(offensive_families),
        "reachability": branches,
        "reachability_evaluated": len(offensive_families) >= 2,
        "decision_quality": _decision_quality(branches),
        "proofability": _proofability(spec),
    }
    graph_input = _graph_input_report(spec, collected_domains)
    if graph_input is not None:
        report["graph_input"] = graph_input
    passes, reason = _gate_start(report)
    report["passes_gate"] = passes
    report["gate_reason"] = reason
    return report


class _SingleToolMCPManager:
    def __init__(self, tool: Any):
        self._tool = tool

    async def get_tool_by_name(self, name: str) -> Any:
        return self._tool if name == "cypher_query" else None


def _live_refresh_fn(manager: _SingleToolMCPManager, now: str, ttl_seconds: int):
    cache: dict[tuple[tuple[str, ...], tuple[str, ...]], list[Any]] = {}

    async def refresh(state: engagement_state.EngagementState) -> engagement_state.EngagementState:
        principals = tuple(graph_reconciler.controlled_principals_from_state(state))
        credential_domains = tuple(graph_reconciler.credential_target_domains_from_state(state))
        key = (principals, credential_domains)
        facts = cache.get(key)
        if facts is None:
            facts = await graph_reconciler.reconcile_graph_position(
                manager,
                list(principals),
                state.objective,
                now,
                ttl_seconds,
                credential_domains=list(credential_domains),
            )
            cache[key] = list(facts)
        return _replace_graph_facts(state, facts)

    return refresh


async def _run_census(
    starts: list[StartSpec],
    refresh_fn: Callable[[engagement_state.EngagementState], Awaitable[engagement_state.EngagementState]],
    *,
    collected_domains: list[str] | None,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    reports = [
        await analyze_start(
            spec,
            refresh_fn=refresh_fn,
            collected_domains=collected_domains,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        for spec in starts
    ]
    passing = [item for item in reports if item.get("passes_gate") is True]
    selected = passing[0] if passing else {}
    selected_start = selected.get("start") or {}
    return {
        "kind": "frontier_census",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "modeled_reachability": True,
        "collected_domains": sorted({_text(item).casefold() for item in (collected_domains or []) if _text(item)}),
        "starts": reports,
        "passes_gate": bool(passing),
        "selected_start": selected_start.get("name") if passing else "",
        "recommended_discriminator": _recommended_discriminator_for_report(selected),
        "gate_reason": (
            f"{len(passing)} censused start(s) satisfy the branch-rich discriminator gate"
            if passing
            else "no censused start satisfies the branch-rich discriminator gate"
        ),
    }


async def _live_collected_domains(tool: Any | None) -> list[str] | None:
    if tool is None:
        return None
    return list(await mythic_tools._bloodhound_collected_domains(tool))


def run_live_frontier_census(
    *,
    starts: list[StartSpec] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    """Run the census against the current BloodHound graph without mutating it."""
    _, registry = bare_bloodhound.load_bloodhound_mcp_tools()
    tool = registry.get("cypher_query")
    if tool is None:
        return {
            "kind": "frontier_census",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "modeled_reachability": True,
            "starts": [],
            "passes_gate": False,
            "selected_start": "",
            "recommended_discriminator": "minimal-two-lane-purpose-range",
            "gate_reason": "BloodHound cypher_query tool unavailable",
        }
    now = datetime.now(timezone.utc).isoformat()
    manager = _SingleToolMCPManager(tool)
    collected_domains = asyncio.run(_live_collected_domains(registry.get("domain_info")))
    return asyncio.run(_run_census(
        list(starts or candidate_starts()),
        _live_refresh_fn(manager, now, ttl_seconds),
        collected_domains=collected_domains,
        max_depth=max_depth,
        max_nodes=max_nodes,
    ))
