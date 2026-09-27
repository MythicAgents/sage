from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai.langgraph import model, policy


CATALOG = [{"name": "collect-graph", "description": "existing capability"}]


class Action:
    def __init__(self):
        self.name = "collect-graph"
        self.target = "opaque-target"
        self.preconditions = []
        self.effects = ["effect:opaque"]
        self.reason = ""


def _inputs():
    histories = [
        policy.PolicyDecision(
            episode_id="episode-replay",
            decision_id=f"decision-{index}",
            policy_mode="llm",
            candidate_hash="sha256:prior",
            disposition="select",
            selected_capability=f"capability-{index}",
            selected_target=f"target-{index}",
            expected_evidence=f"evidence-{index}",
            rationale="raw-history-secret",
            raw_response="raw-response-secret",
            response_metadata={"credential": "history-secret"},
        )
        for index in range(20)
    ]
    hops = [
        SimpleNamespace(
            technique=f"technique-{index}",
            target=f"target-{index}",
            effect=f"effect-{index}",
            status="observed",
            preconditions=[] if index == 0 else [f"precondition-{index}"],
            satisfied_effects=None if index == 0 else [f"satisfied-{index}"],
            raw_evidence="raw-hop-secret",
            proof_envelopes=["proof-secret"],
            credentials=["credential-secret"],
        )
        for index in range(30)
    ]
    state = SimpleNamespace(
        hops=hops,
        footholds=[SimpleNamespace(
            callback_id="7", agent="apollo", host="opaque-host", forest="opaque-forest",
            identity="opaque-identity", integrity="high", alive=True, raw_evidence="foothold-secret",
        )],
        graph_facts=[SimpleNamespace(predicate="existing:fact", raw_evidence="graph-secret")],
    )
    state.achieved_effects = lambda: ["existing:achieved"]
    return state, [Action()], histories


def _capture(state, candidates, history):
    return policy._captured_decision_packet(
        objective="measure input behavior",
        state=state,
        candidates=candidates,
        history=history,
        budgets={"task_budget": 1},
        selection_contract=policy.SELECTION_CONTRACT_LLM,
        catalog=CATALOG,
    )


def _llm_select(state, candidates, history, requests):
    async def decide(request):
        requests.append(request)
        return {
            "disposition": "select",
            "capability": "collect-graph",
            "target": "opaque-target",
        }

    return asyncio.run(policy.LLMPolicy(
        decide,
        catalog=CATALOG,
    ).select(
        episode_id="episode-replay",
        objective="measure input behavior",
        state=state,
        candidates=candidates,
        history=history,
        budgets={"task_budget": 1},
    ))


def _controller():
    controller = model.Model.__new__(model.Model)
    controller._controller_observed_decisions = []
    controller._controller_observed_transactions = []
    controller._controller_runtime_telemetry = {"configured_policy_mode": "llm"}
    controller.policy_mode = "llm"
    controller._policy_model_calls = 0
    return controller


def test_replay_capture_requires_both_flags_and_preserves_legacy_bytes(monkeypatch):
    state, candidates, history = _inputs()
    monkeypatch.delenv(policy._CAPTURE_DECISION_PACKETS_ENV, raising=False)
    monkeypatch.delenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, raising=False)
    assert _capture(state, candidates, history) == ({}, "")

    monkeypatch.setenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, "true")
    assert _capture(state, candidates, history) == ({}, "")

    monkeypatch.setenv(policy._CAPTURE_DECISION_PACKETS_ENV, "true")
    monkeypatch.delenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, raising=False)
    legacy_packet, legacy_hash = _capture(state, candidates, history)
    expected = policy._decision_packet(
        objective="measure input behavior", state=state, candidates=candidates,
        history=history, budgets={"task_budget": 1}, selection_contract=policy.SELECTION_CONTRACT_LLM,
    )
    expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert json.dumps(legacy_packet, sort_keys=True, separators=(",", ":"), ensure_ascii=True) == expected_bytes
    assert legacy_hash == f"sha256:{policy.hashlib.sha256(expected_bytes.encode()).hexdigest()}"


