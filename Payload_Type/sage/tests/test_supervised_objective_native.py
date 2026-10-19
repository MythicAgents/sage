"""Native chat -> real Model.invoke/controller -> exact card resume, with inert effects."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import capabilities as cap, engagement_state as es
from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import RequestLane, StopConditionKind
from sage_chat import session
from sage_chat.headless import build_chat_request


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-focused-capability-tests/scripts/run_offline_human_led_case.py")
spec = importlib.util.spec_from_file_location("objective_native_driver", SCRIPT)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


@pytest.mark.parametrize("first_decision", ["accept", "reject", "respond", "respond-stop"])
def test_native_objective_survives_decisions_without_reusing_approval(monkeypatch, tmp_path, first_decision):
    asyncio.run(_native_objective_case(monkeypatch, tmp_path, first_decision))


async def _native_objective_case(monkeypatch, tmp_path, first_decision):
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.delenv("SAGE_AUTONOMOUS_CONTROLLER", raising=False)
    monkeypatch.delenv("SAGE_CONTROLLER_HITL", raising=False)

    def no_network(*_args, **_kwargs):
        raise AssertionError("offline native objective probe attempted network access")

    monkeypatch.setattr("socket.socket.connect", no_network)
    monkeypatch.setattr("socket.socket.connect_ex", no_network)
    objective = "Verify the two declared checks; retain uncertainty until evidence exists."
    steer = "Skip the first check and propose the second check for approval."
    effects, policy_inputs = [], []
    state = es.EngagementState(objective=objective, footholds=[
        es.Foothold(callback_id="7", agent="unsupported-test-agent", host="lab.test",
                    forest="corp.test", identity="corp\\operator", alive=True,
                    integrity="high", source="offline-fixture", timestamp=""),
    ])
    actions = [
        cap.CapabilityAction(name="offline-check", target=target,
                             effects=[f"checked:{target}"], intent={"callback_id": "7"})
        for target in ("first", "second")
    ]
    # Inject only the observed-state frontier and effect producer, retaining real
    # transport, routing, policy, card correlation, and final request admission.
    monkeypatch.setattr(cap, "actions_from_state", lambda _state: [
        action for action in actions if action.target not in [item["target"] for item in effects]
    ])

    def select(messages):
        request = json.loads(messages[-1].content)
        policy_inputs.append(request)
        return AIMessage(content=json.dumps({
            "disposition": "select", "candidate_id": request["candidates"][0]["candidate_id"],
            "rationale": "scripted offline selection",
        }))

    fake = driver.ScriptedModel(messages=iter(itertools.repeat(select)))
    model = Model(provider="openai", model="offline-model", system_prompt="offline probe",
                  config={"configurable": {"api_key": "offline-only",
                           "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
                  task_id=0, agent_task_id="offline-objective", mode="supervised",
                  autonomous_solve=True, policy_mode="hybrid", operation_id=1,
                  channel_id=1, apitoken_id=0)
    model.mythic_client = MythicTools(agent_task_id="offline-objective", operation_id=1, channel_id=1)
    model.mythic_client.get_tools = lambda _names: []
    model._payload_names = []
    model.memory = InMemorySaver()
    model.llm = fake
    model._get_base_chat_model = lambda *args, **kwargs: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"

    async def observe():
        return state

    async def refresh(*_args):
        return None

    async def effect(action, inputs):
        model.mythic_client._require_request_contract_effect(
            "execute_capability", {"action": action, "inputs": inputs},
        )
        effects.append({"target": action["target"], "args": {"action": action, "inputs": inputs}})
        state.hops.append(es.Hop(id=str(len(effects)), technique="offline-observation",
                                target=action["target"], effect=action["effects"][0],
                                status="achieved", satisfied_effects=action["effects"],
                                evidence={"source": "inert test producer"}, preconditions=[],
                                source="offline-fixture", timestamp=""))
        return json.dumps({"ok": True, "verdict": "achieved"})

    model._build_current_engagement_state = observe
    model.mythic_client._refresh_graph_facts_if_stale = refresh
    model.mythic_client.execute_capability = effect
    model._rebuild_graph()
    chat = driver.DriverChat(model)
    config = {"mode": "supervised", "autonomous_solve": True, "policy_mode": "hybrid"}

    def request(request_id, prompt="", card=None, decision="accept", response=""):
        value = build_chat_request(prompt, channel_id=1, operation_id=1,
                                   request_id=request_id, config=config)
        if card is not None:
            value.InputResponse = driver.input_response(decision, card, response=response)
        return value

    def cards():
        return [item["metadata"]["input_requested"] for item in chat.emissions
                if (item.get("metadata") or {}).get("special_type") == "input_requested"]

    async def episode():
        first = request(1, objective)
        session.bind_channel_thread_id(first, model)
        try:
            await chat.chat(first)
            chat._used = True
            assert model._request_contract.lane == RequestLane.SUPERVISED_WORKFLOW
            assert model._request_contract.stop_condition.kind == StopConditionKind.OBJECTIVE_PROVED
            assert len(cards()) == 1 and effects == []
            original_card = cards()[0]
            episode_id = model._policy_episode_id
            await chat.chat(request(2, card=original_card,
                                    decision="respond" if first_decision == "respond-stop" else first_decision,
                                    response=("stop" if first_decision == "respond-stop" else
                                              steer if first_decision == "respond" else "")))
            if first_decision == "respond-stop":
                assert effects == [] and len(cards()) == 1
                assert model._stop_requested is True
                assert model._controller_hitl_pending is None
                await chat.chat(request(3, card=original_card))
                assert effects == [] and len(cards()) == 1
                return
            assert len(cards()) == 2
            assert model._controller_hitl_objective == objective
            assert model._policy_episode_id == episode_id
            replacement = cards()[1]
            pending_args = replacement["data"]["actions"][0]["arguments"]
            assert pending_args["action"]["target"] == "second"
            assert len(effects) == (1 if first_decision == "accept" else 0)
            if first_decision == "respond":
                assert policy_inputs[-1]["operator_steering"] == steer
                assert policy_inputs[-1]["objective"] == objective
            # The old card cannot approve the changed action or erase its pending card.
            before = len(effects)
            await chat.chat(request(3, card=original_card))
            assert len(effects) == before and model._controller_hitl_pending is not None
            await chat.chat(request(4, card=replacement))
            assert len(effects) == before + 1, model.controller_runtime_telemetry()
            assert effects[-1]["args"] == pending_args
            assert [item["target"] for item in effects] == (
                ["first", "second"] if first_decision == "accept" else ["second"]
            )
            await chat.chat(request(5, card=replacement))
            assert len(effects) == before + 1
            assert model.controller_runtime_telemetry()["objective_recognized"] is False
            assert state.objective == objective
        finally:
            await session.drop_channel_session(first)

    await episode()
