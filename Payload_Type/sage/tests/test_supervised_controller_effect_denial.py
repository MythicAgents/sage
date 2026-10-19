"""Accepted controller actions consume final-effect refusals before another card."""

import asyncio
import json

import pytest

from ai.langgraph import capabilities as cap, engagement_state as es, mythic_tools as mt
from tests.test_autonomous_controller_wiring import (
    _bare_model, _foothold, _install_exact_card_claim, _install_supervised_contract,
)


@pytest.mark.parametrize("outcome", ["denied", "success", "stale-receipt", "operator-reject"])
def test_controller_effect_denial_stops_without_suppressing_valid_successor(monkeypatch, outcome):
    def no_network(*_args, **_kwargs):
        raise AssertionError("offline controller test attempted network access")

    monkeypatch.setattr("socket.socket.connect", no_network)
    objective = "Complete the two declared offline checks."
    state = es.EngagementState(objective=objective, footholds=[
        _foothold("3", agent="unsupported-test-agent"),
    ], hops=[], graph_facts=[])
    actions = [cap.CapabilityAction(
        name="offline-check", target=target, effects=[f"checked:{target}"],
        intent={"callback_id": "3"},
    ) for target in ("first", "second")]
    calls, completed, cards, messages = [], [], [], []
    monkeypatch.setattr(cap, "actions_from_state", lambda _state: [
        action for action in actions if action.target not in completed
    ])
    model = _install_supervised_contract(
        _bare_model("", state, calls), actions_complete=False,
        request_id="controller-denial-" + outcome,
    )
    model.policy_mode = "symbolic"

    async def emit(items):
        cards.append(items)

    async def stream(text):
        messages.append(text)
        return True

    async def effect(action, _inputs):
        calls.append(action["target"])
        if outcome == "denied":
            # Produce the same real receipt used by a nested authorization guard;
            # only capability effects are inert in this controller-delivery test.
            model.mythic_client._active_approval_claim = model._active_approval_claim
            model.mythic_client._request_contract = model._request_contract
            reason = mt.MythicTools._note_effect_denial(
                model.mythic_client, "STOP — exact registered-file prerequisite denied",
            )
            return json.dumps({"ok": False, "verdict": "failed", "reason": reason})
        completed.append(action["target"])
        state.hops.append(es.Hop(
            id="observed-first", technique="offline-observation", target=action["target"],
            effect=action["effects"][0], status="achieved",
            satisfied_effects=action["effects"], evidence={"source": "inert test producer"},
            preconditions=[], source="test", timestamp="",
        ))
        return json.dumps({"ok": True, "verdict": "achieved"})

    model._hitl_card_emitter = emit
    model._hitl_card_pending = False
    model._stream_message_to_mythic = stream
    model.mythic_client.execute_capability = effect

    async def episode():
        assert await model._run_autonomous_controller(objective) == ""
        assert len(cards) == 1 and calls == []
        digest = _install_exact_card_claim(model, cards[0], "approved-first")
        if outcome == "stale-receipt":
            model.mythic_client._last_effect_denial = {"reason": "old request denial"}
        await model.handle_controller_hitl_resume(
            "deny" if outcome == "operator-reject" else "approve",
            expected_action_digest=digest,
        )

    asyncio.run(episode())
    terminals = model._ensure_request_event_ledger().actual_events(
        kind="control_transition", phase="request_terminal",
    )
    if outcome == "denied":
        assert calls == ["first"] and len(cards) == 1
        assert any("exact registered-file prerequisite denied" in text for text in messages)
        assert len(terminals) == 1 and terminals[0].content == "blocked"
        assert model.mythic_client._last_effect_denial is None
        assert model._controller_hitl_pending is None
    else:
        assert calls == ([] if outcome == "operator-reject" else ["first"])
        assert len(cards) == 2
        assert cards[1][0]["args"]["action"]["target"] == "second"
        assert not terminals
