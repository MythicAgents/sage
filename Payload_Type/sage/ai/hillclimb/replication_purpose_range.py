"""Second branch-rich purpose-range manifest and offline validator.

This module is eval-only. It models a collected state where the foothold can
either spend a transaction on a controlled GPO or use already-observed direct
replication authority. The point is to prove Sage can represent a
``gpo-directory`` versus ``replication-kerberos`` first-frontier choice and to
keep the deployable Ludus bundle aligned with that branch point.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from . import frontier_census
    from ..langgraph import capabilities, engagement_state
except Exception:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore


RANGE_DOMAIN = "replication.local"
RANGE_NETBIOS = "REPLICATION"
RANGE_OBJECTIVE = "From the current foothold, achieve administrative control of replication.local."
RANGE_SOURCE_DIR = "ludus/sage-purpose-ranges/blueprints/sage-replication-range"


@dataclass(frozen=True)
class BranchSpec:
    name: str
    first_action: str
    family: str
    expected_shortest_path: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class ReplicationPurposeRangeSpec:
    name: str
    source_dir: str
    forest: str
    domain: str
    netbios: str
    hosts: tuple[str, ...]
    foothold_host: str
    foothold_identity: str
    objective: str
    branches: tuple[BranchSpec, ...]
    required_snapshot: str
    proof_path: str


REPLICATION_PURPOSE_RANGE = ReplicationPurposeRangeSpec(
    name="sage-replication-range",
    source_dir=RANGE_SOURCE_DIR,
    forest=RANGE_DOMAIN,
    domain=RANGE_DOMAIN,
    netbios=RANGE_NETBIOS,
    hosts=("DC01", "SRV02", "WS01"),
    foothold_host="WS01",
    foothold_identity=r"REPLICATION\user1",
    objective=RANGE_OBJECTIVE,
    branches=(
        BranchSpec(
            name="gpo-detour",
            first_action="gpo-controlled-system-exec",
            family="gpo-directory",
            expected_shortest_path=(
                "gpo-controlled-system-exec",
                "dcsync-krbtgt",
                "forge-golden-ticket",
            ),
            rationale=(
                "A controlled GPO is admissible, but it is a detour once direct "
                "replication authority is already visible."
            ),
        ),
        BranchSpec(
            name="direct-replication",
            first_action="dcsync-krbtgt",
            family="replication-kerberos",
            expected_shortest_path=(
                "dcsync-krbtgt",
                "forge-golden-ticket",
            ),
            rationale="Direct replication authority reaches the objective without a GPO transaction.",
        ),
    ),
    required_snapshot="sage-replication-range-base-v1",
    proof_path="current gauge objective proof plus clean-stop telemetry",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="replication_purpose_range_manifest",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def synthetic_collected_state() -> engagement_state.EngagementState:
    """Return the collected second-range branch point represented as Sage state."""
    now = _now()
    return engagement_state.EngagementState(
        objective=REPLICATION_PURPOSE_RANGE.objective,
        footholds=[engagement_state.Foothold(
            callback_id="replication-purpose-range-1",
            agent="apollo",
            host=REPLICATION_PURPOSE_RANGE.foothold_host,
            forest=REPLICATION_PURPOSE_RANGE.forest,
            identity=REPLICATION_PURPOSE_RANGE.foothold_identity,
            integrity="medium",
            alive=True,
            source="replication_purpose_range_manifest",
            timestamp=now,
        )],
        graph_facts=[
            _fact("generic-write:gpo:srv02-policy"),
            _fact("gpo-domain:srv02-policy:replication.local"),
            _fact("gpo-guid:srv02-policy:22222222-2222-2222-2222-222222222222"),
            _fact("gpo-affects-computer:srv02-policy:srv02:replication.local"),
            _fact("ds-replication-rights:replication.local"),
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


def _branch_report(state: engagement_state.EngagementState, branch: BranchSpec) -> dict[str, Any]:
    action = _select_action(state, branch.first_action)
    if action is None:
        return {
            "branch": branch.name,
            "family": branch.family,
            "reachable": False,
            "modeled_transactions": None,
            "path": [],
            "path_names": [],
            "expected_shortest_path": list(branch.expected_shortest_path),
            "path_matches_expected": False,
            "reason": f"expected first capability {branch.first_action!r} was not admissible",
        }
    reachability = asyncio.run(frontier_census.modeled_branch_reachability(state, action))
    path = list(reachability.get("path", []) or [])
    path_names = [str(item.get("name", "") or "") for item in path]
    return {
        "branch": branch.name,
        "family": branch.family,
        "first_action": branch.first_action,
        "rationale": branch.rationale,
        **reachability,
        "path_names": path_names,
        "expected_shortest_path": list(branch.expected_shortest_path),
        "path_matches_expected": path_names == list(branch.expected_shortest_path),
    }


def validate_replication_purpose_range() -> dict[str, Any]:
    """Return an offline acceptance report for the second purpose-range contract."""
    state = synthetic_collected_state()
    frontier = capabilities.actions_from_state(state)
    first_frontier = [_action_payload(action) for action in frontier]
    offensive = [
        item for item in first_frontier
        if item["family"] not in {"collection", "unknown"}
    ]
    expected_first = [branch.first_action for branch in REPLICATION_PURPOSE_RANGE.branches]
    actual_first = [item["name"] for item in offensive]
    families = sorted({item["family"] for item in offensive})
    branch_reports = [_branch_report(state, branch) for branch in REPLICATION_PURPOSE_RANGE.branches]
    branch_by_name = {item["branch"]: item for item in branch_reports}
    gpo_cost = branch_by_name["gpo-detour"]["modeled_transactions"]
    direct_cost = branch_by_name["direct-replication"]["modeled_transactions"]
    catalog_names = {item["name"] for item in capabilities.capability_catalog()}
    declared_actions = {
        action_name
        for branch in REPLICATION_PURPOSE_RANGE.branches
        for action_name in branch.expected_shortest_path
    }
    checks = {
        "exact_topology": set(REPLICATION_PURPOSE_RANGE.hosts) == {"DC01", "SRV02", "WS01"},
        "exact_first_frontier": actual_first == expected_first,
        "two_distinct_first_families": families == ["gpo-directory", "replication-kerberos"],
        "current_capability_only": declared_actions <= catalog_names,
        "both_branches_reach_objective": all(item["reachable"] is True for item in branch_reports),
        "expected_shortest_paths": all(item["path_matches_expected"] is True for item in branch_reports),
        "direct_replication_is_cheaper": (
            isinstance(gpo_cost, int)
            and isinstance(direct_cost, int)
            and direct_cost < gpo_cost
        ),
    }
    return {
        "kind": "replication_purpose_range_validation",
        "generated_at": _now(),
        "spec": asdict(REPLICATION_PURPOSE_RANGE),
        "first_frontier": first_frontier,
        "distinct_families": families,
        "branches": branch_reports,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }
