"""A Supervisor turn that finishes through respond_to_user keeps its tool history for later turns.

Recorded evidence: ``fixtures/model_input_boundaries/reasoning_tool_finish_round_trips.json`` holds live attempt 1's
raw gpt-5.6-sol / gpt-6.1-sol Responses output and its two recorded rejection envelopes (see the fixture's
provenance and substitutions). Each case replays it through the real HeadlessSageChat handler, Model, graph,
checkpointer, finish tool, parent reducers, sanitizer, installed ChatOpenAI/OpenAI SDK and native renderer
(``tests.openai_responses_harness``). Stand-ins, labelled: the provider HTTP transport and response envelopes, the
callback rows (the recorded tool output, renamed), and the replies to steps the recording does not cover.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.load import load
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import tests.openai_responses_harness as harness
from ai.langgraph import model as model_module
from sage_chat.session import load_channel_session_record
from tests.openai_responses_harness import (
    CHANNEL,
    CYBER_RESPONSES,
    OPERATION,
    ProviderWire,
    install_wire,
    request,
    route_config,
    text_item,
    texts,
)
from tests.test_reasoning_tool_recovery import (  # noqa: F401  (_isolation is this module's autouse fixture too)
    RECALL_PROMPT,
    REFUSALS,
    SEED_PROMPT,
    SOL_A,
    SOL_B,
    TOOL_PROMPT,
    _isolation,
    _process,
    _restarted,
    sol,
)

FIXTURES = Path(__file__).parent / "fixtures/model_input_boundaries"
RECORDED = json.loads((FIXTURES / "reasoning_tool_finish_round_trips.json").read_text())
STEPS = RECORDED["steps"]
BATTERY = {row["id"]: row for row in json.loads((FIXTURES / "responses_round_trips.json").read_text())["rows"]}
BANNER = "📊 **Final Report**"
LEDGER_VIEW = "[Sage engagement ledger"
ACK = getattr(model_module, "_FINISH_ACKNOWLEDGEMENT", "<no acknowledgement>")


class RecordedWire(ProviderWire):
    """Serves recorded outputs in request order, and the recorded A2/B2 rejection envelopes for those settings."""

    def __init__(self, script):
        super().__init__(script)

    def handle(self, item: httpx.Request) -> httpx.Response:
        body = json.loads(item.content)
        rejected = None
        if (item.url.path.endswith("/chat/completions") and body.get("tools")
                and body.get("reasoning_effort") not in (None, "none")):
            rejected = STEPS["A2"]["rejection"]
        elif item.url.path.endswith("/responses") and (body.get("reasoning") or {}).get("effort") == "none":
            rejected = STEPS["B2"]["rejection"]
        if rejected is not None:
            assert item.url.path == rejected["request_path"]
            self.requests.append((item.url.path, body))
            return httpx.Response(rejected["status"], json=rejected["body"])
        return super().handle(item)


def _outputs(*steps: str) -> list:
    return [output for step in steps for output in STEPS[step]["outputs"]]


def _recorded_rows(step: str) -> list | None:
    results = list(STEPS[step]["tool_results"].values())
    if not results:
        return None
    (result,) = results
    rows = json.loads(result)
    assert json.dumps(rows) == result  # the stand-in returns exactly the recorded output text
    return rows


async def _drive(process, monkeypatch, steps: list[tuple[str, str, dict]]) -> list[dict]:
    """Send each (recorded step, prompt, config); list_callbacks returns that step's recorded output."""
    out = []
    for index, (step, prompt, config) in enumerate(steps, start=1):
        rows = _recorded_rows(step) if step in STEPS else None
        if rows is not None:
            monkeypatch.setattr(harness, "CALLBACK_ROWS", rows)
        mark = len(process.chat.emissions)
        await process.chat.chat(request(prompt, index, config))
        record = await load_channel_session_record(OPERATION, CHANNEL)
        terminal = [e for e in process.chat.emissions[mark:] if e in process.chat.terminal_emissions]
        assert len(terminal) == 1, process.chat.emissions[mark:]
        out.append({"terminal": terminal[0], "texts": texts(process.chat)[mark:],
                    "emissions": list(process.chat.emissions[mark:]),
                    "thread": str((record or {}).get("thread_id") or "")})
    return out


def _typed_calls(body: dict) -> list[dict]:
    """Every function call in a Responses input with its immediately following result and preceding reasoning."""
    items = body["input"]
    found = []
    for index, item in enumerate(items):
        if item.get("type") != "function_call":
            continue
        following = items[index + 1] if index + 1 < len(items) else {}
        preceding = items[index - 1] if index else {}
        found.append({
            "call_id": item["call_id"], "name": item["name"], "arguments": json.loads(item["arguments"]),
            "output": (following.get("output") if following.get("type") == "function_call_output"
                       and following.get("call_id") == item["call_id"] else None),
            "reasoning": ((preceding.get("id"), preceding.get("encrypted_content"))
                          if preceding.get("type") == "reasoning" else None),
        })
    return found


