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


def test_explicit_invalid_policy_mode_is_rejected():
    try:
        policy.normalize_policy_mode("automatic")
    except ValueError as exc:
        assert "unsupported policy mode" in str(exc)
    else:
        raise AssertionError("invalid policy mode silently normalized")


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
    requests = []

    async def decide(request):
        requests.append(request)
        return json.dumps({
            "disposition": "select",
            "capability": "second",
            "target": "b",
            "rationale": "second has the required effect",
            "confidence": 0.8,
        })

    candidates = [Action("first", "a", "effect:a"), Action("second", "b", "effect:b")]
    decision = asyncio.run(policy.LLMPolicy(
        decide,
        provider="test",
        model_id="model",
        catalog=[
            {"name": "first", "description": "first semantic capability"},
            {"name": "second", "description": "second semantic capability"},
            {"name": "catalog-only", "description": "not currently admissible"},
        ],
    ).select(
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
    assert requests[0]["selection_contract"] == "semantic_catalog"
    assert "candidates" not in requests[0]
    assert [item["name"] for item in requests[0]["capability_catalog"]] == [
        "first",
        "second",
        "catalog-only",
    ]
    assert requests[0]["normalized_state"]["achieved_effects"] == []


def test_hybrid_policy_selects_from_frontier_and_labels_decision():
    requests = []

    async def decide(request):
        requests.append(request)
        return {
            "disposition": "select",
            "candidate_index": 1,
            "rationale": "model-ranked admissible candidate",
        }

    candidates = [Action("first", "a", "effect:a"), Action("second", "b", "effect:b")]
    decision = asyncio.run(policy.HybridPolicy(decide).select(
        episode_id="episode-hybrid",
        objective="test",
        state=State(),
        candidates=candidates,
        history=[],
    ))

    assert decision.policy_mode == "hybrid"
    assert decision.selected_index == 1
    assert decision.selected_capability == "second"
    assert requests[0]["selection_contract"] == "admissible_frontier"
    assert [item["name"] for item in requests[0]["candidates"]] == ["first", "second"]
    assert "capability_catalog" not in requests[0]


def test_llm_policy_rejects_catalog_capability_outside_admissible_frontier():
    async def decide(_request):
        return {
            "disposition": "select",
            "capability": "catalog-only",
            "target": "elsewhere",
            "rationale": "propose a semantic capability",
        }

    decision = asyncio.run(policy.LLMPolicy(
        decide,
        catalog=[
            {"name": "first", "description": "currently admissible"},
            {"name": "catalog-only", "description": "known but currently inadmissible"},
        ],
    ).select(
        episode_id="episode-llm-veto",
        objective="test",
        state=State(),
        candidates=[Action("first", "a", "effect:a")],
        history=[],
    ))

    assert decision.policy_mode == "llm"
    assert decision.disposition == "stop"
    assert decision.selected_index is None
    assert "not currently admissible" in decision.rationale


def test_llm_policy_rejects_frontier_capability_outside_catalog():
    async def decide(_request):
        return {
            "disposition": "select",
            "capability": "frontier-only",
            "target": "a",
        }

    decision = asyncio.run(policy.LLMPolicy(
        decide,
        catalog=[{"name": "catalog-only", "description": "known semantic capability"}],
    ).select(
        episode_id="episode-llm-catalog-veto",
        objective="test",
        state=State(),
        candidates=[Action("frontier-only", "a", "effect:a")],
        history=[],
    ))

    assert decision.disposition == "stop"
    assert decision.selected_index is None
    assert "outside the catalog" in decision.rationale


def test_hybrid_policy_rejects_invalid_frontier_index_without_fallback():
    async def decide(_request):
        return {
            "disposition": "select",
            "candidate_index": 99,
            "rationale": "invalid model output",
        }

    decision = asyncio.run(policy.HybridPolicy(decide).select(
        episode_id="episode-hybrid-veto",
        objective="test",
        state=State(),
        candidates=[Action("first", "a", "effect:a")],
        history=[],
    ))

    assert decision.policy_mode == "hybrid"
    assert decision.disposition == "stop"
    assert decision.selected_index is None
    assert "invalid frontier selection" in decision.rationale


def test_null_hybrid_policy_fails_closed_without_symbolic_selection():
    candidates = [Action("first", "a", "effect:a")]
    decision = asyncio.run(policy.HybridPolicy(None).select(
        episode_id="episode-hybrid",
        objective="test",
        state=State(),
        candidates=candidates,
        history=[],
    ))

    assert decision.policy_mode == "hybrid"
    assert decision.disposition == "stop"
    assert decision.selected_index is None
    assert "no model decision seam" in decision.rationale


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


def test_learned_policy_call_failures_do_not_execute_or_fallback():
    async def fail(_request):
        raise RuntimeError("model unavailable")

    for backend in (
        policy.LLMPolicy(
            fail,
            catalog=[{"name": "offensive-action", "description": "test capability"}],
        ),
        policy.HybridPolicy(fail),
    ):
        state = State()
        action = Action("offensive-action", "target", "effect:target")
        calls = []
        controller = ac.AutonomousController(
            observe=lambda: state,
            execute=lambda *_args: calls.append(True) or {"ok": True},
            frontier_fn=lambda _state: [action],
            policy_backend=backend,
            objective="test",
            episode_id=f"episode-{backend.mode}-failure",
        )

        result = asyncio.run(controller.run())

        assert result.status == ac.STATUS_NO_ACTION
        assert calls == []
        assert result.policy_mode == backend.mode
        assert "policy call failed" in result.decisions[0]["rationale"]


def test_controller_rejects_wrong_mode_policy_decision_without_execution():
    state = State()
    action = Action("offensive-action", "target", "effect:target")
    calls = []

    class WrongModePolicy:
        mode = "llm"

        async def select(self, **_kwargs):
            return policy.PolicyDecision(
                episode_id="episode-wrong-mode",
                decision_id="decision-wrong-mode",
                policy_mode="symbolic",
                candidate_hash=policy.candidate_hash([action]),
                disposition="select",
                selected_index=0,
                selected_capability=action.name,
                selected_target=action.target,
            )

    controller = ac.AutonomousController(
        observe=lambda: state,
        execute=lambda *_args: calls.append(True) or {"ok": True},
        frontier_fn=lambda _state: [action],
        policy_backend=WrongModePolicy(),
        objective="test",
        episode_id="episode-wrong-mode",
    )

    result = asyncio.run(controller.run())

    assert result.status == ac.STATUS_NO_ACTION
    assert calls == []
    telemetry = result.to_dict()
    assert telemetry["policy_identity_valid"] is False
    assert telemetry["policy_switches"][0]["observed_policy_mode"] == "symbolic"
    assert telemetry["semantic_transaction_count"] == 0


def test_controller_rejects_unattributed_policy_decision_without_execution():
    state = State()
    action = Action("offensive-action", "target", "effect:target")
    calls = []

    class UnattributedPolicy:
        mode = "llm"

        async def select(self, **_kwargs):
            return policy.PolicyDecision(
                episode_id="different-episode",
                decision_id="",
                policy_mode="llm",
                candidate_hash=policy.candidate_hash([action]),
                disposition="select",
                selected_index=0,
                selected_capability=action.name,
                selected_target=action.target,
            )

    controller = ac.AutonomousController(
        observe=lambda: state,
        execute=lambda *_args: calls.append(True) or {"ok": True},
        frontier_fn=lambda _state: [action],
        policy_backend=UnattributedPolicy(),
        objective="test",
        episode_id="episode-required",
    )

    result = asyncio.run(controller.run())

    assert result.status == ac.STATUS_NO_ACTION
    assert calls == []
    assert result.decisions[0]["decision_id"] == ""
    assert result.to_dict()["semantic_transaction_count"] == 0


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


def test_controller_result_does_not_authorize_cross_policy_transaction():
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
                "policy_mode": "symbolic",
            },
        ],
    )

    telemetry = result.to_dict()
    assert telemetry["authorized_transaction_count"] == 0
    assert telemetry["semantic_policy_coverage"] == 0.0
    assert telemetry["policy_identity_valid"] is False
    assert telemetry["policy_switches"][0]["source"] == "transaction"
