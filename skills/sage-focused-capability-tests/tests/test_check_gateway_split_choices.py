"""Offline checks for the gateway split-choices live check's local logic (no live Mythic, no provider).

Two kinds of input, labelled where used:
- Recorded: ``fixtures/gateway_split_choices_attempt1_s.json``, attempt 1's step S capture and Mythic rows, reduced
  (see its ``provenance``). It is read only as recorded input shapes; attempt 1's verdict is not re-read.
- Synthetic: the wire records, Mythic rows and tasks built below, shaped like the receiver's records and Mythic's rows.
  They prove each pass rule accepts a correct set and rejects each falsifier. Mutations of the recorded shapes are
  labelled synthetic too.
Sage's task admission and resource ledger run for real against the budget the check arms.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_gateway_split_choices.py"
SPEC = importlib.util.spec_from_file_location("check_gateway_split_choices", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
RECORDED = ROOT / "skills/sage-focused-capability-tests/tests/fixtures/gateway_split_choices_attempt1_s.json"

GATEWAY = "https://gateway.invalid:7443"
ROUTE = {"GATEWAY_ENDPOINT": GATEWAY, "GATEWAY_MODEL": "gpt-5.6-cyber"}
URL = GATEWAY + "/chat/completions"
WHOAMI = {"command": "whoami", "parameters": {}, "callback_display_id": 2}
# Sage's prepared arguments for the guarded call, in the recorded shape (attempt 1's replay and card).
PREPARED = {"command": "whoami", "parameters": "", "callback_display_id": 2, "timeout": 60, "token_id": None,
            "visibility_context": {"_sage_prepared_action": True,
                                   "_sage_prepared_binding": {"schema_digest": "d", "group": None, "file": None}}}
BOUNDARY = ("[step-boundary] this step's one guarded action was already run; "
            f"`{check.ISSUE_TOOL}` needs a new step from the Supervisor")
OUTCOME = (f"[Sage record r · step outcome · from Sage]\n[step outcome] Mythic_Operator step ended: action_executed "
           f"(this step's one guarded action was already run; `{check.ISSUE_TOOL}` needs a new step from the "
           "Supervisor).")
HALT = "🛑 **Halted: step budget exhausted.** Sage reached this request's model-step limit and stopped deliberately."


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


def _replay(text: str, *calls: dict, prepared: dict | None = None) -> dict:
    """The assistant message Sage sends back for a reply, as langchain-openai serializes it (synthetic)."""
    out = []
    for call in calls:
        args = prepared if prepared is not None and call["function"]["name"] == check.ISSUE_TOOL else None
        out.append({"id": call["id"], "type": "function", "function": {
            "name": call["function"]["name"],
            "arguments": json.dumps(args) if args is not None else call["function"]["arguments"]}})
    return {"role": "assistant", "content": text, "tool_calls": out}


def _tool(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "result"}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


SUPERVISOR_TOOLS = ("respond_to_user", "transfer_to_Mythic_Operator")
WORKER_TOOLS = (check.ISSUE_TOOL, "list_callbacks", "summarize_and_handback")
PROMPT_S, PROMPT_K = "Run whoami on callback 2.", "List the callbacks."


def _pair(rid: str, sent: int, messages: list, reply: dict, *, tools: tuple = SUPERVISOR_TOOLS, **extra) -> list:
    body = {"model": "gpt-5.6-cyber", "messages": messages, **extra,
            "tools": [{"type": "function", "function": {"name": name}} for name in tools]}
    return [{"kind": "model_request", "record": {"time_ns": sent, "payload": {
                "request_id": rid, "url": URL, "body_base64": _b64(body)}}},
            {"kind": "model_response", "record": {"time_ns": sent + 5, "payload": {
                "request_id": rid, "status_code": 200, "outcome": "returned", "body_base64": _b64(reply)}}}]


TRANSFER = _call("call_t", "transfer_to_Mythic_Operator", {"handoff_title": "Run whoami"})
ISSUE = _call("call_i", check.ISSUE_TOOL, WHOAMI)
ISSUE_AGAIN = _call("call_j", check.ISSUE_TOOL, WHOAMI)
LISTING = _call("call_l", "list_callbacks", {})
HISTORY = _call("call_h", "get_task_history_for_callback", {"callback_display_id": 2}, 0)
LOOKUP = _call("call_k", "get_ttp_guidance", {"goal": "identity"}, 1)
DELEGATED = {"role": "assistant",
             "content": "<untrusted_delegated_assignment> Run whoami </untrusted_delegated_assignment>"}
REPLIES = {
    "r1": _reply("r1", _choice(0, calls=(TRANSFER,))),
    "r2": _reply("r2", _choice(0, text="I'm using whoami."), _choice(1, calls=(ISSUE,))),
    "r3": _reply("r3", _choice(0, text="Done.")),
    "r4": _reply("r4", _choice(0, text="The step is complete.")),
    "r5": _reply("r5", _choice(0, calls=(LISTING,))),
    "r6": _reply("r6", _choice(0, text="Two lookups."), _choice(1, calls=(HISTORY, LOOKUP))),
    "r7": _reply("r7", _choice(0, text="One callback.")),
}


def _s_records(r=REPLIES, *, q3=None, q4=None) -> list:
    """Step S (synthetic): the Supervisor hands off, the worker's split issue call is replayed with Sage's prepared
    arguments and its result, the worker answers, and the Supervisor ends the step."""
    worker = [DELEGATED, _user(PROMPT_S)]
    replayed = worker + [_replay("I'm using whoami.", ISSUE, prepared=PREPARED), _tool("call_i")]
    return (_pair("q1", 110, [_user(PROMPT_S)], r["r1"])
            + _pair("q2", 120, worker, r["r2"], tools=WORKER_TOOLS)
            + _pair("q3", 130, replayed if q3 is None else q3, r["r3"], tools=WORKER_TOOLS)
            + _pair("q4", 140, [_user(PROMPT_S), _user("specialist return")] if q4 is None else q4, r["r4"]))


def _k_messages(*, replay5=None, replay6=None) -> tuple[list, list]:
    first = [_user(PROMPT_K), replay5 or _replay(".", LISTING), _tool("call_l")]
    return first, first + [replay6 or _replay("Two lookups.", HISTORY, LOOKUP), _tool("call_h"), _tool("call_k")]


def _k_records(r=REPLIES, *, replay5=None, replay6=None, extra=None) -> list:
    """Step K (synthetic): a single-choice call replayed with ".", then a split reply with two calls, replayed."""
    q6, q7 = _k_messages(replay5=replay5, replay6=replay6)
    return (_pair("q5", 210, [_user(PROMPT_K)], r["r5"], **(extra or {}))
            + _pair("q6", 220, q6, r["r6"]) + _pair("q7", 230, q7, r["r7"]))


def _records(replies=None, **k_changes) -> list:
    r = replies or REPLIES
    return _s_records(r) + _k_records(r, **k_changes)


CARD = {"data": {"tool_name": check.ISSUE_TOOL, "arguments": PREPARED}, "status": "accepted"}
ROWS = {"S": {"status": "complete", "messages": [{"id": 1, "message": "Approve?",
                                                  "metadata": {"input_requested": CARD}}]},
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


def _totals(records: list) -> dict:
    everything = check.split.exchanges(records)
    return {"provider_calls": len(everything), "provider_tokens": sum(check.split.usage_tokens(i) for i in everything),
            "requests_setting_n": check.requests_setting_n(records)}


def _evaluate(run=None, records=None, rows=None, tasks=None):
    run = run or _run()
    records = records if records is not None else _records()
    wire, _unattributed = check.split.attribute(records, run["steps"])
    return check.evaluate(run, wire, rows if rows is not None else ROWS, tasks if tasks is not None else TASKS,
                          _totals(records), check.request_tools(records))


def _failing(result: dict, rule: str) -> list:
    entries = result["criteria"][rule].get("responses") or result["criteria"][rule].get("calls") or []
    return [entry for entry in entries if not (entry.get("delivered") or entry.get("returned")
                                               or entry.get("carried_on") or entry.get("pass"))]


def test_a_correct_run_passes_every_rule():
    result = _evaluate()

    assert result["verdict"] == "pass", json.dumps(result, indent=1)
    criteria = result["criteria"]
    assert criteria["G"]["split_responses"] == 2
    assert [(e["response"], e["how"]) for e in criteria["R1"]["responses"]] == [("r2", "replayed"), ("r6", "replayed")]
    assert {entry["call"] for entry in criteria["R2"]["calls"]} == {"call_i", "call_h", "call_k"}
    assert criteria["R3"]["applies"] and criteria["R3"]["pass"]
    assert [(e["response"], e["how"]) for e in criteria["R4"]["responses"]] == [
        ("r1", "the handoff's worker received it"), ("r5", "replayed")]
    assert criteria["not_judged"]["replies"] == []


def test_no_split_reply_means_the_check_did_not_run():
    replies = {**REPLIES, "r2": _reply("r2", _choice(0, text="I'm using whoami.", calls=(ISSUE,))),
               "r6": _reply("r6", _choice(0, text="Two lookups.", calls=(HISTORY, LOOKUP)))}

    result = _evaluate(records=_records(replies))

    assert result["verdict"] == "not_run" and "split" in result["not_run_reason"]


def test_a_stop_before_the_first_step_did_not_run():
    result = _evaluate(run=_run(steps={}, stopped={"step": "S", "reason": "phase 1 container not ready"}),
                       records=[])
    assert result["verdict"] == "not_run" and "before the first step" in result["not_run_reason"]


# Synthetic mutations of a replay: each fails the rule that reads it.

@pytest.mark.parametrize("change", ["call_dropped", "call_added", "calls_reordered", "text_dropped"])
def test_a_split_reply_replayed_differently_fails_r1(change):
    replay = {"call_dropped": _replay("Two lookups.", HISTORY),
              "call_added": _replay("Two lookups.", HISTORY, LOOKUP, LISTING),
              "calls_reordered": _replay("Two lookups.", LOOKUP, HISTORY),
              "text_dropped": _replay(".", HISTORY, LOOKUP)}[change]

    result = _evaluate(records=_s_records() + _k_records(replay6=replay))

    assert result["verdict"] == "fail"
    assert [entry["response"] for entry in _failing(result, "R1")] == ["r6"]


def test_a_split_call_dropped_while_the_worker_ends_on_its_text_fails_r1_r2_and_r3():
    """The shape of a dropped call (synthetic): the worker kept only choice 0, so no request replays call_i, no tool
    ran, no card was raised, and the worker's text went back to the Supervisor."""
    records = _s_records(q3=[DELEGATED, _user(PROMPT_S), {"role": "assistant", "content": "I'm using whoami."}],
                         q4=[_user(PROMPT_S), _user("specialist return: I'm using whoami.")]) + _k_records()
    rows = {**ROWS, "S": {"status": "complete", "messages": []}}

    result = _evaluate(records=records, rows=rows, tasks=[])

    assert result["verdict"] == "fail"
    assert [entry["response"] for entry in _failing(result, "R1")] == ["r2"]
    assert [entry["call"] for entry in _failing(result, "R2")] == ["call_i"]
    assert result["criteria"]["R3"]["pass"] is False


