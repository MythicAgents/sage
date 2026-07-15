import asyncio
import hashlib
import json

from ai.langgraph import autonomous_controller as ac
from ai.langgraph import policy


class Action:
    def __init__(self, name, target, effect, operational_cost=None):
        self.name = name
        self.target = target
        self.preconditions = []
        self.effects = [effect]
        self.reason = ""
        if operational_cost is not None:
            self.operational_cost = operational_cost


def _immediate_cost():
    return {
        "interaction_class": "direct",
        "execution_scope": "direct",
        "requires_propagation_wait": False,
        "expected_wait_seconds": 0,
        "wait_reasons": [],
    }


def _gpo_cost(wait_seconds=300):
    return {
        "interaction_class": "propagation-bound",
        "execution_scope": "domain-policy",
        "requires_propagation_wait": True,
        "expected_wait_seconds": wait_seconds,
        "wait_reasons": ["group-policy-refresh"],
    }


class State:
    def __init__(self, effects=()):
        self.effects = set(effects)

    def achieved_effects(self):
        return set(self.effects)


def test_missing_and_invalid_policy_mode_resolve_symbolic_and_valid_modes_remain_stable():
    assert policy.resolve_policy_mode("", default=policy.POLICY_SYMBOLIC) == ("symbolic", "default_missing")
    assert policy.resolve_policy_mode("automatic", default=policy.POLICY_SYMBOLIC) == ("symbolic", "default_invalid")
    assert policy.normalize_policy_mode("automatic") == "symbolic"
    assert policy.normalize_policy_mode("hybrid") == "hybrid"


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
    assert [item["name"] for item in requests[0]["current_admissible_actions"]] == ["first", "second"]
    assert [item["name"] for item in requests[0]["capability_catalog"]] == [
        "first",
        "second",
        "catalog-only",
    ]
    assert requests[0]["normalized_state"]["achieved_effects"] == []


def test_llm_policy_can_select_collection_when_it_is_the_only_admissible_action():
    requests = []

    async def decide(request):
        requests.append(request)
        return {
            "disposition": "select",
            "capability": "collect-graph",
            "target": "north.sevenkingdoms.local|baseline",
            "rationale": "collection is the only legal first step",
        }

    decision = asyncio.run(policy.LLMPolicy(
        decide,
        catalog=[{"name": "collect-graph", "description": "collect graph data"}],
    ).select(
        episode_id="episode-collect",
        objective="test",
        state=State(),
        candidates=[Action("collect-graph", "north.sevenkingdoms.local|baseline", "graph-collected")],
        history=[],
    ))

    assert decision.disposition == "select"
    assert decision.selected_capability == "collect-graph"
    assert decision.selected_family == "collection"
    assert decision.selected_is_first_admissible is True
    assert requests[0]["current_admissible_actions"] == [{
        "name": "collect-graph",
        "target": "north.sevenkingdoms.local|baseline",
        "preconditions": [],
        "effects": ["graph-collected"],
        "operational_cost": _immediate_cost(),
        "reason": "",
    }]


def test_learned_policy_payloads_expose_same_operational_cost_contract():
    action = Action(
        "gpo-controlled-system-exec",
        "gpo=workstation-policy;domain=lab.local",
        "system-exec:gpo:workstation-policy@lab.local",
        operational_cost=_gpo_cost(120),
    )

    llm_payload = policy.LLMPolicy(
        None,
        catalog=[{"name": action.name, "description": "test capability"}],
    ).request_payload("test", State(), [action], [], None)
    hybrid_payload = policy.HybridPolicy(None).request_payload("test", State(), [action], [], None)

    assert llm_payload["current_admissible_actions"][0]["operational_cost"] == _gpo_cost(120)
    assert hybrid_payload["candidates"][0]["operational_cost"] == _gpo_cost(120)


def test_policy_payload_defaults_missing_operational_cost_to_explicit_immediate_profile():
    action = Action("collect-graph", "lab.local|baseline", "graph-collected")

    payload = policy.LLMPolicy(
        None,
        catalog=[{"name": action.name, "description": "test capability"}],
    ).request_payload("test", State(), [action], [], None)

    assert payload["current_admissible_actions"][0]["operational_cost"] == _immediate_cost()