def test_actual_llm_request_is_unchanged_while_capture_adds_all_replay_context(monkeypatch):
    state, candidates, history = _inputs()
    monkeypatch.setenv(policy._CAPTURE_DECISION_PACKETS_ENV, "true")
    monkeypatch.delenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, raising=False)
    legacy_requests = []
    _llm_select(state, candidates, history, legacy_requests)

    monkeypatch.setenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, "true")
    replay_requests = []
    decision = _llm_select(state, candidates, history, replay_requests)
    assert replay_requests == legacy_requests
    assert len(replay_requests[0]["prior_decisions"]) == 8
    assert len(replay_requests[0]["normalized_state"]["recent_outcomes"]) == 12
    assert "replay_context" not in replay_requests[0]

    context = decision.decision_packet["replay_context"]
    assert set(context) == {
        "schema", "capability_catalog", "history", "hops", "footholds", "graph_facts", "achieved_effects"
    }
    assert context["schema"] == "m3-policy-input-replay-v1"
    assert context["capability_catalog"] == CATALOG
    assert len(context["history"]) == 20
    assert len(context["hops"]) == 30
    assert set(context["history"][0]) == {
        "decision_id", "disposition", "selected_capability", "selected_target", "expected_evidence"
    }
    assert set(context["hops"][0]) == {
        "technique", "target", "effect", "status", "preconditions", "satisfied_effects"
    }
    assert context["hops"][0]["preconditions"] == []
    assert context["hops"][0]["satisfied_effects"] == []
    assert context["footholds"] == replay_requests[0]["normalized_state"]["footholds"]
    assert context["graph_facts"] == replay_requests[0]["normalized_state"]["graph_facts"]
    assert context["achieved_effects"] == replay_requests[0]["normalized_state"]["achieved_effects"]
    raw_context = json.dumps(context, sort_keys=True)
    for forbidden in ("raw-history-secret", "raw-response-secret", "history-secret", "raw-hop-secret",
                      "proof-secret", "credential-secret", "foothold-secret", "graph-secret"):
        assert forbidden not in raw_context


def test_relevant_replay_input_changes_the_sidecar_hash(monkeypatch):
    state, candidates, history = _inputs()
    monkeypatch.setenv(policy._CAPTURE_DECISION_PACKETS_ENV, "true")
    monkeypatch.setenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, "true")
    first_packet, first_hash = _capture(state, candidates, history)
    history[-1] = replace(history[-1], expected_evidence="changed-existing-expectation")
    second_packet, second_hash = _capture(state, candidates, history)
    assert first_hash != second_hash
    assert first_packet["replay_context"]["history"] != second_packet["replay_context"]["history"]


def test_capture_exports_full_decision_and_native_metadata_uses_projection(monkeypatch):
    state, candidates, history = _inputs(); requests = []
    monkeypatch.setenv(policy._CAPTURE_DECISION_PACKETS_ENV, "true")
    monkeypatch.setenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, "true")
    monkeypatch.setenv(model.observation_channel.ENV_NAME, "/tmp/operator-owned-observation")
    decision = _llm_select(state, candidates, history, requests)
    original_request = json.dumps(requests, sort_keys=True, separators=(",", ":"))
    emitted = []
    monkeypatch.setattr(model.observation_channel, "emit", lambda kind, payload: emitted.append((kind, payload)) or True)
    controller = _controller()
    result = controller._controller_record_policy_decision(decision)
    assert len(emitted) == 1 and emitted[0][0] == "tool_event"
    assert emitted[0][1] == {"event": "policy_decision", "decision": decision.to_dict()}
    assert result["decision_id"] == decision.decision_id
    assert "decision_packet" not in result and "decision_packet_hash" not in result
    native_metadata = controller.controller_runtime_telemetry()
    assert native_metadata["decisions"] == [result]
    assert "decision_packet" not in json.dumps(native_metadata, sort_keys=True)
    assert json.dumps(requests, sort_keys=True, separators=(",", ":")) == original_request


def test_default_near_match_and_failed_delivery_are_distinct(monkeypatch):
    state, candidates, history = _inputs(); requests = []
    monkeypatch.setenv(policy._CAPTURE_DECISION_PACKETS_ENV, "true")
    monkeypatch.setenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, "true")
    decision = _llm_select(state, candidates, history, requests)
    calls = []
    monkeypatch.setattr(model.observation_channel, "emit", lambda *args: calls.append(args))
    monkeypatch.setenv(model.observation_channel.ENV_NAME + "_EXTRA", "/tmp/near-match")
    controller = _controller(); result = controller._controller_record_policy_decision(decision)
    assert not calls and result["decision_packet"] == decision.decision_packet
    assert result["decision_packet_hash"] == decision.decision_packet_hash

    monkeypatch.setenv(model.observation_channel.ENV_NAME, "/tmp/operator-owned-observation")
    monkeypatch.setattr(model.observation_channel, "emit", lambda *args: (_ for _ in ()).throw(
        model.observation_channel.ObservationChannelError("observation delivery failed")
    ))
    failed = _controller()
    with pytest.raises(model.observation_channel.ObservationChannelError, match="observation delivery failed"):
        failed._controller_record_policy_decision(decision)
    assert failed._controller_observed_decisions == []
    assert failed._controller_runtime_telemetry == {"configured_policy_mode": "llm"}

    monkeypatch.setattr(model.observation_channel, "emit", lambda *args: False)
    with pytest.raises(model.observation_channel.ObservationChannelError, match="observation delivery failed"):
        failed._controller_record_policy_decision(decision)
    assert failed._controller_observed_decisions == []
