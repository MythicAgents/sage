"""Offline checks for the gateway split-choices live check's local logic (no Mythic, no provider).

The wire records, Phoenix spans and Mythic rows below are synthetic, shaped like the receiver's records, a recorded
Phoenix LLM span and Mythic's rows; they prove the pass rules accept a correct set and reject each falsifier. The
Phoenix reader runs against a real SQLite file holding one span shaped like a recorded split reply's span.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_gateway_split_choices.py"
SPEC = importlib.util.spec_from_file_location("check_gateway_split_choices", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))

GATEWAY = "https://gateway.invalid:7443"
ROUTE = {"GATEWAY_ENDPOINT": GATEWAY, "GATEWAY_MODEL": "gpt-5.6-cyber"}
URL = GATEWAY + "/chat/completions"
WHOAMI = {"command": "whoami", "parameters": {}, "callback_display_id": 2}


def _b64(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def _call(call_id: str, name: str, args: dict, index: int = 0) -> dict:
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _choice(index: int, *, text: str | None = None, calls: tuple = ()) -> dict:
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = list(calls)
    return {"index": index, "finish_reason": "tool_calls" if calls else "stop", "message": message}


def _reply(response_id: str, *choices: dict) -> dict:
    return {"id": response_id, "object": "chat.completion", "model": "gpt-5.6-cyber", "choices": list(choices),
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


SUPERVISOR_TOOLS = ("respond_to_user", "transfer_to_Mythic_Operator")
WORKER_TOOLS = (check.ISSUE_TOOL, "summarize_and_handback")


def _pair(rid: str, sent: int, messages: list, reply: dict, *, tools: tuple = (), **extra) -> list[dict]:
    body = {"model": "gpt-5.6-cyber", "messages": messages, **extra,
            "tools": [{"type": "function", "function": {"name": name}} for name in tools]}
    return [{"kind": "model_request", "record": {"time_ns": sent, "payload": {
                "request_id": rid, "url": URL, "body_base64": _b64(body)}}},
            {"kind": "model_response", "record": {"time_ns": sent + 5, "payload": {
                "request_id": rid, "status_code": 200, "outcome": "returned", "body_base64": _b64(reply)}}}]


def _tool(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "result"}


TRANSFER = _call("call_t", "transfer_to_Mythic_Operator", {"handoff_title": "Run whoami"})
ISSUE = _call("call_i", check.ISSUE_TOOL, WHOAMI)
LISTING = _call("call_l", "list_callbacks", {})
REPLIES = {
    "r1": _reply("r1", _choice(0, calls=(TRANSFER,))),
    "r2": _reply("r2", _choice(0, text="I'm using whoami."), _choice(1, calls=(ISSUE,))),
    "r3": _reply("r3", _choice(0, text="Done.")),
    "r4": _reply("r4", _choice(0, text="The step is complete.")),
    "r5": _reply("r5", _choice(0, text="I'll list them."), _choice(1, calls=(LISTING,))),
    "r6": _reply("r6", _choice(0, text="One callback.")),
}


def _records(replies: dict | None = None, *, extra: dict | None = None) -> list[dict]:
    r = replies or REPLIES
    return (_pair("q1", 110, [], r["r1"]) + _pair("q2", 120, [], r["r2"])
            + _pair("q3", 130, [_tool("call_i")], r["r3"]) + _pair("q4", 140, [_tool("call_t")], r["r4"])
            + _pair("q5", 210, [], r["r5"], **(extra or {})) + _pair("q6", 220, [_tool("call_l")], r["r6"]))


def _generation(text: str = "", calls: tuple = ()) -> dict:
    return {"text": text, "tool_calls": [{"id": c["id"], "name": c["function"]["name"],
                                          "args": json.loads(c["function"]["arguments"])} for c in calls]}


GENERATIONS = {
    "r1": {"span": 1, "generations": [_generation(calls=(TRANSFER,))]},
    "r2": {"span": 2, "generations": [_generation("I'm using whoami.", (ISSUE,))]},
    "r3": {"span": 3, "generations": [_generation("Done.")]},
    "r4": {"span": 4, "generations": [_generation("The step is complete.")]},
    "r5": {"span": 5, "generations": [_generation("I'll list them.", (LISTING,))]},
    "r6": {"span": 6, "generations": [_generation("One callback.")]},
}
CARD = {"data": {"tool_name": check.ISSUE_TOOL, "arguments": {**WHOAMI, "parameters": ""}}, "status": "accepted"}
ROWS = {"S": {"status": "complete", "messages": [{"id": 1, "metadata": {"input_requested": CARD}}]},
        "K": {"status": "complete", "messages": [{"id": 2, "message": "One callback.", "metadata": {}}]}}
TASKS = [{"display_id": 41, "command_name": "whoami", "params": "", "callback": {"display_id": 2},
          "completed": True, "status": "completed", "responses": [{"id": 1}]}]


def _run(**changes) -> dict:
    run = {"callback": 2, "route_summary": ROUTE, "active_seconds": 300.0,
           "steps": {"S": {"request_id": 11, "sent_ns": 100, "sends": 1, "cards_accepted": 1, "cards_rejected": 0},
                     "K": {"request_id": 12, "sent_ns": 200, "sends": 1}},
           "files_after_cleanup": {"env_restored": True, "env_local_restored": True},
           "capture_released": {"record": "wire.jsonl"}}
    run.update(changes)
    return run


def _evaluate(run=None, records=None, generations=None, rows=None, tasks=None):
    run = run or _run()
    records = records if records is not None else _records()
    wire, _unattributed = check.split.attribute(records, run["steps"])
    everything = check.split.exchanges(records)
    totals = {"provider_calls": len(everything),
              "provider_tokens": sum(check.split.usage_tokens(i) for i in everything),
              "requests_setting_n": check.requests_setting_n(records)}
    return check.evaluate(run, wire, generations if generations is not None else GENERATIONS,
                          rows if rows is not None else ROWS, tasks if tasks is not None else TASKS, totals,
                          check.request_tools(records))


def test_a_correct_run_passes_every_rule():
    result = _evaluate()

    assert result["verdict"] == "pass", json.dumps(result, indent=1)
    criteria = result["criteria"]
    assert criteria["G"]["split_responses"] == 2
    assert [entry["response"] for entry in criteria["R1"]["responses"]] == ["r2", "r5"]
    assert {entry["call"] for entry in criteria["R2"]["calls"]} == {"call_i", "call_l"}
    assert criteria["R3"]["applies"] and criteria["R3"]["pass"]
    assert len(criteria["R4"]["responses"]) == 4


def test_no_split_reply_means_the_check_did_not_run():
    replies = {**REPLIES, "r2": _reply("r2", _choice(0, text="I'm using whoami.", calls=(ISSUE,))),
               "r5": _reply("r5", _choice(0, text="I'll list them.", calls=(LISTING,)))}
    generations = {**GENERATIONS}

    result = _evaluate(records=_records(replies), generations=generations)

    assert result["verdict"] == "not_run" and "split" in result["not_run_reason"]


def test_a_stop_before_the_first_step_did_not_run():
    result = _evaluate(run=_run(steps={}, stopped={"step": "S", "reason": "phase 1 container not ready"}),
                       records=[])
    assert result["verdict"] == "not_run" and "before the first step" in result["not_run_reason"]


def test_an_unfolded_split_reply_fails_r1():
    generations = {**GENERATIONS, "r2": {"span": 2, "generations": [_generation("I'm using whoami."),
                                                                  _generation(calls=(ISSUE,))]}}
    result = _evaluate(generations=generations)
    assert result["verdict"] == "fail" and not result["criteria"]["R1"]["pass"]


@pytest.mark.parametrize("change", ["missing_span", "text_lost", "call_reordered"])
def test_a_generation_that_differs_from_the_choices_fails_r1(change):
    generations = copy.deepcopy(GENERATIONS)
    if change == "missing_span":
        del generations["r5"]
    elif change == "text_lost":
        generations["r2"]["generations"][0]["text"] = ""
    else:
        generations["r2"]["generations"][0]["tool_calls"][0]["id"] = "call_other"
    assert not _evaluate(generations=generations)["criteria"]["R1"]["pass"]


def test_a_split_call_whose_result_never_returns_fails_r2():
    records = (_pair("q1", 110, [], REPLIES["r1"]) + _pair("q2", 120, [], REPLIES["r2"])
               + _pair("q3", 130, [], REPLIES["r3"]) + _pair("q4", 140, [_tool("call_t")], REPLIES["r4"])
               + _pair("q5", 210, [], REPLIES["r5"]) + _pair("q6", 220, [_tool("call_l")], REPLIES["r6"]))
    result = _evaluate(records=records)
    assert result["verdict"] == "fail"
    assert [entry["call"] for entry in result["criteria"]["R2"]["calls"] if not entry["returned"]] == ["call_i"]


@pytest.mark.parametrize("change", ["no_card", "card_rejected", "two_tasks", "other_callback", "other_command"])
def test_a_split_issue_call_without_its_card_or_task_fails_r3(change):
    rows, tasks, run = copy.deepcopy(ROWS), copy.deepcopy(TASKS), _run()
    if change == "no_card":
        rows["S"]["messages"] = []
    elif change == "card_rejected":
        run["steps"]["S"]["cards_accepted"] = 0
        rows["S"]["messages"][0]["metadata"]["input_requested"]["status"] = "rejected"
    elif change == "two_tasks":
        tasks.append({**tasks[0], "display_id": 42})
    elif change == "other_callback":
        tasks[0]["callback"]["display_id"] = 3
    else:
        tasks[0]["command_name"] = "getprivs"
    result = _evaluate(run=run, rows=rows, tasks=tasks)
    assert result["verdict"] == "fail" and not result["criteria"]["R3"]["pass"]


def test_an_issue_call_in_one_choice_makes_r3_an_observation():
    replies = {**REPLIES, "r2": _reply("r2", _choice(0, text="I'm using whoami.", calls=(ISSUE,)))}

    result = _evaluate(records=_records(replies))

    assert result["verdict"] == "pass"
    assert result["criteria"]["R3"]["applies"] is False and "pass" not in result["criteria"]["R3"]


def test_a_request_setting_n_or_a_changed_single_choice_generation_fails_r4():
    assert not _evaluate(records=_records(extra={"n": 1}))["criteria"]["R4"]["pass"]
    generations = copy.deepcopy(GENERATIONS)
    generations["r3"]["generations"][0]["text"] = "Different."
    assert not _evaluate(generations=generations)["criteria"]["R4"]["pass"]


@pytest.mark.parametrize("change", ["files", "capture", "resent", "time"])
def test_a_limit_or_cleanup_miss_fails_l(change):
    run = _run()
    if change == "files":
        run["files_after_cleanup"]["env_local_restored"] = False
    elif change == "capture":
        run["capture"] = run.pop("capture_released")
    elif change == "resent":
        run["steps"]["K"]["sends"] = 2
    else:
        run["active_seconds"] = 31 * 60
    result = _evaluate(run=run)
    assert result["verdict"] == "fail" and not result["criteria"]["L"]["pass"]


def test_the_phoenix_reader_maps_a_response_id_to_its_generations(tmp_path):
    db = tmp_path / "phoenix.db"
    result = {"generations": [[{"text": "I'm using whoami.", "message": {"kwargs": {
                  "content": "I'm using whoami.", "tool_calls": [{"name": check.ISSUE_TOOL, "args": WHOAMI,
                                                                  "id": "call_i", "type": "tool_call"}]}}}]],
              "llm_output": {"id": "resp_split", "model_name": "gpt-5.6-cyber"}, "type": "LLMResult"}
    with sqlite3.connect(db) as connection:
        connection.execute("create table spans (id integer primary key, span_kind text, attributes text)")
        connection.execute("insert into spans values (1, 'CHAIN', '{}')")
        connection.execute("insert into spans values (2, 'LLM', ?)",
                           (json.dumps({"output": {"value": json.dumps(result)}}),))

    found = check.phoenix_generations(db, 0)

    assert found == {"resp_split": {"span": 2, "generations": [
        {"text": "I'm using whoami.", "tool_calls": [{"id": "call_i", "name": check.ISSUE_TOOL, "args": WHOAMI}]}]}}
    assert check.phoenix_generations(db, 2) == {}


def test_the_route_and_channels_carry_no_credential_and_only_the_gateway_model(tmp_path):
    route = tmp_path / "route"
    route.write_text(f"GATEWAY_ENDPOINT={GATEWAY}\nGATEWAY_MODEL=gpt-5.6-cyber\n")
    route.chmod(0o600)
    assert check.read_route(route) == ROUTE
    route.write_text(f"GATEWAY_ENDPOINT={GATEWAY}\nGATEWAY_MODEL=bedrock-claude-4-6-sonnet\n")
    with pytest.raises(SystemExit):
        check.read_route(route)
    for channel in check.CHANNELS:
        config = check.channel_config(ROUTE, channel)["config"]
        assert not set(config) & {"API_KEY", "API_ENDPOINT", "SAGE_OPENAI_API", "provider_retry_policy"}
    assert check.channel_config(ROUTE, "S")["config"]["mode"] == "supervised"
    assert check.channel_config(ROUTE, "K")["config"]["provider"] == "openai_compatible"


def test_the_capture_block_names_this_check():
    block = check.split.env_block("/Mythic/x", "/Mythic/x/fifo")
    assert block.startswith("# >>> gateway split-choices check") and "provider-split" not in block


# R2 for Sage's control calls, whose acknowledgement never reaches the provider within the turn.

def _control_records(transfer_reply: dict, worker_messages: list, finish_reply: dict) -> list[dict]:
    return (_pair("q1", 110, [], transfer_reply, tools=SUPERVISOR_TOOLS)
            + _pair("q2", 120, worker_messages, REPLIES["r2"], tools=WORKER_TOOLS)
            + _pair("q3", 130, [_tool("call_i")], REPLIES["r3"], tools=WORKER_TOOLS)
            + _pair("q4", 140, [], finish_reply, tools=SUPERVISOR_TOOLS)
            + _pair("q5", 210, [], REPLIES["r5"]) + _pair("q6", 220, [_tool("call_l")], REPLIES["r6"]))


INSTRUCTION = "Run whoami on callback 2 and report the identity."
SPLIT_TRANSFER = _call("call_t", "transfer_to_Mythic_Operator", {"handoff_title": "Run whoami",
                                                                 "handoff_instruction": INSTRUCTION})
FINAL = "Callback 2 runs as lab.user."
SPLIT_FINISH = _call("call_f", "respond_to_user", {"final_response": FINAL, "outcome": "completed"})


def _control_case(*, worker_messages=None, finish_args=None, mythic_answer=FINAL):
    finish = SPLIT_FINISH if finish_args is None else _call("call_f", "respond_to_user", finish_args)
    replies = {"r1": _reply("r1", _choice(0, text="I'll hand this to the operator."), _choice(1, calls=(SPLIT_TRANSFER,))),
               "r4": _reply("r4", _choice(0, text="Here is the result."), _choice(1, calls=(finish,)))}
    records = _control_records(replies["r1"], worker_messages if worker_messages is not None else
                               [{"role": "user", "content": INSTRUCTION}], replies["r4"])
    generations = {**GENERATIONS,
                   "r1": {"span": 1, "generations": [_generation("I'll hand this to the operator.", (SPLIT_TRANSFER,))]},
                   "r4": {"span": 4, "generations": [_generation("Here is the result.", (finish,))]}}
    rows = copy.deepcopy(ROWS)
    rows["S"]["messages"].append({"id": 3, "message": mythic_answer, "metadata": {}})
    return _evaluate(records=records, generations=generations, rows=rows)


def test_a_split_handoff_and_finish_carry_on_where_sage_sends_no_result():
    result = _control_case()

    assert result["verdict"] == "pass", json.dumps(result["criteria"]["R2"], indent=1)
    carried = {entry["name"]: entry["carried_on"] for entry in result["criteria"]["R2"]["calls"]}
    assert carried["transfer_to_Mythic_Operator"] == "the handoff's worker received it"
    assert carried["respond_to_user"] == "Mythic shows its final_response"


def test_a_handoff_whose_worker_never_ran_fails_r2():
    result = _control_case(worker_messages=[{"role": "user", "content": "something else"}])
    assert result["verdict"] == "fail"
    assert [e["name"] for e in result["criteria"]["R2"]["calls"] if not (e["returned"] or e["carried_on"])] == [
        "transfer_to_Mythic_Operator"]


def test_a_finish_whose_answer_never_reached_mythic_fails_r2():
    result = _control_case(mythic_answer="Here is the result.")
    assert result["verdict"] == "fail"
    assert [e["name"] for e in result["criteria"]["R2"]["calls"] if not (e["returned"] or e["carried_on"])] == [
        "respond_to_user"]


def test_a_reused_worker_answer_counts_only_when_it_is_not_the_replys_own_text():
    reuse = {"final_response": "", "reuse_latest_worker_answer": True, "outcome": "completed"}
    assert _control_case(finish_args=reuse, mythic_answer="Worker: callback 2 is lab.user.")["verdict"] == "pass"
    assert _control_case(finish_args=reuse, mythic_answer="Here is the result.")["verdict"] == "fail"


def test_an_ordinary_call_is_never_excused_by_a_control_reading():
    call = {"id": "call_x", "name": "list_callbacks", "args": {}}
    assert check.carried_on(call, REPLIES["r5"], [], {}, ROWS["K"]) == ""
