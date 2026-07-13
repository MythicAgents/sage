"""Offline contract for the purpose-range true replanning benchmark.

This module is eval-only. It starts from the existing branch-rich purpose
range, forces the shared LAPS/ADCS prefix through a late CA-export attempt,
records a repairable endpoint-control blocker, and proves that the resulting
frontier contains both a repair-in-place route and a GPO detour route.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from . import frontier_census, purpose_range
    from ..langgraph import capabilities, engagement_state
except Exception:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import purpose_range  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore


SHARED_PREFIX = (
    "read-managed-local-admin-secret",
    "use-managed-local-admin-secret",
    "execute-as-local-admin",
)
BLOCKED_ACTION = "adcs-ca-private-key-export"
REPAIR_PATH = (
    "endpoint-protection-adjustment",
    "adcs-ca-private-key-export",
    "adcs-certificate-auth",
)
DETOUR_PATH = (
    "gpo-controlled-system-exec",
    "grant-directory-rights",
    "dcsync-krbtgt",
    "forge-golden-ticket",
)


@dataclass(frozen=True)
class ReplanningBenchmarkSpec:
    name: str
    scenario: str
    objective: str
    foothold_host: str
    foothold_identity: str
    target_host: str
    target_domain: str
    shared_prefix: tuple[str, ...]
    blocked_action: str
    repair_path: tuple[str, ...]
    detour_path: tuple[str, ...]
    blocker_reason: str
    blocker_failure_class: str
    blocker_probe: tuple[tuple[str, Any], ...]
    proof_path: str


REPLANNING_BENCHMARK = ReplanningBenchmarkSpec(
    name="purpose-range-ca-export-replanning",
    scenario="purpose-range-ca-export-replanning",
    objective=purpose_range.PURPOSE_RANGE.objective,
    foothold_host=purpose_range.PURPOSE_RANGE.foothold_host,
    foothold_identity=purpose_range.PURPOSE_RANGE.foothold_identity,
    target_host="ca01",
    target_domain=purpose_range.PURPOSE_RANGE.domain,
    shared_prefix=SHARED_PREFIX,
    blocked_action=BLOCKED_ACTION,
    repair_path=REPAIR_PATH,
    detour_path=DETOUR_PATH,
    blocker_reason="endpoint protection blocked CA export tooling on ca01",
    blocker_failure_class="transient",
    blocker_probe=(
        ("tool_execution_failed", True),
        ("defender_blocked", True),
        ("target_host", "ca01"),
        ("target_domain", "range.local"),
    ),
    proof_path="current gauge objective proof plus clean-stop and runtime telemetry",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def synthetic_collected_state() -> engagement_state.EngagementState:
    """Return the collected purpose-range branch point used by the contract."""
    return purpose_range.synthetic_collected_state()


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


def _replay_achieved(
    state: engagement_state.EngagementState,
    expected_names: tuple[str, ...],
) -> tuple[engagement_state.EngagementState, list[dict[str, Any]], str]:
    current = state
    path: list[dict[str, Any]] = []
    for expected_name in expected_names:
        action = _select_action(current, expected_name)
        if action is None:
            return current, path, f"expected capability {expected_name!r} was not admissible"
        path.append(_action_payload(action))
        current = frontier_census._apply_modeled_action(current, action, _now())
    return current, path, "declared capability effects replayed"


def _blocked_state(
    state: engagement_state.EngagementState,
    action: Any,
) -> engagement_state.EngagementState:
    effects = list(getattr(action, "effects", None) or [])
    effect = effects[0] if effects else f"blocked:{getattr(action, 'name', 'action')}"
    evidence = {
        "source": "replanning_benchmark_manifest",
        "verify_reason": REPLANNING_BENCHMARK.blocker_reason,
        "terminal_failure": False,
        "failure_class": REPLANNING_BENCHMARK.blocker_failure_class,
        "retryable_failure": True,
        **dict(REPLANNING_BENCHMARK.blocker_probe),
    }
    return engagement_state.record_effect_result(
        state,
        f"replanning-benchmark:{getattr(action, 'name', 'action')}",
        str(getattr(action, "target", "") or ""),
        effect,
        "blocked",
        evidence,
        _now(),
        preconditions=list(getattr(action, "preconditions", None) or []),
        satisfied_effects=effects or [effect],
    )


def validate_replanning_benchmark() -> dict[str, Any]:
    """Return an offline acceptance report for the true replanning contract."""
    state = synthetic_collected_state()
    first_frontier = [_action_payload(action) for action in capabilities.actions_from_state(state)]
    first_offensive = [
        item for item in first_frontier
        if item["family"] not in {"collection", "unknown"}
    ]
    prefix_state, shared_path, shared_reason = _replay_achieved(state, REPLANNING_BENCHMARK.shared_prefix)
    late_frontier = [_action_payload(action) for action in capabilities.actions_from_state(prefix_state)]
    blocked_action = _select_action(prefix_state, REPLANNING_BENCHMARK.blocked_action)
    blocker_probe = dict(REPLANNING_BENCHMARK.blocker_probe)
    blocker_verification = capabilities.verify_capability(REPLANNING_BENCHMARK.blocked_action, blocker_probe)
    blocked_state = _blocked_state(prefix_state, blocked_action) if blocked_action is not None else prefix_state
    post_blocker_frontier = [_action_payload(action) for action in capabilities.actions_from_state(blocked_state)]
    repaired_state, repair_path, repair_reason = _replay_achieved(blocked_state, REPLANNING_BENCHMARK.repair_path)
    detour_state, detour_path, detour_reason = _replay_achieved(blocked_state, REPLANNING_BENCHMARK.detour_path)

    first_names = [item["name"] for item in first_offensive]
    late_names = [item["name"] for item in late_frontier]
    post_names = [item["name"] for item in post_blocker_frontier]
    post_families = sorted({item["family"] for item in post_blocker_frontier})
    required_post_names = {
        "gpo-controlled-system-exec",
        "endpoint-protection-adjustment",
        "adcs-ca-private-key-export",
    }
    catalog_names = {item["name"] for item in capabilities.capability_catalog()}
    declared_actions = {
        *REPLANNING_BENCHMARK.shared_prefix,
        REPLANNING_BENCHMARK.blocked_action,
        *REPLANNING_BENCHMARK.repair_path,
        *REPLANNING_BENCHMARK.detour_path,
    }
    checks = {
        "natural_initial_frontier_is_branch_rich": first_names
        == ["gpo-controlled-system-exec", "read-managed-local-admin-secret"],
        "forced_prefix_reaches_late_blocker": (
            [item["name"] for item in shared_path] == list(REPLANNING_BENCHMARK.shared_prefix)
            and blocked_action is not None
            and {"gpo-controlled-system-exec", REPLANNING_BENCHMARK.blocked_action} <= set(late_names)
        ),
        "blocker_is_verifier_backed_and_repairable": (
            getattr(blocker_verification, "verdict", "") == "blocked"
            and any(
                getattr(hop, "status", "") == "blocked"
                and getattr(hop, "evidence", {}).get("verify_reason") == REPLANNING_BENCHMARK.blocker_reason
                and getattr(hop, "evidence", {}).get("defender_blocked") is True
                and getattr(hop, "evidence", {}).get("tool_execution_failed") is True
                and getattr(hop, "evidence", {}).get("terminal_failure") is False
                for hop in blocked_state.hops
            )
        ),
        "post_blocker_frontier_has_multiple_recovery_families": (
            required_post_names <= set(post_names)
            and {"gpo-directory", "managed-local-admin", "adcs"} <= set(post_families)
        ),
        "repair_in_place_reaches_objective": (
            [item["name"] for item in repair_path] == list(REPLANNING_BENCHMARK.repair_path)
            and frontier_census._modeled_objective_met(repaired_state)
        ),
        "detour_reaches_objective": (
            [item["name"] for item in detour_path] == list(REPLANNING_BENCHMARK.detour_path)
            and frontier_census._modeled_objective_met(detour_state)
        ),
        "current_capability_only": declared_actions <= catalog_names,
    }
    return {
        "kind": "replanning_benchmark_validation",
        "generated_at": _now(),
        "spec": asdict(REPLANNING_BENCHMARK),
        "first_frontier": first_frontier,
        "shared_path": shared_path,
        "shared_path_reason": shared_reason,
        "late_frontier": late_frontier,
        "blocker": {
            "action": _action_payload(blocked_action) if blocked_action is not None else None,
            "reason": REPLANNING_BENCHMARK.blocker_reason,
            "failure_class": REPLANNING_BENCHMARK.blocker_failure_class,
            "probe": blocker_probe,
            "verification": {
                "verdict": getattr(blocker_verification, "verdict", ""),
                "reason": getattr(blocker_verification, "reason", ""),
            },
            "post_blocker_frontier": post_blocker_frontier,
            "post_blocker_families": post_families,
        },
        "repair_path": repair_path,
        "repair_reason": repair_reason,
        "detour_path": detour_path,
        "detour_reason": detour_reason,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }
