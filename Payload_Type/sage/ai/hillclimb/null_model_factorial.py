"""Offline null-model factorial for the three Sage policy modes."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

try:
    from . import fitness
    from .range_state import GroundTruth, Milestone, Scenario
    from ..langgraph import autonomous_controller as controller
    from ..langgraph import policy
except Exception:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import fitness  # type: ignore
    from range_state import GroundTruth, Milestone, Scenario  # type: ignore
    import autonomous_controller as controller  # type: ignore
    import policy  # type: ignore


_OBJECTIVE_EFFECT = "objective:test"
_SCENARIO = Scenario(
    name="null-model-factorial",
    engagement_id="offline-null-model-factorial",
    objective="prove the synthetic objective",
    milestone_subset=(Milestone.FOOTHOLD, Milestone.OBJECTIVE),
)


@dataclass(frozen=True)
class _Action:
    name: str = "test-capability"
    target: str = "test-target"
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = (_OBJECTIVE_EFFECT,)
    reason: str = "single semantic capability in the frozen factorial fixture"


class _State:
    def __init__(self) -> None:
        self.effects: set[str] = set()

    def achieved_effects(self) -> set[str]:
        return set(self.effects)


def _backend(mode: str) -> Any:
    if mode == policy.POLICY_SYMBOLIC:
        return policy.SymbolicPolicy()
    if mode == policy.POLICY_LLM:
        return policy.LLMPolicy(
            None,
            provider="null",
            model_id="null",
            catalog=[{"name": _Action.name, "description": "factorial capability"}],
        )
    if mode == policy.POLICY_HYBRID:
        return policy.HybridPolicy(None, provider="null", model_id="null")
    raise ValueError(f"unsupported factorial mode: {mode}")


def _run_mode(mode: str) -> dict[str, Any]:
    state = _State()
    action = _Action()
    executions: list[str] = []

    def execute(selected: _Action, _decision: policy.PolicyDecision) -> dict[str, Any]:
        executions.append(selected.name)
        state.effects.update(selected.effects)
        return {"ok": True}

    result = asyncio.run(controller.AutonomousController(
        observe=lambda: state,
        execute=execute,
        objective_met=lambda current: _OBJECTIVE_EFFECT in current.achieved_effects(),
        frontier_fn=lambda _state: [action],
        policy_backend=_backend(mode),
        objective=_SCENARIO.objective,
        episode_id=f"episode-null-{mode}",
        config=controller.ControllerConfig(max_cycles=3),
    ).run())
    result_data = result.to_dict()
    telemetry = {
        **result_data,
        "configured_policy_mode": mode,
        "model_provider": "null",
        "model_id": "null",
        "model_calls": 0,
        "controller_status": result.status,
        "controller_terminal_reason": result.reason,
        "objective_recognized": result.status == controller.STATUS_COMPLETE,
    }
    objective_proven = _OBJECTIVE_EFFECT in state.achieved_effects()
    ground_truth = GroundTruth(
        scenario=_SCENARIO.name,
        milestones={
            Milestone.FOOTHOLD: True,
            Milestone.OBJECTIVE: objective_proven,
        },
        furthest=Milestone.OBJECTIVE if objective_proven else Milestone.FOOTHOLD,
    )
    card = fitness.score(
        {
            **telemetry,
            "status": "completed",
            "request_completed": True,
        },
        ground_truth,
        scenario=_SCENARIO,
    )
    return {
        "policy_mode": mode,
        "controller_status": result.status,
        "executed_capabilities": executions,
        "telemetry": telemetry,
        "scorecard": asdict(card),
    }


def _row_failures(row: dict[str, Any]) -> list[str]:
    mode = row["policy_mode"]
    telemetry = row["telemetry"]
    card = row["scorecard"]
    executions = row["executed_capabilities"]
    failures: list[str] = []

    expected_transactions = 1 if mode == policy.POLICY_SYMBOLIC else 0
    expected_objective = mode == policy.POLICY_SYMBOLIC
    checks = {
        "policy identity": telemetry["policy_mode"] == mode
        and telemetry["configured_policy_mode"] == mode
        and telemetry["policy_identity_valid"] is True,
        "no policy switch": telemetry["policy_switches"] == [],
        "zero model calls": telemetry["model_calls"] == 0,
        "semantic transaction count": telemetry["semantic_transaction_count"] == expected_transactions,
        "authorized transaction count": telemetry["authorized_transaction_count"] == expected_transactions,
        "semantic policy coverage": telemetry["semantic_policy_coverage"] == 1.0,
        "execution count": len(executions) == expected_transactions,
        "objective proof": card["objective_proven"] is expected_objective,
        "clean terminal": card["clean_stop"] is True,
        "objective clean stop": card["objective_clean_stop"] is expected_objective,
    }
    for label, passed in checks.items():
        if not passed:
            failures.append(label)
    return failures


def run_null_model_factorial() -> dict[str, Any]:
    rows = [_run_mode(mode) for mode in (
        policy.POLICY_SYMBOLIC,
        policy.POLICY_LLM,
        policy.POLICY_HYBRID,
    )]
    failures = {
        row["policy_mode"]: row_failures
        for row in rows
        if (row_failures := _row_failures(row))
    }
    return {
        "kind": "null_model_factorial",
        "verdict": "PASS" if not failures else "FAIL",
        "failures": failures,
        "rows": rows,
    }