def _expected_calls(step: str, *, reasoning: bool = True) -> list[dict]:
    """The calls a recorded step produced, each with the result it carried, as a later request must replay them."""
    entry = STEPS[step]
    expected = []
    for output in entry["outputs"]:
        thought = next((item for item in output if item["type"] == "reasoning"), None)
        for item in output:
            if item["type"] == "function_call":
                expected.append({
                    "call_id": item["call_id"], "name": item["name"], "arguments": json.loads(item["arguments"]),
                    "output": entry["tool_results"].get(item["call_id"], ACK),
                    "reasoning": ((thought["id"], thought["encrypted_content"]) if thought and reasoning else None),
                })
    return expected


def _final_response(step: str) -> str:
    (call,) = [item for item in STEPS[step]["outputs"][-1] if item.get("name") == "respond_to_user"]
    return json.loads(call["arguments"])["final_response"]


def _assistant_texts(body: dict) -> list[str]:
    return [part.get("text", "") for item in body["input"]
            if item.get("type") == "message" and item.get("role") == "assistant"
            for part in (item["content"] if isinstance(item["content"], list) else [{"text": item["content"]}])]


def _no_duplicates(body: dict) -> None:
    calls = [item["call_id"] for item in body["input"] if item.get("type") == "function_call"]
    results = [item["call_id"] for item in body["input"] if item.get("type") == "function_call_output"]
    assert len(calls) == len(set(calls)) and sorted(calls) == sorted(results)
    assert sum(1 for text in harness.input_texts(body) if text.startswith(LEDGER_VIEW)) <= 1


def _native_messages(step: dict, answer: str) -> dict[str, list[str]]:
    """Native messages carrying ``answer``, by response key (a streamed text and its terminal share one message)."""
    found: dict[str, list[str]] = {}
    for emission in step["emissions"]:
        content = str(emission.get("content") or "")
        if answer in content:
            found.setdefault(str(emission.get("response_key")), []).append(content)
    return found


def _one_final(step: dict, answer: str) -> None:
    """Exactly one native final, equal to the renderer's banner and the complete answer; no opaque reasoning."""
    found = _native_messages(step, answer)
    assert len(found) == 1, step["emissions"]
    (contents,) = found.values()
    assert {content.strip() for content in contents} == {f"{BANNER}\n{answer}"}
    joined = "\n".join(step["texts"])
    assert "opaque-recorded-substitute" not in joined and "rs_" not in joined


# --------------------------------------------------------------------------------------------------
# Recorded A and B sequences through the real consumer
# --------------------------------------------------------------------------------------------------

def test_recorded_finish_turns_keep_their_calls_results_and_reasoning_across_an_api_correction(monkeypatch):
    wire = RecordedWire(_outputs("A1", "A3", "A4"))
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        return process, await _drive(process, monkeypatch, [
            ("A1", SEED_PROMPT, sol(SOL_A)),
            ("A2", TOOL_PROMPT, sol(SOL_A, api="chat_completions")),
            ("A3", TOOL_PROMPT, sol(SOL_A)),
            ("A4", RECALL_PROMPT, sol(SOL_A)),
        ])

    process, (a1, a2, a3, a4) = asyncio.run(scenario())
    assert [s["terminal"]["kind"] for s in (a1, a2, a3, a4)] == ["complete", "error", "complete", "complete"]
    assert a1["thread"] == a2["thread"] == a3["thread"] == a4["thread"]
    assert not any(refusal in text for s in (a1, a2, a3, a4) for text in s["texts"] for refusal in REFUSALS)
    responses = wire.responses()
    assert len(responses) == 5 and process.list_callbacks_calls == 2

    # Within A1 the read's result was consumed (unchanged behavior).
    assert _typed_calls(responses[1]) == _expected_calls("A1")[:1]
    # A3's first request replays A1's read and finish pairs, with A1's same-model reasoning, then A1's answer.
    assert _typed_calls(responses[2]) == _expected_calls("A1")
    assert _final_response("A1") in _assistant_texts(responses[2])
    # A4 replays both earlier turns' pairs in order (A3's read had no reasoning item; its finish did).
    assert _typed_calls(responses[4]) == _expected_calls("A1") + _expected_calls("A3")
    for body in responses[2:]:
        _no_duplicates(body)
    _one_final(a1, _final_response("A1"))
    _one_final(a3, _final_response("A3"))
    _one_final(a4, _final_response("A4"))


