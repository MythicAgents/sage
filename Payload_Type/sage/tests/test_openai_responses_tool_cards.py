"""Tool cards are the same on Chat Completions and Responses for the same calling agent.

Regression check for the OpenAI Responses line (plan revision 2): which agent runs a tool is the model's
routing choice, and native chat renders one card for the tool call either way, a specialist's or one the Supervisor
runs itself. This holds identically on both APIs, and this test keeps the rendering from diverging between APIs.

Drives the real HeadlessSageChat handler, Model, graph, base-model factory and installed ChatOpenAI through
``tests.openai_responses_harness``; provider replies are synthetic and scripted per API (wiring only).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import langchain.chat_models
import pytest

import sage_chat.service as service
from ai.langgraph import model as model_module
from sage_chat import session as chat_session
from tests import openai_responses_harness as harness

BRIEF = {"handoff_title": "List callbacks", "handoff_instruction": "Use list_callbacks once and report.",
         "expected_result": "The callback list."}
ANSWER = "One callback: WORKSTATION-5."


def _chat_completions_wire(monkeypatch, script):
    def handle(request):
        body = json.loads(request.content)
        message = script.pop(0) if script else {"role": "assistant", "content": ANSWER}
        return httpx.Response(200, json={
            "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    original = langchain.chat_models.init_chat_model
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: original(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))))


def _cc_call(name, args, call_id):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _turn(monkeypatch, api: str, routed: bool, channel: int) -> dict:
    if api == "responses":
        if routed:
            steps = [[harness.call_item("transfer_to_Mythic_Operator", BRIEF, "call_t")],
                     [harness.call_item("list_callbacks", {}, "call_a")],
                     [harness.text_item(ANSWER)], [harness.text_item(ANSWER)]]
        else:
            steps = [[harness.call_item("list_callbacks", {}, "call_a")], [harness.text_item(ANSWER)]]
        harness.install_wire(monkeypatch, harness.ProviderWire(steps))
        config = harness.route_config(**harness.CYBER_RESPONSES)
    else:
        if routed:
            script = [_cc_call("transfer_to_Mythic_Operator", BRIEF, "call_t"), _cc_call("list_callbacks", {}, "call_a"),
                      {"role": "assistant", "content": ANSWER}, {"role": "assistant", "content": ANSWER}]
        else:
            script = [_cc_call("list_callbacks", {}, "call_a"), {"role": "assistant", "content": ANSWER}]
        _chat_completions_wire(monkeypatch, script)
        config = harness.route_config(model="test-model")

    async def run():
        process = harness.Process()
        try:
            await process.chat.chat(harness.request("Use list_callbacks once.", 1, config, channel=channel))
            return process
        finally:
            await process.close()
            await chat_session.remove_session(str(channel))

    process = asyncio.run(run())
    service._CHANNEL_TURN_LOCKS.clear()
    cards = {e["response_key"] for e in process.chat.emissions
             if "list_callbacks" in json.dumps(e.get("metadata") or {}, default=str)}
    terminal = [e for e in process.chat.emissions if e in process.chat.terminal_emissions]
    return {"calls": process.list_callbacks_calls, "card_rows": len(cards),
            "terminal": [e["kind"] for e in terminal]}


@pytest.mark.parametrize(("routed", "expected_cards"), [(False, 1), (True, 2)])
def test_tool_card_rows_follow_the_calling_agent_identically_on_both_apis(monkeypatch, routed, expected_cards):
    base = 80 + (10 if routed else 0)
    chat_completions = _turn(monkeypatch, "chat_completions", routed, base + 1)
    responses = _turn(monkeypatch, "responses", routed, base + 2)
    assert chat_completions == responses
    assert responses == {"calls": 1, "card_rows": expected_cards, "terminal": ["complete"]}