def test_a_single_choice_replay_with_a_changed_argument_fails_r4():
    changed = _call("call_l", "list_callbacks", {"include_dead": True})

    result = _evaluate(records=_s_records() + _k_records(replay5=_replay(".", changed)))

    assert result["verdict"] == "fail"
    assert [entry["response"] for entry in _failing(result, "R4")] == ["r5"]
    assert "arguments differ" in _failing(result, "R4")[0]["problem"]


def test_a_changed_argument_on_a_prepared_guarded_call_is_left_to_r3():
    """Sage's prepared arguments differ from the reply's by design; R1 compares ids and names, R3 the task."""
    assert check.replay_problem(REPLIES["r2"], _replay("I'm using whoami.", ISSUE, prepared=PREPARED)) == ""
    unprepared = {**PREPARED, "visibility_context": {}}
    assert "arguments differ" in check.replay_problem(
        REPLIES["r2"], _replay("I'm using whoami.", ISSUE, prepared=unprepared))


def test_a_request_setting_n_fails_r4():
    assert not _evaluate(records=_records(extra={"n": 1}))["criteria"]["R4"]["pass"]


# The step boundary: a guarded call made after the step's one guarded action has run.

def _boundary_case(*, mythic=BOUNDARY, outcome=OUTCOME, next_tools=SUPERVISOR_TOOLS, reappears=False):
    """Synthetic: after the accepted issue call ran, the worker's next reply splits a second issue call; Sage removes
    it and ends the step, and the Supervisor's request carries the step outcome."""
    replies = {**REPLIES, "r3": _reply("r3", _choice(0, text="Retrying."), _choice(1, calls=(ISSUE_AGAIN,)))}
    q4 = [_user(PROMPT_S), _user(outcome)] + ([_tool("call_j")] if reappears else [])
    records = (_pair("q1", 110, [_user(PROMPT_S)], replies["r1"])
               + _pair("q2", 120, [DELEGATED, _user(PROMPT_S)], replies["r2"], tools=WORKER_TOOLS)
               + _pair("q3", 130, [DELEGATED, _user(PROMPT_S), _replay("I'm using whoami.", ISSUE, prepared=PREPARED),
                                   _tool("call_i")], replies["r3"], tools=WORKER_TOOLS)
               + _pair("q4", 140, q4, replies["r4"], tools=next_tools) + _k_records())
    rows = copy.deepcopy(ROWS)
    rows["S"]["messages"].append({"id": 3, "message": mythic, "metadata": {}})
    return _evaluate(records=records, rows=rows)


def test_a_guarded_call_refused_by_the_step_boundary_is_delivered():
    result = _boundary_case()

    assert result["verdict"] == "pass", json.dumps(result["criteria"], indent=1)
    r1 = {entry["response"]: entry["how"] for entry in result["criteria"]["R1"]["responses"]}
    assert r1["r3"] == "Sage's step boundary refused it and the Supervisor ran next"
    r2 = {entry["call"]: entry["carried_on"] for entry in result["criteria"]["R2"]["calls"]}
    assert r2["call_j"].startswith("Sage's step boundary")
    r3 = {entry["call"]: entry["outcome"] for entry in result["criteria"]["R3"]["calls"]}
    assert r3 == {"call_i": "card accepted", "call_j": "step boundary"}
    assert result["criteria"]["R3"]["steps"]["S"]["refused_by_step_boundary"] == 1


@pytest.mark.parametrize("change", ["no_mythic_message", "no_step_outcome", "next_is_a_worker", "id_reappears",
                                    "other_tool_named"])
def test_a_step_boundary_needs_both_effects_naming_the_call(change):
    kwargs = {"no_mythic_message": {"mythic": "Tool finished."},
              "no_step_outcome": {"outcome": "[Sage record r · specialist return] Retrying."},
              "next_is_a_worker": {"next_tools": WORKER_TOOLS},
              "id_reappears": {"reappears": True},
              "other_tool_named": {"mythic": BOUNDARY.replace(check.ISSUE_TOOL, "execute_capability"),
                                   "outcome": OUTCOME.replace(check.ISSUE_TOOL, "execute_capability")}}[change]

    result = _boundary_case(**kwargs)

    assert result["verdict"] == "fail"
    assert "r3" in [entry["response"] for entry in _failing(result, "R1")]


# Not judged: the step's last reply when Sage then halted at its step limit.

def test_the_last_reply_before_a_step_limit_halt_is_not_judged():
    replies = {**REPLIES, "r4": _reply("r4", _choice(0, text="Checking."), _choice(1, calls=(LISTING,)))}
    rows = copy.deepcopy(ROWS)
    rows["S"]["messages"].append({"id": 9, "message": HALT, "metadata": {}})

    result = _evaluate(records=_s_records(replies) + _k_records(), rows=rows)

    assert result["verdict"] == "pass", json.dumps(result["criteria"], indent=1)
    assert [entry["response"] for entry in result["criteria"]["not_judged"]["replies"]] == ["r4"]
    assert result["criteria"]["G"]["split_responses"] == 2
    assert "r4" not in [entry["response"] for entry in result["criteria"]["R1"]["responses"]]
    # Without the halt, the same unreplayed reply is judged and fails.
    assert _failing(_evaluate(records=_s_records(replies) + _k_records()), "R1")[0]["response"] == "r4"


def test_a_halt_does_not_excuse_a_reply_that_is_not_the_steps_last():
    rows = copy.deepcopy(ROWS)
    rows["S"]["messages"].append({"id": 9, "message": HALT, "metadata": {}})
    records = _s_records(q3=[DELEGATED, _user(PROMPT_S)]) + _k_records()

    result = _evaluate(records=records, rows=rows)

    assert [entry["response"] for entry in _failing(result, "R1")] == ["r2"]


# R3: a split issue call's card and task.

@pytest.mark.parametrize("change", ["no_card", "two_tasks", "other_callback", "other_command", "other_parameters",
                                    "card_differs_from_replay", "card_unresolved", "accepted_without_task"])
def test_a_split_issue_call_without_its_card_or_task_fails_r3(change):
    rows, tasks = copy.deepcopy(ROWS), copy.deepcopy(TASKS)
    card = rows["S"]["messages"][0]["metadata"]["input_requested"]
    if change == "no_card":
        rows["S"]["messages"] = []
    elif change == "two_tasks":
        tasks.append({**tasks[0], "display_id": 42})
    elif change == "other_callback":
        tasks[0]["callback"]["display_id"] = 3
    elif change == "other_command":
        tasks[0]["command_name"] = "getprivs"
    elif change == "other_parameters":
        tasks[0]["params"] = '{"x": 1}'
    elif change == "card_differs_from_replay":
        card["data"]["arguments"] = {**PREPARED, "command": "getprivs"}
    elif change == "card_unresolved":
        card["status"] = ""
    else:
        tasks = []

    result = _evaluate(rows=rows, tasks=tasks)

    assert result["verdict"] == "fail" and not result["criteria"]["R3"]["pass"]


def test_a_rejected_card_for_a_split_issue_call_leads_to_no_task():
    rows = copy.deepcopy(ROWS)
    rows["S"]["messages"][0]["metadata"]["input_requested"]["status"] = "rejected"
    run = _run()
    run["steps"]["S"].update(cards_accepted=0, cards_rejected=1)

    assert _evaluate(run=run, rows=rows, tasks=[])["criteria"]["R3"]["pass"] is True
    assert _evaluate(run=run, rows=rows, tasks=TASKS)["criteria"]["R3"]["pass"] is False


def test_an_issue_call_in_one_choice_makes_r3_an_observation():
    replies = {**REPLIES, "r2": _reply("r2", _choice(0, text="I'm using whoami.", calls=(ISSUE,)))}

    result = _evaluate(records=_records(replies))

    assert result["verdict"] == "pass"
    assert result["criteria"]["R3"]["applies"] is False and "pass" not in result["criteria"]["R3"]


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
    assert check.channel_config(ROUTE, "S")["config"]["max_steps"] == 16
    assert check.channel_config(ROUTE, "K")["config"]["max_steps"] == 4
    assert check.channel_config(ROUTE, "K")["config"]["provider"] == "openai_compatible"


def test_the_capture_block_names_this_check():
    block = check.split.env_block("/Mythic/x", "/Mythic/x/fifo")
    assert block.startswith("# >>> gateway split-choices check") and "provider-split" not in block


# R2 for Sage's control calls, whose acknowledgement never reaches the provider within the turn.

INSTRUCTION = "Run whoami on callback 2 and report the identity."
SPLIT_TRANSFER = _call("call_t", "transfer_to_Mythic_Operator", {"handoff_title": "Run whoami",
                                                                 "handoff_instruction": INSTRUCTION})
FINAL = "Callback 2 runs as lab.user."
SPLIT_FINISH = _call("call_f", "respond_to_user", {"final_response": FINAL, "outcome": "completed"})


def _control_case(*, worker_messages=None, finish_args=None, mythic_answer=FINAL):
    finish = SPLIT_FINISH if finish_args is None else _call("call_f", "respond_to_user", finish_args)
    replies = {**REPLIES,
               "r1": _reply("r1", _choice(0, text="I'll hand this to the operator."),
                            _choice(1, calls=(SPLIT_TRANSFER,))),
               "r4": _reply("r4", _choice(0, text="Here is the result."), _choice(1, calls=(finish,)))}
    worker = worker_messages if worker_messages is not None else [{"role": "assistant", "content": INSTRUCTION}]
    replayed = worker + [_replay("I'm using whoami.", ISSUE, prepared=PREPARED), _tool("call_i")]
    records = (_pair("q1", 110, [_user(PROMPT_S)], replies["r1"])
               + _pair("q2", 120, worker, replies["r2"], tools=WORKER_TOOLS)
               + _pair("q3", 130, replayed, replies["r3"], tools=WORKER_TOOLS)
               + _pair("q4", 140, [_user(PROMPT_S)], replies["r4"]) + _k_records())
    rows = copy.deepcopy(ROWS)
    rows["S"]["messages"].append({"id": 3, "message": mythic_answer, "metadata": {}})
    return _evaluate(records=records, rows=rows)


def test_a_split_handoff_and_finish_carry_on_where_sage_sends_no_result():
    result = _control_case()

    assert result["verdict"] == "pass", json.dumps(result["criteria"]["R2"], indent=1)
    carried = {entry["name"]: entry["carried_on"] for entry in result["criteria"]["R2"]["calls"]}
    assert carried["transfer_to_Mythic_Operator"] == "the handoff's worker received it"
    assert carried["respond_to_user"] == "Mythic shows its final_response"


def test_a_handoff_whose_worker_never_ran_fails_r2():
    result = _control_case(worker_messages=[_user("something else")])
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
    assert check.carried_on(call, REPLIES["r6"], [], {}, ROWS["K"]) == ""


# Recorded: attempt 1's step S, read as input shapes only (no verdict).

def _recorded():
    fixture = json.loads(RECORDED.read_text())
    wire, unattributed = check.split.attribute(fixture["records"], fixture["steps"])
    assert unattributed == []
    return fixture, wire["S"], fixture["rows"]["S"], check.request_tools(fixture["records"])


def test_the_readers_read_attempt_1s_recorded_step():
    _fixture, items, rows, offered = _recorded()
    split = [position for position, item in enumerate(items) if check.is_split(item)]
    assert split == [2, 3, 5, 7]

    found = {position: check.delivery(position, items, rows, offered) for position in range(len(items) - 1)}
    # Each split reply's replay is read; the issue call refused at the step boundary is recognised from the
    # Supervisor's next request and Mythic; the step's last reply, followed by the step-limit halt, is not judged.
    assert found[2]["how"] == found[3]["how"] == "replayed"
    assert found[5]["how"] == "Sage's step boundary refused it and the Supervisor ran next"
    assert check.halted_after(7, items, rows) and not any(check.halted_after(p, items, rows) for p in range(7))
    # Single-choice replies: a read call replayed with ".", the guarded call replayed with Sage's prepared arguments.
    assert found[0]["how"] == found[4]["how"] == "replayed"
    assert found[1]["how"] == found[6]["how"] == "the handoff's worker received it"

    entries, counts = check._issue_r3("S", items, rows, offered, [], 2)
    assert [(entry["outcome"], entry["pass"]) for entry in entries] == [("step boundary", True)]
    assert counts == {"issue_calls": 2, "split_issue_calls": 1, "refused_by_step_boundary": 1, "issue_cards": 1,
                      "accepted_issue_cards": 1}


def _mutate_later(items: list, position: int, change) -> list:
    """Synthetic mutation of a recorded reply's replay in every later request of the step."""
    items = copy.deepcopy(items)
    ids = {call["id"] for call in check.reply_calls(items[position]["reply"])}
    for item in items[position + 1:]:
        kept = []
        for entry in item["input"]:
            if entry.get("role") == "assistant" and ids & {c.get("id") for c in entry.get("tool_calls") or []}:
                entry = change(entry)
            elif change is _drop and entry.get("role") == "tool" and entry.get("tool_call_id") in ids:
                entry = None
            if entry is not None:
                kept.append(entry)
        item["input"] = kept
    return items


def _drop(entry):
    return {"role": "assistant", "content": entry["content"]}


@pytest.mark.parametrize("change", ["call_dropped", "calls_reordered", "text_dropped", "worker_ends_on_text"])
def test_mutations_of_a_recorded_split_replay_are_not_delivered(change):
    _fixture, items, rows, offered = _recorded()
    mutate = {"call_dropped": lambda e: {**e, "tool_calls": e["tool_calls"][:1]},
              "calls_reordered": lambda e: {**e, "tool_calls": list(reversed(e["tool_calls"]))},
              "text_dropped": lambda e: {**e, "content": "."},
              "worker_ends_on_text": _drop}[change]

    found = check.delivery(3, _mutate_later(items, 3, mutate), rows, offered)

    assert found["delivered"] is False, found


def test_a_recorded_single_choice_replay_with_a_changed_argument_is_not_delivered():
    _fixture, items, rows, offered = _recorded()

    def changed(entry):
        call = copy.deepcopy(entry["tool_calls"][0])
        call["function"]["arguments"] = json.dumps({"include_dead": True})
        return {**entry, "tool_calls": [call]}

    found = check.delivery(0, _mutate_later(items, 0, changed), rows, offered)

    assert found["delivered"] is False and "arguments differ" in found["problem"]