def test_recorded_finish_turns_survive_an_effort_correction_and_a_direct_text_close(monkeypatch):
    wire = RecordedWire(_outputs("B1", "B3", "B4"))
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        return process, await _drive(process, monkeypatch, [
            ("B1", SEED_PROMPT, sol(SOL_B)),
            ("B2", TOOL_PROMPT, sol(SOL_B, effort="none")),
            ("B3", TOOL_PROMPT, sol(SOL_B)),
            ("B4", RECALL_PROMPT, sol(SOL_B)),
        ])

    process, (b1, b2, b3, b4) = asyncio.run(scenario())
    assert [s["terminal"]["kind"] for s in (b1, b2, b3, b4)] == ["complete", "error", "complete", "complete"]
    assert b1["thread"] == b2["thread"] == b3["thread"] == b4["thread"]
    low = [body for body in wire.responses() if body["reasoning"]["effort"] == "low"]
    assert len(low) == 5
    assert _typed_calls(low[2]) == _expected_calls("B1")
    assert _typed_calls(low[4]) == _expected_calls("B1") + _expected_calls("B3")
    for body in low[2:]:
        _no_duplicates(body)
    _one_final(b1, _final_response("B1"))
    _one_final(b3, _final_response("B3"))
    # B4 closes with the recorded direct text: the existing direct-answer path, rendered once.
    (direct,) = [part["text"] for item in STEPS["B4"]["outputs"][0] for part in item["content"]]
    assert len(_native_messages(b4, direct)) == 1


def test_a_restart_after_a_finish_turn_replays_the_same_history(monkeypatch):
    wire = RecordedWire(_outputs("A1", "A3"))
    install_wire(monkeypatch, wire)

    async def scenario():
        first = _process()
        before = await _drive(first, monkeypatch, [("A1", SEED_PROMPT, sol(SOL_A))])
        second = await _restarted(first)
        after = await _drive(second, monkeypatch, [("A3", TOOL_PROMPT, sol(SOL_A))])
        return before + after

    a1, a3 = asyncio.run(scenario())
    assert a1["thread"] == a3["thread"] and a3["terminal"]["kind"] == "complete"
    responses = wire.responses()
    assert _typed_calls(responses[2]) == _expected_calls("A1")
    _no_duplicates(responses[2])
    _one_final(a3, _final_response("A3"))


@pytest.mark.parametrize("change,kept", [
    ({"effort": "medium"}, True),      # same model, another effort: its reasoning items still apply
    ({"model": SOL_B}, False),          # another model: reasoning dropped, calls and results kept
])
def test_finish_turn_reasoning_replays_only_to_the_model_that_produced_it(monkeypatch, change, kept):
    wire = RecordedWire([*_outputs("A1"), [text_item("Your label is amber-seven.", 9)]])
    install_wire(monkeypatch, wire)
    later = sol(change.get("model", SOL_A), effort=change.get("effort", "low"))

    async def scenario():
        process = _process()
        return await _drive(process, monkeypatch, [("A1", SEED_PROMPT, sol(SOL_A)), ("next", RECALL_PROMPT, later)])

    a1, nxt = asyncio.run(scenario())
    assert a1["thread"] == nxt["thread"] and nxt["terminal"]["kind"] == "complete"
    body = wire.responses()[2]
    assert body["model"] == later["model"]
    assert _typed_calls(body) == _expected_calls("A1", reasoning=kept)
    assert any(item.get("type") == "reasoning" for item in body["input"]) is kept


def test_the_recorded_battery_finish_row_keeps_its_history_through_the_native_consumer(monkeypatch):
    """responses_round_trips.json:supervisor_tool_round_trip (gpt-6-cyber), replayed as Responses output items."""
    row = BATTERY["supervisor_tool_round_trip"]
    call, answer = load(row["call"]), load(row["answer"])
    wire = RecordedWire([list(call.content), list(answer.content), [text_item("amber-seven", 9)]])
    install_wire(monkeypatch, wire)
    monkeypatch.setattr(harness, "CALLBACK_ROWS", json.loads(row["tool_result"]))
    config = route_config(**CYBER_RESPONSES)

    async def scenario():
        process = _process()
        return await _drive(process, monkeypatch, [("battery", row["prompt"], config),
                                                   ("recall", RECALL_PROMPT, config)])

    first, recall = asyncio.run(scenario())
    assert first["terminal"]["kind"] == recall["terminal"]["kind"] == "complete"
    (respond,) = answer.tool_calls
    _one_final(first, respond["args"]["final_response"])
    body = wire.responses()[2]
    assert [(c["call_id"], c["name"], c["output"]) for c in _typed_calls(body)] == [
        (row["expected"]["call_id"], "list_callbacks", json.dumps(json.loads(row["tool_result"]))),
        (respond["id"], "respond_to_user", ACK),
    ]
    _no_duplicates(body)


