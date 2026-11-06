"""Supervised Autonomous Solve asks the hybrid policy to judge every singleton frontier.

Run: cd Payload_Type/sage && python3 -m pytest tests/test_supervised_singleton_objective_selection.py -q

A sole state-admissible candidate is not automatically relevant to the operator's objective. In native supervised
chat with Autonomous Solve on, the controller must consult its existing semantic policy before it offers any
guarded card, on the first selection and on every later one. Auto keeps kernel-owned singleton selection.

The reconstructed state below comes from a retained supervised run: its graph facts, its baseline collection
hop, and the two candidate IDs it recorded. That run's decision packets were not captured, so these tests
assert equivalence of candidate identity only, not a replay of the full packet.
"""
import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.langgraph import capabilities  # noqa: E402
from ai.langgraph import engagement_state as es  # noqa: E402
from ai.langgraph import model  # noqa: E402
from ai.langgraph import policy  # noqa: E402
from tests.test_autonomous_controller_wiring import (  # noqa: E402
    _bare_model,
    _install_exact_card_claim,
    _install_supervised_contract,
)
from tests.test_bloodhound_ingest_contract import FakeIngestTool, install_bloodhound  # noqa: E402


@pytest.fixture(autouse=True)
def _connected_ingest_ready_bloodhound(monkeypatch):
    install_bloodhound(monkeypatch, FakeIngestTool())


SPAWN_OBJECTIVE = (
    "Bounded objective: establish exactly one additional Apollo callback on CASTELBLACK running as "
    "NORTH\\samwell.tarly, by spawning the already-built Apollo shellcode payload through our existing "
    "callback 2. Use spawn with that payload on callback 2. Do exactly that one action, then stop and report "
    "the new callback. No other tasking, no credential use, no changes elsewhere."
)
NORTH_OBJECTIVE = "Obtain Domain Admin on north.sevenkingdoms.local and DCSync its krbtgt, then stop."

# Candidate identities recorded by the retained run: the first frontier's sole GPO action, and the sole
# trusted-scope collection offered once that GPO action had been denied.
RECORDED_GPO_CANDIDATE_ID = (
    "candidate:sha256:007df894332d5b3b452a56829ec68fb625fc70d7032f880c5201254ce2f41f04"
)
RECORDED_COLLECTION_CANDIDATE_ID = (
    "candidate:sha256:e51d6ba7ff0755c1f6ebfc86dec8c45452a39f087a40a91a40a690114359b764"
)
RECORDED_GRAPH_FACTS = (
    "generic-write:gpo:starkwallpaper",
    "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
    "gpo-guid:starkwallpaper:3f1c08f5-d531-4d99-83b3-5ccf0a3981ef",
    "gpo-affects-computer:starkwallpaper:castelblack:north.sevenkingdoms.local",
    "gpo-affects-computer:starkwallpaper:winterfell:north.sevenkingdoms.local",
    "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local",
    "trust-reachable:sevenkingdoms.local:north.sevenkingdoms.local",
    "trust-reachable:north.sevenkingdoms.local:sevenkingdoms.local",
    "trust-reachable:essos.local:sevenkingdoms.local",
    "trust-reachable:sevenkingdoms.local:essos.local",
    "domain-collected:north.sevenkingdoms.local",
    "domain-collected:sevenkingdoms.local",
)


def _recorded_state(objective):
    foothold = es.Foothold(
        callback_id="2", agent="apollo", host="castelblack", forest="north",
        identity="north\\samwell.tarly", integrity="medium", alive=True, source="test", timestamp="",
    )
    baseline = es.Hop(
        id="collect-graph:north|baseline", technique="collect-graph", target="north|baseline",
        effect="graph-built:north|baseline", status="achieved",
        evidence={
            "graph_verified": True,
            "covered_domains": ["north.sevenkingdoms.local", "sevenkingdoms.local"],
            "callback_id": "2",
            "collecting_callback_id": "2",
            "access_level": "medium",
        },
        preconditions=["live-foothold:*"], satisfied_effects=["graph-built:north|baseline"],
        source="test", timestamp="",
    )
    facts = [
        es.GraphFact(predicate=predicate, source="bloodhound:cypher", timestamp="", ttl_seconds=600)
        for predicate in RECORDED_GRAPH_FACTS
    ]
    return es.EngagementState(objective=objective, footholds=[foothold], hops=[baseline], graph_facts=facts)


def _response(payload):
    return type("Response", (), {
        "content": json.dumps(payload),
        "response_metadata": {"model_provider": "test", "model_name": "selector"},
        "usage_metadata": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    })()


class _ScriptedPolicyModel:
    """The configured policy model: answers each request from a script and records what it was shown."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    async def ainvoke(self, messages):
        request = json.loads(messages[-1].content)
        self.requests.append(request)
        answer = self.answers.pop(0)
        return _response(answer(request) if callable(answer) else answer)


def _select_sole(request):
    assert len(request["candidates"]) == 1
    return {
        "disposition": "select",
        "candidate_id": request["candidates"][0]["candidate_id"],
        "rationale": "the sole candidate advances the stated objective",
    }


STOP = {"disposition": "stop", "rationale": "the sole candidate does not serve the objective"}


def _supervised(state, policy_model, executed, *, request_id):
    m = _install_supervised_contract(
        _bare_model(json.dumps({"ok": True}), state, executed),
        request_id=request_id, callback_ids=("2",), actions_complete=False,
    )
    m.mythic_client._engagement_graph_facts = list(state.graph_facts)
    m.policy_mode = "hybrid"
    m.provider = "test"
    m.model = "selector"
    m.llm = policy_model
    cards = []

    async def emit(action_requests):
        cards.append(copy.deepcopy(action_requests))

    m._hitl_card_emitter = emit
    m._hitl_card_pending = False
    return m, cards


def _card_names(cards):
    names = []
    for card in cards:
        args = card[0]["args"]
        action = args.get("action") if isinstance(args.get("action"), dict) else {}
        names.append(action.get("name") or card[0]["name"])
    return names


def test_reconstructed_state_reproduces_the_recorded_gpo_singleton():
    frontier = model._autonomous_policy_candidates(
        capabilities.actions_from_state(_recorded_state(SPAWN_OBJECTIVE))
    )
    assert [policy.semantic_candidate_id(action) for action in frontier] == [RECORDED_GPO_CANDIDATE_ID]


def test_unsupported_spawn_objective_gpo_singleton_is_judged_by_model_and_offers_no_card():
    state = _recorded_state(SPAWN_OBJECTIVE)
    executed = []
    policy_model = _ScriptedPolicyModel([STOP])
    m, cards = _supervised(state, policy_model, executed, request_id="request-spawn-negative")

    report = asyncio.run(m._run_autonomous_controller(SPAWN_OBJECTIVE))

    assert cards == []
    assert executed == []
    assert m._controller_hitl_pending is None
    assert "halted_no_action" in report
    assert len(policy_model.requests) == 1
    request = policy_model.requests[0]
    assert request["objective"] == SPAWN_OBJECTIVE
    assert request["semantic_candidate_ids"] == [RECORDED_GPO_CANDIDATE_ID]
    telemetry = m.controller_runtime_telemetry()
    decision = telemetry["decisions"][0]
    assert decision["disposition"] == "stop"
    assert decision["model_response_observed"] is True
    assert decision["decision_owner"] != "kernel_singleton"
    assert decision["backend_provenance_source"] != "kernel_singleton"
    assert telemetry["kernel_singleton_count"] == 0
    assert telemetry["model_calls"] == 1
    assert telemetry["total_tokens"] == 15
    assert telemetry["semantic_transaction_count"] == 0


@pytest.mark.parametrize("successor_answer", [
    {"disposition": "stop", "rationale": "collection does not advance a spawn"},
    {"disposition": "ask", "rationale": "the objective needs a capability that is not offered"},
    {"disposition": "select", "candidate_id": "candidate:sha256:not-in-frontier"},
])
def test_recorded_successor_collection_singleton_after_denial_has_no_kernel_fallback(successor_answer):
    state = _recorded_state(SPAWN_OBJECTIVE)
    executed = []
    policy_model = _ScriptedPolicyModel([_select_sole, successor_answer])
    m, cards = _supervised(state, policy_model, executed, request_id="request-spawn-successor")

    assert asyncio.run(m._run_autonomous_controller(SPAWN_OBJECTIVE)) == ""
    assert _card_names(cards) == ["gpo-controlled-system-exec"]

    report = asyncio.run(m.handle_controller_hitl_resume("deny"))

    assert executed == []
    assert _card_names(cards) == ["gpo-controlled-system-exec"]
    assert m._controller_hitl_pending is None
    assert "halted_no_action" in report
    assert [request["semantic_candidate_ids"] for request in policy_model.requests] == [
        [RECORDED_GPO_CANDIDATE_ID],
        [RECORDED_COLLECTION_CANDIDATE_ID],
    ]
    telemetry = m.controller_runtime_telemetry()
    assert [decision["model_response_observed"] for decision in telemetry["decisions"]] == [True, True]
    assert telemetry["decisions"][1]["selected_index"] is None
    assert telemetry["kernel_singleton_count"] == 0
    assert telemetry["model_calls"] == 2
    assert telemetry["total_tokens"] == 30
    assert telemetry["semantic_transaction_count"] == 0


def test_supported_objective_singletons_are_model_selected_and_approvals_replay_without_reselection():
    state = _recorded_state(NORTH_OBJECTIVE)
    executed = []

    async def execute(action, inputs):
        executed.append((action["name"], action["target"]))
        effect = action["effects"][0]
        state.hops.append(es.Hop(
            id=f"done-{len(executed)}", technique=f"capability:{action['name']}", target=action["target"],
            effect=effect, status="achieved", evidence={}, preconditions=[], satisfied_effects=[effect],
            source="test", timestamp="",
        ))
        return json.dumps({"ok": True, "verdict": "achieved", "effects": [effect]})

    policy_model = _ScriptedPolicyModel([_select_sole, _select_sole, STOP])
    m, cards = _supervised(state, policy_model, executed, request_id="request-north-positive")
    m.mythic_client.execute_capability = execute

    assert asyncio.run(m._run_autonomous_controller(NORTH_OBJECTIVE)) == ""
    assert _card_names(cards) == ["gpo-controlled-system-exec"]
    assert executed == []
    assert len(policy_model.requests) == 1

    digest = _install_exact_card_claim(m, cards[0], "approval-gpo")
    assert asyncio.run(m.handle_controller_hitl_resume("approve", expected_action_digest=digest)) == ""

    # The approved action ran once from its revalidated replay; only the successor singleton reached the model.
    assert [name for name, _ in executed] == ["gpo-controlled-system-exec"]
    assert len(policy_model.requests) == 2
    assert [candidate["name"] for candidate in policy_model.requests[1]["candidates"]] == [
        "grant-directory-rights",
    ]
    assert _card_names(cards) == ["gpo-controlled-system-exec", "grant-directory-rights"]

    digest = _install_exact_card_claim(m, cards[1], "approval-rights")
    report = asyncio.run(m.handle_controller_hitl_resume("approve", expected_action_digest=digest))

    assert [name for name, _ in executed] == ["gpo-controlled-system-exec", "grant-directory-rights"]
    assert len(policy_model.requests) == 3  # the declined third singleton; neither approval was reselected
    assert _card_names(cards) == ["gpo-controlled-system-exec", "grant-directory-rights"]
    assert m._controller_hitl_pending is None
    assert "halted_no_action" in report

    telemetry = m.controller_runtime_telemetry()
    assert telemetry["model_calls"] == 3
    assert telemetry["total_tokens"] == 45
    assert telemetry["token_usage_complete"] is True
    assert telemetry["kernel_singleton_count"] == 0
    assert len(telemetry["effective_backend_requests"]) == 3
    executed_decisions = [
        transaction for transaction in telemetry["transactions"]
        if transaction.get("capability") in {"gpo-controlled-system-exec", "grant-directory-rights"}
    ]
    assert [transaction["capability"] for transaction in executed_decisions] == [
        "gpo-controlled-system-exec", "grant-directory-rights",
    ]
    for transaction in executed_decisions:
        assert transaction["model_response_observed"] is True
        assert transaction["decision_owner"] != "kernel_singleton"


def test_supervised_denial_of_a_model_selected_singleton_executes_nothing():
    state = _recorded_state(NORTH_OBJECTIVE)
    executed = []
    policy_model = _ScriptedPolicyModel([_select_sole, STOP])
    m, cards = _supervised(state, policy_model, executed, request_id="request-north-denied")

    assert asyncio.run(m._run_autonomous_controller(NORTH_OBJECTIVE)) == ""
    asyncio.run(m.handle_controller_hitl_resume("deny"))

    assert executed == []
    assert _card_names(cards) == ["gpo-controlled-system-exec"]
    assert m._controller_hitl_pending is None


def test_supervised_singleton_without_a_policy_model_stops_without_a_card():
    state = _recorded_state(NORTH_OBJECTIVE)
    executed = []
    m, cards = _supervised(state, None, executed, request_id="request-no-policy-model")

    report = asyncio.run(m._run_autonomous_controller(NORTH_OBJECTIVE))

    assert cards == []
    assert executed == []
    assert "halted_no_action" in report
    assert m.controller_runtime_telemetry()["kernel_singleton_count"] == 0


def test_supervised_multi_candidate_frontier_keeps_its_single_model_selection(monkeypatch):
    state = _recorded_state(NORTH_OBJECTIVE)
    actions = [
        capabilities.CapabilityAction(
            name="offline-check", target=target, effects=[f"checked:{target}"], intent={"callback_id": "2"},
        )
        for target in ("first", "second")
    ]
    monkeypatch.setattr(capabilities, "actions_from_state", lambda _state: actions)

    def select_second(request):
        assert len(request["candidates"]) == 2
        return {"disposition": "select", "candidate_id": request["candidates"][1]["candidate_id"]}

    executed = []
    policy_model = _ScriptedPolicyModel([select_second])
    m, cards = _supervised(state, policy_model, executed, request_id="request-branch")

    assert asyncio.run(m._run_autonomous_controller(NORTH_OBJECTIVE)) == ""

    assert len(policy_model.requests) == 1
    assert [card[0]["args"]["action"]["target"] for card in cards] == ["second"]
    assert executed == []


def test_supervised_empty_frontier_stops_without_model_or_card():
    state = es.EngagementState(objective=NORTH_OBJECTIVE, footholds=[es.Foothold(
        callback_id="2", agent="unsupported-test-agent", host="castelblack", forest="north",
        identity="north\\samwell.tarly", integrity="medium", alive=True, source="test", timestamp="",
    )], hops=[], graph_facts=[])
    executed = []
    policy_model = _ScriptedPolicyModel([])
    m, cards = _supervised(state, policy_model, executed, request_id="request-empty")

    report = asyncio.run(m._run_autonomous_controller(NORTH_OBJECTIVE))

    assert "halted_no_action" in report
    assert policy_model.requests == []
    assert cards == []
    assert executed == []


def test_auto_lane_keeps_kernel_owned_singleton_selection():
    from ai.langgraph.request_contract import build_request_contract

    state = _recorded_state(NORTH_OBJECTIVE)
    executed = []
    m = _bare_model(
        json.dumps({"ok": False, "verdict": "blocked", "reason": "stop after one attempt"}), state, executed,
    )
    m.mode = "auto"
    m.command_name = "chat"
    m._autonomous_solve = False
    m._request_contract = None
    m._controller_hitl_pending = None
    m._controller_hitl_approved_key = ""
    m._controller_hitl_approved_pending = None
    m._controller_hitl_objective = ""
    m.state = {"messages": [], "supervisor_messages": []}
    m.install_request_contract(build_request_contract(
        request_id="request-auto",
        channel_id="channel-1",
        operation_id="operation-1",
        mode="auto",
        autonomous_solve=False,
        callback_ids=("2",),
    ))
    m.mythic_client._engagement_graph_facts = list(state.graph_facts)
    m.policy_mode = "hybrid"
    m.provider = "test"
    m.model = "selector"
    policy_model = _ScriptedPolicyModel([STOP, STOP, STOP])
    m.llm = policy_model

    assert m._controller_hitl_enabled() is False
    asyncio.run(m._run_autonomous_controller(NORTH_OBJECTIVE))

    assert policy_model.requests == []
    assert executed and executed[0][0]["name"] == "gpo-controlled-system-exec"
    decision = m.controller_runtime_telemetry()["decisions"][0]
    assert decision["decision_owner"] == "kernel_singleton"
    assert decision["model_response_observed"] is False


def test_supervised_autonomous_solve_off_stays_off_the_controller():
    from ai.langgraph.request_contract import build_request_contract

    m = _bare_model(json.dumps({"ok": True}), _recorded_state(NORTH_OBJECTIVE), [])
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = False
    m._request_contract = None
    m.state = {"messages": [], "supervisor_messages": []}
    m.install_request_contract(build_request_contract(
        request_id="request-supervised-off",
        channel_id="channel-1",
        operation_id="operation-1",
        mode="supervised",
        autonomous_solve=False,
        callback_ids=("2",),
    ))

    assert m._controller_hitl_enabled() is False
    assert m._should_use_controller(is_interactive=True, prompt=NORTH_OBJECTIVE) is False
