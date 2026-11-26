"""A tool the Supervisor runs itself shows one tool card, as a specialist's call does.

Drives the real HeadlessSageChat handler, Model, graph, capture callback, request ledger and chat emitter through
``tests.openai_responses_harness``; only the provider HTTP, the Mythic tools' backends and the Mythic transport are
stand-ins. Effect and near-match provider replies are synthetic (wiring only). Battery rows replay recorded
Supervisor calls (see ``fixtures/model_input_boundaries/supervisor_tool_cards.json`` provenance).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.load import load
from langchain_core.tools import tool

import sage_chat.service as service
from ai.langgraph import model as model_module
from ai.langgraph.model import MessageCaptureCallback
from sage_chat import session as chat_session
from tests import openai_responses_harness as harness
from tests.test_hitl_steer_current_intent import _NativeScriptedModel
from tests.test_openai_responses_tool_cards import _cc_call, _chat_completions_wire

ANSWER = "One callback: WORKSTATION-5."
FIXTURES = Path(__file__).parent / "fixtures/model_input_boundaries"


@tool
async def read_engagement_state() -> str:
    """Read the engagement ledger (offline stand-in)."""
    return json.dumps({"footholds": [{"callback": 5, "host": "WORKSTATION-5"}]})


@tool
async def submit_engagement_evidence(citations: list[str]) -> str:
    """Submit cited evidence (offline stand-in)."""
    return json.dumps({"admitted": citations})


class _Process(harness.Process):
    """The harness process, also offering the engagement tools the Supervisor gets in every mode."""

    def __init__(self, extra=(read_engagement_state, submit_engagement_evidence)):
        self.extra = list(extra)
        super().__init__()

    def sink(self):
        sink = super().sink()
        base = sink.get_tools.side_effect
        sink.get_tools.side_effect = lambda names: [*base(names), *[t for t in self.extra if t.name in names]]
        return sink


def _wire(monkeypatch, api: str, steps: list) -> None:
    """Steps are lists of (name, args, call_id) calls, or answer text; a callable step runs first (side effect)."""
    if api == "responses":
        items = [step if callable(step) else [harness.text_item(step)] if isinstance(step, str)
                 else [harness.call_item(*call) for call in step] for step in steps]
        harness.install_wire(monkeypatch, harness.ProviderWire(items))
        return
    messages = []
    for step in steps:
        if isinstance(step, str):
            messages.append({"role": "assistant", "content": step})
        else:
            message = _cc_call(*step[0])
            message["tool_calls"] += [_cc_call(*call)["tool_calls"][0] for call in step[1:]]
            messages.append(message)
    _chat_completions_wire(monkeypatch, messages)


def _turn(monkeypatch, steps, *, api="chat_completions", mode="conversation", channel=300, process=None):
    _wire(monkeypatch, api, steps)
    config = (harness.route_config(**harness.CYBER_RESPONSES, mode=mode) if api == "responses"
              else harness.route_config(model="test-model", mode=mode))
    process = process or _Process()

    async def run():
        try:
            await process.chat.chat(harness.request("Use list_callbacks once.", 1, config, channel=channel))
        finally:
            await process.close()
            await chat_session.remove_session(str(channel))

    asyncio.run(run())
    service._CHANNEL_TURN_LOCKS.clear()
    return process


def _tool_rows(process, name=None) -> list[tuple[str, dict]]:
    return [(e["response_key"], e["metadata"]["tool_use"]) for e in process.chat.emissions
            if isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "tool_use"
            and name in (None, e["metadata"]["tool_use"]["tool_name"])]


def _assert_closed_request(process) -> None:
    texts = harness.texts(process.chat)
    assert not any("reconciliation failed" in text for text in texts), texts
    assert [e["kind"] for e in process.chat.terminal_emissions] == ["complete"]


def _assert_one_card(process, name, call_id, *, status="completed", preview="WORKSTATION-5") -> list[dict]:
    rows = _tool_rows(process, name)
    assert len({key for key, _ in rows}) == 1, rows
    assert [card["status"] for _, card in rows] == ["started", status]
    assert all(card["tool_call_id"] == call_id and "delegation_id" not in card for _, card in rows)
    assert preview in rows[-1][1]["result_preview"]
    return [card for _, card in rows]


@pytest.mark.parametrize("mode", ["conversation", "supervised"])
@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_a_supervisor_direct_listing_shows_one_card_and_no_supervisor_text(monkeypatch, api, mode):
    channel = 310 + (2 if api == "responses" else 0) + (1 if mode == "supervised" else 0)
    process = _turn(monkeypatch, [[("list_callbacks", {}, "call_a")], ANSWER], api=api, mode=mode, channel=channel)

    assert process.list_callbacks_calls == 1
    _assert_one_card(process, "list_callbacks", "call_a")
    assert _tool_rows(process) == _tool_rows(process, "list_callbacks")
    non_card = [(e["response_key"], str(e.get("content") or "")) for e in process.chat.emissions
                if (e.get("metadata") or {}).get("special_type") != "tool_use"]
    assert len({key for key, text in non_card if ANSWER in text}) == 1  # one report row (streamed, then completed)
    assert not any("call_a" in text or "Tool Request" in text for _, text in non_card)
    _assert_closed_request(process)


@pytest.mark.parametrize("mode", ["conversation", "supervised", "auto"])
def test_the_engagement_tools_show_their_cards_in_every_mode(monkeypatch, mode):
    steps = [[("read_engagement_state", {}, "call_r")], [("submit_engagement_evidence", {"citations": ["42"]}, "call_s")],
             ANSWER]
    if mode == "auto":  # auto reaches the Supervisor graph only on the deterministic controller's rollback setting
        monkeypatch.setenv("SAGE_AUTONOMOUS_CONTROLLER", "0")
    process = _turn(monkeypatch, steps, mode=mode, channel=320 + ["conversation", "supervised", "auto"].index(mode))

    _assert_one_card(process, "read_engagement_state", "call_r")
    (_, submitted) = _assert_one_card(process, "submit_engagement_evidence", "call_s", preview="42")
    assert "42" in submitted["arguments"]
    _assert_closed_request(process)


def test_a_raising_supervisor_tool_ends_its_card_as_error(monkeypatch):
    @tool("read_engagement_state")
    async def broken() -> str:
        """Read the engagement ledger (offline stand-in that fails)."""
        raise RuntimeError("ledger unavailable")

    process = _turn(monkeypatch, [[("read_engagement_state", {}, "call_r")], ANSWER], channel=330,
                    process=_Process(extra=[broken]))
    _assert_one_card(process, "read_engagement_state", "call_r", status="error", preview="ledger unavailable")


# --- calls that never run open no card ----------------------------------------------------------------------------

def _no_supervisor_card(process) -> None:
    assert process.list_callbacks_calls == 0
    assert _tool_rows(process) == []
    assert not any("reconciliation failed" in text for text in harness.texts(process.chat))


@pytest.mark.parametrize("steps", [
    [[("respond_to_user", {"final_response": ANSWER}, "call_c")]],                                       # control alone
    [[("list_callbacks", {}, "call_a"), ("respond_to_user", {"final_response": ANSWER}, "call_c")], ANSWER],  # batch
    [[("list_callbacks", {}, "call_d"), ("list_callbacks", {}, "call_d")], ANSWER],                    # invalid envelope
], ids=["control_alone", "ordinary_with_control", "duplicate_call_ids"])
def test_rejected_supervisor_forms_open_no_card(monkeypatch, steps):
    _no_supervisor_card(_turn(monkeypatch, steps, channel=340 + len(steps[0]) + len(steps)))


def test_a_listing_turn_authority_refuses_opens_no_card(monkeypatch):
    original = model_module.Model._request_contract_block_reason
    monkeypatch.setattr(model_module.Model, "_request_contract_block_reason",  # the refusal decision, not the path
                        lambda self, name, args=None: "offline refusal" if name == "list_callbacks"
                        else original(self, name, args))
    process = _turn(monkeypatch, [[("list_callbacks", {}, "call_a")], ANSWER], channel=350)
    _no_supervisor_card(process)
    _assert_closed_request(process)


def test_a_listing_the_stop_check_answers_opens_no_card(monkeypatch):
    process = _Process()

    def stop_then_call(_body):
        model = process.models[-1]
        model._stop_requested, model._stop_reason = True, "operator"
        return [harness.call_item("list_callbacks", {}, "call_a")]

    _no_supervisor_card(_turn(monkeypatch, [stop_then_call], api="responses", channel=351, process=process))


def test_an_mcp_tool_on_the_supervisor_opens_no_card():
    emitted = []

    async def record(**card):
        emitted.append(card)

    callback = MessageCaptureCallback("Supervisor", tool_use_func=record, tool_source_func=lambda name: "mcp")
    asyncio.run(callback.on_tool_start({"name": "graph_query"}, "{}", run_id=None, inputs={}, tool_call_id="call_m"))
    assert emitted == []


# --- battery: recorded Supervisor-direct calls through the real Supervisor graph ---------------------------------

BATTERY = json.loads((FIXTURES / "supervisor_tool_cards.json").read_text())


def _battery_rows() -> list[dict]:
    rows = []
    for row in BATTERY["rows"]:
        if "from" in row:
            source, row_id = row["from"].split("#")
            (recorded,) = [r for r in json.loads((FIXTURES / source).read_text())["rows"] if r["id"] == row_id]
            row = {**row, "call": recorded["call"], "tool_result": recorded["tool_result"]}
        rows.append(row)
    return rows


def test_battery_provenance_names_its_substitution():
    assert BATTERY["provenance"]["kind"].startswith("recorded model output")
    assert "rc10" in BATTERY["provenance"]["substitutions"]
    assert {row["id"] for row in BATTERY["rows"]} == {
        "responses_gpt6_cyber", "gateway_chat_completions", "anthropic_tool_use", "bedrock_tool_use"}


@pytest.mark.parametrize("row", _battery_rows(), ids=lambda row: row["id"])
def test_recorded_supervisor_calls_each_yield_one_completed_card(monkeypatch, row):
    call = load(row["call"])
    (expected,) = call.tool_calls
    fake = _NativeScriptedModel(messages=iter([call, *[ANSWER] * 3]))
    monkeypatch.setattr(model_module.Model, "_get_base_chat_model", lambda self, *a, **k: fake)
    monkeypatch.setattr(harness, "CALLBACK_ROWS", json.loads(row["tool_result"]))
    process = _turn(monkeypatch, [], channel=360 + [r["id"] for r in BATTERY["rows"]].index(row["id"]))

    assert process.list_callbacks_calls == 1
    _assert_one_card(process, "list_callbacks", expected["id"], preview=row["expected"]["preview_contains"])
    _assert_closed_request(process)
