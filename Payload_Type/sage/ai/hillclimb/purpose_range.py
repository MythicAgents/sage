"""Minimal two-lane policy holdout range manifest and offline validator.

This module is eval-only. It keeps the purpose range honest by expressing the
branch point through the same capability frontier the live controller uses:
after graph collection, ``RANGE\\user1`` must see both the GPO and LAPS lanes.
The validator applies only declared capability effects, so it proves the
current capability surface can represent the benchmark before any live seed is
spent provisioning or solving it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from . import frontier_census
    from ..langgraph import capabilities, engagement_state, policy
except Exception:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import policy  # type: ignore


RANGE_DOMAIN = "range.local"
RANGE_NETBIOS = "RANGE"
RANGE_OBJECTIVE = "From the current foothold, achieve administrative control of range.local."
RANGE_SOURCE_DIR = "ad/SAGE-POLICY-RANGE"


@dataclass(frozen=True)
class LaneSpec:
    name: str
    first_action: str
    family: str
    actions: tuple[str, ...]
    target: str
    rationale: str


@dataclass(frozen=True)
class VariantSpec:
    name: str
    purpose: str
    expected_better_lane: str
    observable_signal: str
    blocked_lane: str = ""
    blocker_evidence: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class PurposeRangeSpec:
    name: str
    source_dir: str
    forest: str
    domain: str
    netbios: str
    hosts: tuple[str, ...]
    foothold_host: str
    foothold_identity: str
    objective: str
    lanes: tuple[LaneSpec, ...]
    variants: tuple[VariantSpec, ...]
    required_snapshot: str
    proof_path: str


PURPOSE_RANGE = PurposeRangeSpec(
    name="sage-policy-range",
    source_dir=RANGE_SOURCE_DIR,
    forest=RANGE_DOMAIN,
    domain=RANGE_DOMAIN,
    netbios=RANGE_NETBIOS,
    hosts=("DC01", "CA01", "SRV02", "WS01"),
    foothold_host="WS01",
    foothold_identity=r"RANGE\user1",
    objective=RANGE_OBJECTIVE,
    lanes=(
        LaneSpec(
            name="gpo",
            first_action="gpo-controlled-system-exec",
            family="gpo-directory",
            actions=(
                "gpo-controlled-system-exec",
                "grant-directory-rights",
                "dcsync-krbtgt",
                "forge-golden-ticket",
            ),
            target="SRV02",
            rationale="GPO control reaches the objective in four modeled semantic transactions.",
        ),
        LaneSpec(
            name="laps",
            first_action="read-managed-local-admin-secret",
            family="managed-local-admin",
            actions=(
                "read-managed-local-admin-secret",
                "use-managed-local-admin-secret",
                "execute-as-local-admin",
                "adcs-ca-private-key-export",
                "adcs-certificate-auth",
            ),
            target="CA01",
            rationale="LAPS read reaches the same objective through the existing CA private-key path.",
        ),
    ),
    variants=(
        VariantSpec(
            name="visible-cost",
            purpose="Both lanes work; GPO is visibly one semantic transaction shorter than LAPS.",
            expected_better_lane="gpo",
            observable_signal="modeled semantic transaction count",
        ),
        VariantSpec(
            name="recovery",
            purpose="The GPO lane emits an explicit verifier blocker and LAPS remains admissible.",
            expected_better_lane="laps",
            observable_signal="verifier blocker evidence on the attempted GPO action",
            blocked_lane="gpo",
            blocker_evidence=(
                ("defender_blocked", True),
                ("terminal_failure", True),
                ("verify_reason", "endpoint protection blocked the staged GPO payload on srv02"),
            ),
        ),
    ),
    required_snapshot="sage-policy-range-base-v6",
    proof_path="current gauge objective proof plus clean-stop telemetry",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="purpose_range_manifest",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def synthetic_collected_state() -> engagement_state.EngagementState:
    """Return the collected purpose-range branch point represented as Sage state."""
    now = _now()
    return engagement_state.EngagementState(
        objective=PURPOSE_RANGE.objective,
        footholds=[engagement_state.Foothold(
            callback_id="purpose-range-1",
            agent="apollo",
            host=PURPOSE_RANGE.foothold_host,
            forest=PURPOSE_RANGE.forest,
            identity=PURPOSE_RANGE.foothold_identity,
            integrity="medium",
            alive=True,
            source="purpose_range_manifest",
            timestamp=now,
        )],
        graph_facts=[
            _fact("generic-write:gpo:srv02-policy"),
            _fact("gpo-domain:srv02-policy:range.local"),
            _fact("gpo-guid:srv02-policy:11111111-1111-1111-1111-111111111111"),
            _fact("gpo-affects-computer:srv02-policy:srv02:range.local"),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=user1;account_domain=range.local;target=ca01;target_domain=range.local"
            ),
        ],
    )


def _action_payload(action: Any) -> dict[str, Any]:
    return frontier_census._action_payload(action)


def _select_action(state: engagement_state.EngagementState, expected_name: str) -> Any | None:
    return next(
        (
            action
            for action in capabilities.actions_from_state(state)
            if str(getattr(action, "name", "") or "") == expected_name
        ),
        None,
    )


def _replay_lane(state: engagement_state.EngagementState, lane: LaneSpec) -> dict[str, Any]:
    current = state
    path: list[dict[str, Any]] = []
    for expected_name in lane.actions:
        action = _select_action(current, expected_name)
        if action is None:
            return {
                "lane": lane.name,
                "family": lane.family,
                "reachable": False,
                "modeled_transactions": len(path),
                "path": path,
                "reason": f"expected capability {expected_name!r} was not admissible",
            }
        path.append(_action_payload(action))
        current = frontier_census._apply_modeled_action(current, action, _now())
    return {
        "lane": lane.name,
        "family": lane.family,
        "reachable": frontier_census._modeled_objective_met(current),
        "modeled_transactions": len(path),
        "path": path,
        "reason": "declared lane actions reach the objective"
        if frontier_census._modeled_objective_met(current)
        else "declared lane actions did not reach the objective",
    }


def _blocked_state(
    state: engagement_state.EngagementState,
    action: Any,
    variant: VariantSpec,
) -> engagement_state.EngagementState:
    evidence = {
        "source": "purpose_range_manifest",
        **dict(variant.blocker_evidence),
    }
    effects = list(getattr(action, "effects", None) or [])
    effect = effects[0] if effects else f"blocked:{getattr(action, 'name', 'action')}"
    return engagement_state.record_effect_result(
        state,
        f"purpose-range:{getattr(action, 'name', 'action')}",
        str(getattr(action, "target", "") or ""),
        effect,
        "blocked",
        evidence,
        _now(),
        preconditions=list(getattr(action, "preconditions", None) or []),
        satisfied_effects=effects or [effect],
    )


def validate_purpose_range() -> dict[str, Any]:
    """Return an offline acceptance report for the purpose-range contract."""
    state = synthetic_collected_state()
    frontier = capabilities.actions_from_state(state)
    first_frontier = [_action_payload(action) for action in frontier]
    offensive = [
        item for item in first_frontier
        if item["family"] not in {"collection", "unknown"}
    ]
    expected_first = [lane.first_action for lane in PURPOSE_RANGE.lanes]
    actual_first = [item["name"] for item in offensive]
    families = sorted({item["family"] for item in offensive})
    lane_reports = [_replay_lane(state, lane) for lane in PURPOSE_RANGE.lanes]
    lane_by_name = {item["lane"]: item for item in lane_reports}

    visible_cost = PURPOSE_RANGE.variants[0]
    gpo_cost = lane_by_name["gpo"]["modeled_transactions"]
    laps_cost = lane_by_name["laps"]["modeled_transactions"]
    visible_cost_report = {
        "name": visible_cost.name,
        "purpose": visible_cost.purpose,
        "observable_signal": visible_cost.observable_signal,
        "expected_better_lane": visible_cost.expected_better_lane,
        "gpo_modeled_transactions": gpo_cost,
        "laps_modeled_transactions": laps_cost,
        "passes": (
            lane_by_name["gpo"]["reachable"] is True
            and lane_by_name["laps"]["reachable"] is True
            and gpo_cost < laps_cost
        ),
    }

    recovery = PURPOSE_RANGE.variants[1]
    blocked_action = _select_action(state, PURPOSE_RANGE.lanes[0].first_action)
    blocked_state = _blocked_state(state, blocked_action, recovery) if blocked_action is not None else state
    post_blocker_frontier = [_action_payload(action) for action in capabilities.actions_from_state(blocked_state)]
    recovery_report = {
        "name": recovery.name,
        "purpose": recovery.purpose,
        "observable_signal": recovery.observable_signal,
        "expected_better_lane": recovery.expected_better_lane,
        "blocked_lane": recovery.blocked_lane,
        "blocker_evidence": dict(recovery.blocker_evidence),
        "post_blocker_frontier": post_blocker_frontier,
        "passes": (
            blocked_action is not None
            and any(item["name"] == "read-managed-local-admin-secret" for item in post_blocker_frontier)
            and any(
                getattr(hop, "evidence", {}).get("defender_blocked") is True
                for hop in blocked_state.hops
            )
        ),
    }

    catalog_names = {item["name"] for item in capabilities.capability_catalog()}
    declared_actions = {
        action_name
        for lane in PURPOSE_RANGE.lanes
        for action_name in lane.actions
    }
    checks = {
        "exact_topology": set(PURPOSE_RANGE.hosts) == {"DC01", "CA01", "SRV02", "WS01"},
        "exact_first_frontier": actual_first == expected_first,
        "two_distinct_first_families": families == ["gpo-directory", "managed-local-admin"],
        "current_capability_only": declared_actions <= catalog_names,
        "both_lanes_reach_objective": all(item["reachable"] is True for item in lane_reports),
        "visible_cost_variant": visible_cost_report["passes"] is True,
        "recovery_variant": recovery_report["passes"] is True,
    }
    return {
        "kind": "purpose_range_validation",
        "generated_at": _now(),
        "spec": asdict(PURPOSE_RANGE),
        "first_frontier": first_frontier,
        "distinct_families": families,
        "lanes": lane_reports,
        "variants": [visible_cost_report, recovery_report],
        "checks": checks,
        "passes_gate": all(checks.values()),
    }