# --------------------------------------------------------------------------------------------------
# The finish tool's update, given the invocation the wrapper recorded
# --------------------------------------------------------------------------------------------------

_REQUEST = "chat:1:request:1"


def _source_model(invocation: list) -> model_module.Model:
    source = model_module.Model.__new__(model_module.Model)
    source.state = {"_message_seq": 40}
    source._message_seq = 40
    source._supervisor_invocation_input = invocation
    return source


def _finish(source, messages: list, call_id: str, **arguments):
    tool = model_module._create_respond_to_user_tool(source_model=source)
    return tool.func(SimpleNamespace(state={"messages": messages}, tool_call_id=call_id), **arguments)


def test_finish_persists_only_this_invocation_then_its_acknowledgement_and_the_final():
    earlier = [HumanMessage("Earlier question.", id="h0"), AIMessage("Earlier answer.", name="Supervisor", id="a0")]
    read = AIMessage("", name="Supervisor", id="a1",
                     tool_calls=[{"name": "list_callbacks", "args": {}, "id": "call_read"}])
    result = ToolMessage("[]", name="list_callbacks", tool_call_id="call_read", id="t1")
    nudge = HumanMessage("internal", id="n1", additional_kwargs={"_hide_from_stream": True})
    finish = AIMessage("", name="Supervisor", id="a2", tool_calls=[{
        "name": "respond_to_user", "args": {"final_response": "Done.", "outcome": "completed"}, "id": "call_fin"}])
    source = _source_model(earlier)
    command = _finish(source, [*earlier, read, result, nudge, finish], "call_fin",
                      final_response="Done.", outcome="completed")
    persisted = command.update["supervisor_messages"]
    assert command.goto == "__end__" and command.update["messages"] == persisted
    assert persisted[:3] == [read, result, finish]
    ack, final = persisted[3:]
    assert isinstance(ack, ToolMessage) and ack.tool_call_id == "call_fin" and ack.name == "respond_to_user"
    assert ack.content == ACK
    assert final.content == "Done." and final.additional_kwargs["_is_final_report"] is True
    assert final.additional_kwargs["_turn_outcome"] == "completed"
    seqs = [message.additional_kwargs["_seq"] for message in persisted]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[0] >= 40
    assert command.update["_message_seq"] == source._message_seq > seqs[-1]


def test_finish_reusing_a_worker_answer_persists_the_same_shape():
    answer_text = "\n| Reported | Limit |\n|---|---|\n| Two rights enabled | Elevation unknown |\n"
    operator = HumanMessage("Read task 3.", id="h1",
                            additional_kwargs={"_operator_input": True, "_request_id": _REQUEST})
    header = AIMessage("[Mythic_Operator completed task]", name="Mythic_Operator", id="w1",
                       additional_kwargs={"_is_completion_header": True, "_request_id": _REQUEST})
    worker = AIMessage(answer_text, name="Mythic_Operator", id="w2", additional_kwargs={"_worker_final_answer": {
        "request_id": _REQUEST, "worker": "Mythic_Operator", "source_seq": 3,
        "sha256": hashlib.sha256(answer_text.encode()).hexdigest()}})
    invocation = [operator, header, worker]
    finish = AIMessage("", name="Supervisor", id="a9", tool_calls=[{
        "name": "respond_to_user", "args": {"reuse_latest_worker_answer": True}, "id": "call_reuse"}])
    command = _finish(_source_model(invocation), [*invocation, finish], "call_reuse",
                      reuse_latest_worker_answer=True)
    persisted = command.update["supervisor_messages"]
    assert persisted[0] is finish and persisted[1].tool_call_id == "call_reuse"
    assert persisted[2].content == answer_text


@pytest.mark.parametrize("arguments", [
    {"final_response": "  "},
    {"final_response": "Both.", "reuse_latest_worker_answer": True},
    {"final_response": "Done.", "outcome": "whenever"},
    {"reuse_latest_worker_answer": True},   # no eligible current-worker answer
])
def test_invalid_finish_arguments_still_return_a_review_and_persist_nothing(arguments):
    invocation = [HumanMessage("Question.", id="h2")]
    finish = AIMessage("", name="Supervisor", id="a3",
                       tool_calls=[{"name": "respond_to_user", "args": arguments, "id": "call_bad"}])
    result = _finish(_source_model(invocation), [*invocation, finish], "call_bad", **arguments)
    assert isinstance(result, ToolMessage) and result.status == "error" and result.tool_call_id == "call_bad"
