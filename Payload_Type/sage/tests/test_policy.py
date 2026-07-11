import asyncio
import json

from ai.langgraph import autonomous_controller as ac
from ai.langgraph import policy


class Action:
    def __init__(self, name, target, effect):
        self.name = name
        self.target = target
        self.preconditions = []
        self.effects = [effect]
        self.reason = ""


class State:
    def __init__(self, effects=()):
        self.effects = set(effects)

    def achieved_effects(self):
        return set(self.effects)


def test_symbolic_policy_preserves_first_admissible_selection():
    candidates = [Action("first", "a", "effect:a"), Action("second", "b", "effect:b")]
    decision = asyncio.run(policy.SymbolicPolicy().select(
        episode_id="episode-1",
        objective="test",
        state=State(),
        candidates=candidates,
        history=[],
    ))
    assert decision.policy_mode == "symbolic"
    assert decision.selected_index == 0
    assert decision.selected_capability == "first"
    assert decision.candidate_hash.startswith("sha256:")


def test_llm_policy_selects_only_returned_candidate():
    async def decide(_request):
        return json.dumps({
            "disposition": "select",
            "candidate_index": 1,
            "rationale": "second has the required effect",
            "confidence": 0.8,
        })

    candidates = [Action("first", "a", "effect:a"), Action("second", "b", "effect:b")]
    decision = asyncio.run(policy.LLMPolicy(decide, provider="test", model_id="model").select(
        episode_id="episode-1",
        objective="test",
        state=State(),
        candidates=candidates,
        history=[],
    ))
    assert decision.disposition == "select"
    assert decision.selected_index == 1
    assert decision.selected_capability == "second"
    assert decision.model_provider == "test"


def test_null_llm_policy_fails_closed_without_execution():
    state = State()
    action = Action("offensive-action", "target", "effect:target")
    calls = []

    def execute(_action, _decision):
        calls.append(True)
        state.effects.add("effect:target")
        return {"ok": True}

    controller = ac.AutonomousController(
        observe=lambda: state,
        execute=execute,
        frontier_fn=lambda _state: [action],
        policy_backend=policy.LLMPolicy(None),
        objective="test",
        episode_id="episode-null",
        config=ac.ControllerConfig(max_cycles=2),
    )
    result = asyncio.run(controller.run())
    assert result.status == ac.STATUS_NO_ACTION
    assert calls == []
    assert result.policy_mode == "llm"
    assert result.decisions[0]["disposition"] == "stop"
    telemetry = result.to_dict()
    assert telemetry["semantic_transaction_count"] == 0
    assert telemetry["authorized_transaction_count"] == 0
    assert telemetry["semantic_policy_coverage"] == 1.0


def test_controller_passes_policy_decision_to_execution():
    state = State()
    action = Action("offensive-action", "target", "effect:target")
    captured = []

    def execute(selected, decision):
        captured.append((selected, decision))
        state.effects.add("effect:target")
        return {"ok": True}

    controller = ac.AutonomousController(
        observe=lambda: state,
        execute=execute,
        frontier_fn=lambda _state: [action],
        objective_met=lambda current: "effect:target" in current.achieved_effects(),
        policy_backend=policy.SymbolicPolicy(),
        objective="test",
        episode_id="episode-symbolic",
    )
    result = asyncio.run(controller.run())
    assert result.status == ac.STATUS_COMPLETE
    assert captured[0][1].decision_id == result.cycles[0].decision_id
    assert result.policy_mode == "symbolic"
    telemetry = result.to_dict()
    assert telemetry["semantic_transaction_count"] == 1
    assert telemetry["authorized_transaction_count"] == 1
    assert telemetry["semantic_policy_coverage"] == 1.0


def test_controller_result_reports_incomplete_policy_coverage():
    result = ac.ControllerResult(
        status=ac.STATUS_BLOCKED,
        reason="test",
        blocker=None,
        cycle_count=1,
        cycles=[],
        achieved_effects=[],
        episode_id="episode-1",
        policy_mode="llm",
        transactions=[
            {
                "kind": "capability",
                "capability": "first",
                "target": "one",
                "decision_id": "decision-1",
                "policy_mode": "llm",
            },
            {
                "kind": "capability",
                "capability": "second",
                "target": "two",
                "decision_id": "",
                "policy_mode": "",
            },
        ],
    )

    telemetry = result.to_dict()
    assert telemetry["semantic_transaction_count"] == 2
    assert telemetry["authorized_transaction_count"] == 1
    assert telemetry["semantic_policy_coverage"] == 0.5