# Phoenix under the capture holds only measurement spans; verify still reaches a verdict and reads no span content.

MEASUREMENT = {"openinference": {"span": {"kind": "LLM"}},
               "llm": {"token_count": {"completion": 47, "total": 11831, "prompt_details": {"cache_read": 0},
                                       "prompt": 11784}}}  # recorded shape (Sage's sage.measurement span)


def test_verify_reaches_a_verdict_with_only_measurement_spans(tmp_path, monkeypatch):
    db = tmp_path / "phoenix.db"
    with sqlite3.connect(db) as connection:
        connection.execute("create table spans (id integer primary key, name text, span_kind text, attributes text, "
                           "llm_token_count_prompt integer, llm_token_count_completion integer)")
        for span in range(1, 8):
            connection.execute("insert into spans values (?, 'sage.measurement', 'LLM', ?, 11784, 47)",
                               (span, json.dumps(MEASUREMENT)))
    wire = tmp_path / "wire.jsonl"
    wire.write_text("".join(json.dumps(record) + "\n" for record in _records()))
    run = _run(capture_released={"record": str(wire)}, phoenix_start=0, phoenix_end=7, task_baseline=40)
    run["steps"]["S"].update(phoenix_after=0, phoenix_until=4)
    run["steps"]["K"].update(phoenix_after=4, phoenix_until=7)
    real = check.split._base()

    async def request_rows(_client, request_id):
        return ROWS["S" if request_id == 11 else "K"]

    async def query(_client, _query, **_variables):
        return {"task": TASKS}

    monkeypatch.setattr(check.split, "_base", lambda: SimpleNamespace(
        request_rows=request_rows, _query=query, model_spans=real.model_spans))

    result = asyncio.run(check.cmd_verify(None, run, SimpleNamespace(phoenix_db=str(db))))

    assert result["verdict"] == "pass", json.dumps(result["criteria"], indent=1)
    assert result["observations"]["llm_spans_by_step"] == {"S": 4, "K": 3}
    assert not hasattr(check, "phoenix_generations")


def test_readiness_reads_the_armed_budget(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    run = {"capture": {"host_dir": str(host)}}
    assert check.budget_state({})["armed"] is False
    assert check.budget_state(run)["valid"] is False  # no budget.json
    (host / "budget.json").write_text(json.dumps(check.split.budget_config()))
    assert check.budget_state(run)["valid"] is True
    (host / "budget.json").write_text(json.dumps(check._PROVIDER_SPLIT_BUDGET()))
    assert check.budget_state(run)["valid"] is False


# The budget the check arms, through Sage's real task admission and resource ledger (no stand-in for either).

def _admit_against(config: dict, tmp_path: Path, monkeypatch) -> list[str]:
    from ai import resource_budget, task_budget

    for name in ("SAGE_RESOURCE_EPISODE_ID", "SAGE_RESOURCE_BUDGET_PHASE", "SAGE_RESOURCE_SERVICE_PROBE_ID",
                 task_budget.TASK_LIMIT_ENV, "SAGE_OBSERVATION_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(config, sort_keys=True))
    path.chmod(0o600)
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(path))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_LEDGER", str(tmp_path / "ledger.jsonl"))
    outcomes = []
    for _attempt in range(2):
        try:
            task_budget.admit_task()
            outcomes.append("admitted")
        except resource_budget.ResourceBudgetError as error:
            outcomes.append(f"{type(error).__name__}: {error}")
    return outcomes


def test_the_provider_split_budget_cannot_admit_a_task(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("provider_split_unchanged", check._SPLIT_PATH)
    provider_split = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider_split)  # type: ignore[union-attr]

    outcomes = _admit_against(provider_split.budget_config(), tmp_path, monkeypatch)

    assert outcomes == ["ResourceBudgetError: unknown resource: payload_task_attempts"] * 2, outcomes


def test_the_budget_the_check_arms_admits_one_task_and_refuses_a_second(tmp_path, monkeypatch):
    config = check.split.budget_config()  # what the arm writes to budget.json

    outcomes = _admit_against(config, tmp_path, monkeypatch)

    assert outcomes == ["admitted", "ResourceBudgetExceeded: resource budget exceeded: payload_task_attempts"], outcomes
    assert set(config["resources"]) == {"provider_requests", "provider_tokens", "control_requests",
                                        "payload_task_attempts"}