def test_candidate_hash_includes_visible_operational_cost_metadata():
    immediate = Action("same", "target", "effect")
    delayed = Action("same", "target", "effect", operational_cost=_gpo_cost(120))

    assert policy.candidate_hash([immediate]) != policy.candidate_hash([delayed])


def test_eval_decision_packet_capture_is_opt_in_and_hashes_common_frontier(monkeypatch):
    candidates = [Action("first", "a", "effect:a"), Action("second", "b", "effect:b")]

    without_capture = asyncio.run(policy.SymbolicPolicy().select(
        episode_id="episode-1",
        objective="test",
        state=State(),
        candidates=candidates,
        history=[],
    ))
    assert without_capture.decision_packet == {}
    assert without_capture.decision_packet_hash == ""

    monkeypatch.setenv("SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS", "1")
    with_capture = asyncio.run(policy.SymbolicPolicy().select(
        episode_id="episode-2",
        objective="test",
        state=State(),
        candidates=candidates,
        history=[],
        budgets={"max_cycles": 7, "wall_clock_budget_s": 2700.0},
    ))

    assert with_capture.decision_packet["selection_contract"] == "symbolic_frontier"
    assert with_capture.decision_packet["policy_version"] == "symbolic-v1"
    assert with_capture.decision_packet["candidate_hash"] == with_capture.candidate_hash
    assert [item["name"] for item in with_capture.decision_packet["admissible_frontier"]] == ["first", "second"]
    assert with_capture.decision_packet["budgets"] == {"max_cycles": 7, "wall_clock_budget_s": 2700}
    raw = json.dumps(
        with_capture.decision_packet,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert with_capture.decision_packet_hash == f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def test_llm_policy_persists_raw_decline_and_response_backend_provenance():
    class Response:
        content = json.dumps({
            "disposition": "stop",
            "rationale": "need a safer branch",
        })
        response_metadata = {
            "model_name": "provider-model-v1",
            "model_provider": "runtime-provider",
            "request_id": "request-7",
        }

    async def decide(_request):
        return Response()

    decision = asyncio.run(policy.LLMPolicy(
        decide,
        provider="configured-provider",
        model_id="configured-model",
        catalog=[{"name": "first", "description": "first semantic capability"}],
    ).select(
        episode_id="episode-stop",
        objective="test",
        state=State(),
        candidates=[Action("first", "a", "effect:a")],
        history=[],
    ))

    assert decision.disposition == "stop"
    assert decision.raw_disposition == "stop"
    assert decision.raw_rationale == "need a safer branch"
    assert '"disposition": "stop"' in decision.raw_response
    assert decision.model_response_observed is True
    assert decision.effective_backend == "runtime-provider:provider-model-v1"
    assert decision.effective_model_provider == "runtime-provider"
    assert decision.effective_model_id == "provider-model-v1"
    assert decision.backend_provenance_source == "response_metadata.model_name"


def test_llm_policy_replay_reuses_original_decision_provenance():
    replay = {
        "decision_id": "decision-original",
        "policy_mode": "llm",
        "rationale": "original collection choice",
        "confidence": 0.9,
        "expected_evidence": "graph verified",
        "model_provider": "configured-provider",
        "model_id": "configured-model",
        "raw_response": '{"disposition":"select","capability":"collect-graph"}',
        "raw_disposition": "select",
        "raw_rationale": "original collection choice",
        "effective_backend": "runtime-provider:runtime-model",
        "effective_model_provider": "runtime-provider",
        "effective_model_id": "runtime-model",
        "backend_provenance_source": "response_metadata.model_name",
        "response_metadata": {
            "model_name": "runtime-model",
            "model_provider": "runtime-provider",
        },
    }

    async def decide(_request):
        return {
            "disposition": "select",
            "capability": "collect-graph",
            "target": "north.sevenkingdoms.local|baseline",
            "_policy_model_response_observed": False,
            "_policy_replay_decision": replay,
        }

    decision = asyncio.run(policy.LLMPolicy(
        decide,
        catalog=[{"name": "collect-graph", "description": "collect graph data"}],
    ).select(
        episode_id="episode-replay",
        objective="test",
        state=State(),
        candidates=[Action("collect-graph", "north.sevenkingdoms.local|baseline", "graph-collected")],
        history=[],
    ))

    assert decision.decision_id == "decision-original"
    assert decision.selected_capability == "collect-graph"
    assert decision.model_response_observed is False
    assert decision.raw_response == replay["raw_response"]
    assert decision.effective_backend == "runtime-provider:runtime-model"
    assert decision.response_metadata == replay["response_metadata"]


def test_hybrid_policy_selects_from_frontier_and_labels_decision():
    requests = []

    async def decide(request):
        requests.append(request)
        return {
            "disposition": "select",
            "candidate_id": request["candidates"][1]["candidate_id"],
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
    assert decision.policy_version == "hybrid-full-frontier-v2"
    assert decision.decision_owner == "model_branch"
    assert decision.branch_opportunity_count == 1
    assert decision.model_owned_decision_count == 1
    assert decision.model_branch_coverage == 1.0
    assert decision.selected_candidate_id == requests[0]["candidates"][1]["candidate_id"]
    assert requests[0]["policy_version"] == "hybrid-full-frontier-v2"
    assert requests[0]["selection_contract"] == "hybrid-full-frontier-v2"
    assert [item["name"] for item in requests[0]["candidates"]] == ["first", "second"]
    assert all("candidate_id" in item for item in requests[0]["candidates"])
    assert all("index" not in item for item in requests[0]["candidates"])
    assert "candidate_id" in requests[0]["response_schema"]
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


def test_hybrid_policy_rejects_invalid_frontier_id_without_fallback():
    async def decide(_request):
        return {
            "disposition": "select",
            "candidate_id": "candidate:sha256:not-admissible",
            "rationale": "invalid model output",
        }

    decision = asyncio.run(policy.HybridPolicy(decide).select(
        episode_id="episode-hybrid-veto",
        objective="test",
        state=State(),
        candidates=[Action("first", "a", "effect:a"), Action("second", "b", "effect:b")],
        history=[],
    ))

    assert decision.policy_mode == "hybrid"
    assert decision.disposition == "stop"
    assert decision.selected_index is None
    assert "invalid frontier selection" in decision.rationale


def test_null_hybrid_policy_fails_closed_without_symbolic_selection():
    candidates = [Action("first", "a", "effect:a"), Action("second", "b", "effect:b")]
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


def test_hybrid_singleton_is_kernel_owned_without_model_call():
    requests = []

    async def decide(request):
        requests.append(request)
        return {"disposition": "stop"}

    decision = asyncio.run(policy.HybridPolicy(decide).select(
        episode_id="episode-hybrid-singleton",
        objective="test",
        state=State(),
        candidates=[Action("only", "a", "effect:a")],
        history=[],
    ))

    assert requests == []
    assert decision.disposition == "select"
    assert decision.selected_index == 0
    assert decision.decision_owner == "kernel_singleton"
    assert decision.kernel_singleton_count == 1
    assert decision.model_owned_decision_count == 0
    assert decision.model_response_observed is False


def test_semantic_candidate_identity_is_permutation_stable_but_ordered_frontier_hash_is_not():
    first = Action("first", "a", "effect:a")
    first.preconditions = ["b", "a"]
    first.effects = ["effect:b", "effect:a"]
    first.reason = "first wording"
    same_first = Action("first", "a", "effect:a")
    same_first.preconditions = ["a", "b"]
    same_first.effects = ["effect:a", "effect:b"]
    same_first.reason = "different wording"
    second = Action("second", "b", "effect:b")
    second.preconditions = ["x"]

    assert policy.semantic_candidate_id(first) == policy.semantic_candidate_id(same_first)
    assert policy.candidate_set_hash([first, second]) == policy.candidate_set_hash([second, first])
    assert policy.ordered_frontier_hash([first, second]) != policy.ordered_frontier_hash([second, first])


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
        candidates = [action]
        if backend.mode == "hybrid":
            candidates.append(Action("alternate-action", "other", "effect:other"))
        calls = []
        controller = ac.AutonomousController(
            observe=lambda: state,
            execute=lambda *_args: calls.append(True) or {"ok": True},
            frontier_fn=lambda _state: candidates,
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
