"""The restoration runner's pass rules read Mythic rows exactly as written (offline stand-in rows)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "chat_restoration_check.py"
spec = importlib.util.spec_from_file_location("chat_restoration_check_under_test", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def _verify(tasks, messages, run, statuses=None):
    async def execute(_client, query, variables=None):
        if "RestorationTasks" in query:
            return {"task": [t for t in tasks if t["display_id"] > variables["after"]]}
        if "RestorationMessages" in query:
            return {"chat_message": [m for m in messages if m["channel_id"] == variables["ch"]]}
        if "RestorationRequests" in query:
            return {"chat_request": [{"id": i, "status": s} for i, s in (statuses or {}).items()
                                     if i in variables["ids"]]}
        raise AssertionError(query)

    original = runner.nc.mythic.execute_custom_query
    runner.nc.mythic.execute_custom_query = execute
    try:
        return asyncio.run(runner.cmd_verify(object(), run, None))
    finally:
        runner.nc.mythic.execute_custom_query = original


def _pwd(display_id, callback=2):
    return {"display_id": display_id, "command_name": "pwd", "status": "completed",
            "completed": True, "callback": {"display_id": callback}}


def _approval(message_id, request_id, channel, status, *, callback=2, command="pwd"):
    return {"id": message_id, "channel_id": channel, "chat_request_id": request_id, "message": "Approve pwd",
            "metadata": {"special_type": "input_requested", "input_requested": {
                "status": status, "data": {
                    "actions": [{"tool_name": "issue_task_and_waitfor_task_output",
                                 "arguments": {"command": command, "callback_display_id": callback}}],
                    "sage_approval_context": {"approval_id": "a" * 32, "action_digest": "d" * 64}}}}}


def _control(message_id, request_id, channel, status, choice="continue"):
    response = {"action": "select", "choice": {"id": choice}} if status == "selected" else (
        {"action": "cancel"} if status == "cancelled" else {})
    return {"id": message_id, "channel_id": channel, "chat_request_id": request_id, "message": "Continue?",
            "metadata": {"special_type": "input_requested", "input_requested": {
                "status": status, "response": response,
                "data": {"sage_continuation": {"source_approval_id": "a" * 32}}}}}


def _tool(message_id, request_id, channel, status, call_id="tooluse_offline"):
    return {"id": message_id, "channel_id": channel, "chat_request_id": request_id, "message": "Tool",
            "metadata": {"special_type": "tool_use", "tool_use": {"status": status, "tool_call_id": call_id}}}


def _task_card(message_id, request_id, channel, task, callback=2, status="completed"):
    """Sage's card for the Mythic task itself; its call id names the task (`mythic-task:<cb>:<task>`)."""
    return _tool(message_id, request_id, channel, status, call_id=f"mythic-task:{callback}:{task}")


_ACCEPTED = {"action": "accept", "response": {"error": None, "status": "success"}}
_REJECTED = {"action": "reject", "response": {"error": None, "status": "success"}}


def _case(channel, request_id, answer, terminal, **extra):
    return {"channel_id": channel, "card_request": {"request_id": request_id}, "tasks_before": answer,
            "tasks_at_answer": answer, "tasks_at_terminal": terminal, "tasks_after": terminal,
            "duplicate": "refused: RuntimeError",
            "next_request": {"request_id": request_id + 1, "status": "complete"}, **extra}


def test_recall_rules_and_budgets():
    run = {"callback": 2, "task_baseline": 16, "restarts": 6, "recall_tasks": [], "cases": {
        "feature": {"channel_id": 7, "recall": {"request_id": 70, "status": "complete"}},
        "legacy": {"channel_id": 8, "recall": {"request_id": 80, "status": "complete"}},
    }}
    messages = [
        {"channel_id": 7, "chat_request_id": 70, "message": "You asked me to remember amber-seven."},
        {"channel_id": 8, "chat_request_id": 80, "message": runner.REFUSAL + " Start a new chat."},
    ]
    verdicts = _verify([], messages, run)
    assert verdicts["feature"]["pass"] is True
    assert verdicts["legacy"]["pass"] is False  # a refusal is not restored history
    assert verdicts["queued_accept"] == "not run" and verdicts["within_restart_budget"] is True


def test_queued_cases_pass_in_either_delivery_order_when_the_decision_applies_once():
    """Rule 2 (plan revision 2): either delivery order counts; the order seen is an observation."""
    run = {"callback": 2, "task_baseline": 16, "restarts": 4, "recall_tasks": [], "cases": {
        "feature": {"channel_id": 9, "mode_override": "supervised"},
        "queued_accept": _case(9, 90, 16, 17, decision=_ACCEPTED, mythic_saw_running=True),
        "queued_reject": _case(10, 100, 17, 17, decision=_REJECTED, mythic_saw_running=True),
    }}
    messages = [
        _approval(1, 90, 9, "accepted"), _tool(2, 90, 9, "completed"), _control(3, 90, 9, "cancelled"),
        _task_card(4, 90, 9, 17),
        _approval(5, 100, 10, "rejected"), _tool(6, 100, 10, "cancelled"), _control(7, 100, 10, "cancelled"),
    ]
    statuses = {90: "complete", 91: "complete", 100: "complete", 101: "complete"}
    verdicts = _verify([_pwd(17)], messages, run, statuses)
    assert verdicts["queued_accept"]["pass"] is True and verdicts["queued_accept"]["seam_exact_proposal"]
    assert verdicts["queued_accept"]["delivery_order"] == "startup_offer_first"
    assert verdicts["queued_accept"]["mode_source"] == "/mode supervised"
    assert verdicts["queued_reject"]["pass"] is True
    # No Continue/Stop card: the original decision arrived first. Judged by the same rule.
    original_first = [m for m in messages if m["id"] not in (3, 7)]
    first = _verify([_pwd(17)], original_first, run, statuses)
    assert first["queued_accept"]["pass"] is True and first["queued_accept"]["delivery_order"] == "original_first"
    # The restored resume ending in a lifecycle error fails, as live requests 11 and 22 did.
    failed = messages + [{"id": 8, "channel_id": 9, "chat_request_id": 90,
                          "message": runner.LIFECYCLE_ERROR + " before terminal response."}]
    assert _verify([_pwd(17)], failed, run, {**statuses, 90: "error"})["queued_accept"]["pass"] is False
    # A queued decision discarded as stale (live cards 418/430) fails.
    stale = messages + [{"id": 9, "channel_id": 10, "chat_request_id": 100,
                         "message": "That approval request is no longer active."}]
    assert _verify([_pwd(17)], stale, run, statuses)["queued_reject"]["pass"] is False
    # A proposal for another command is not the requested action.
    wrong = [_approval(1, 90, 9, "accepted", command="whoami")] + messages[1:]
    assert _verify([_pwd(17)], wrong, run, statuses)["queued_accept"]["pass"] is False
    # A task the request's own rows never name is not that case's effect.
    unnamed = [m for m in messages if m["id"] != 4]
    assert _verify([_pwd(17)], unnamed, run, statuses)["queued_accept"]["pass"] is False
    # Not run only when Mythic refused the decision or never showed Sage running.
    refused = {**run, "cases": {**run["cases"], "queued_accept": _case(
        9, 90, 16, 16, decision="RuntimeError: refused", mythic_saw_running=True)}}
    assert _verify([], messages, refused, statuses)["queued_accept"]["pass"] == "not run"
    stopped = {**run, "cases": {**run["cases"], "queued_accept": _case(
        9, 90, 16, 17, decision=_ACCEPTED, mythic_saw_running=False)}}
    assert _verify([_pwd(17)], messages, stopped, statuses)["queued_accept"]["pass"] == "not run"
    # Mode source: queued_accept must be the `/mode supervised` feature chat.
    configured = {**run, "cases": {**run["cases"], "feature": {"channel_id": 99, "mode_override": "supervised"}}}
    moved = _verify([_pwd(17)], messages, configured, statuses)["queued_accept"]
    assert moved["mode_source"] == "configured supervised" and moved["pass"] is False


def test_down_cases_pass_only_when_continue_applies_the_saved_decision_once():
    run = {"callback": 2, "task_baseline": 16, "restarts": 5, "recall_tasks": [], "cases": {
        "down_accept": _case(12, 120, 16, 17, tasks_before_answer=16),
        "down_reject": _case(13, 130, 17, 17, tasks_before_answer=17),
    }}
    messages = [
        _approval(1, 120, 12, "accepted"), _tool(2, 120, 12, "completed"), _control(3, 120, 12, "selected"),
        _task_card(7, 120, 12, 17),
        _approval(4, 130, 13, "rejected"), _tool(5, 130, 13, "cancelled"), _control(6, 130, 13, "selected"),
    ]
    statuses = {120: "complete", 121: "complete", 130: "complete", 131: "complete"}
    verdicts = _verify([_pwd(17)], messages, run, statuses)
    assert verdicts["down_accept"]["pass"] is True and verdicts["down_reject"]["pass"] is True
    assert verdicts["down_accept"]["mode_source"] == "configured supervised"
    # The saved decision applied before Continue was chosen.
    early = {**run, "cases": {**run["cases"], "down_accept": _case(12, 120, 16, 17, tasks_before_answer=17)}}
    assert _verify([_pwd(17)], messages, early, statuses)["down_accept"]["pass"] is False
    # No Continue/Stop card after the start: the condition arose and Sage did not offer it.
    assert _verify([_pwd(17)], [m for m in messages if m["id"] != 3], run, statuses)["down_accept"]["pass"] is False
    # A Continue/Stop card left pending at the end fails.
    pending = [m if m["id"] != 6 else _control(6, 130, 13, "pending") for m in messages]
    assert _verify([_pwd(17)], pending, run, statuses)["down_reject"]["pass"] is False
    # A rejection that ran the tool fails.
    ran = [m if m["id"] != 5 else _tool(5, 130, 13, "completed") for m in messages]
    assert _verify([_pwd(17)], ran, run, statuses)["down_reject"]["pass"] is False


def test_crash_case_needs_the_request_to_end_without_operator_action():
    def run(status):
        return {"callback": 2, "task_baseline": 18, "recall_tasks": [], "cases": {"crash": {
            "channel_id": 11, "card_request": {"request_id": 109}, "tasks_before": 18, "tasks_after": 19,
            "tasks_at_answer": 18, "tasks_at_terminal": 19,
            "request_status_after_start": status, "killed_after_task": [{"display_id": 19}],
            "next_request": {"request_id": 110, "status": "complete"}}}}
    messages = [{"channel_id": 11, "chat_request_id": 110,
                 "message": "Sage will not resend it. Mythic task 19 on callback 2 (`pwd`) is completed."}]
    assert _verify([_pwd(19)], messages, run("complete"), {110: "complete"})["crash"]["pass"] is True
    assert _verify([_pwd(19)], messages, run("streaming"), {110: "complete"})["crash"]["pass"] is False


# Attempt 1 (2026-11-20, run 20261120T1405Z-recovery-live-attempt1.json and its Mythic rows): labelled
# recorded row shapes. Ids, statuses, card kinds and task call ids are as Mythic stored them; text bodies
# and task output are omitted. Attempt 1's own verdict is unchanged; these test the revised rules.
_A1_TASKS = [_pwd(19), _pwd(20), _pwd(21), _pwd(22)]  # recorded: tasks 19-22, pwd on callback 2
_A1_STATUSES = {31: "complete", 32: "complete", 33: "complete", 34: "complete", 35: "complete",
                36: "complete", 37: "complete", 38: "complete"}


def _a1_down_messages():
    return [
        _approval(530, 35, 19, "accepted"), _tool(527, 35, 19, "completed", "tooluse_3owrWiPPIoUcBOtXXpx7pG"),
        _control(537, 35, 19, "selected"), _task_card(541, 35, 19, 21),
        _approval(536, 36, 20, "rejected"), _tool(534, 36, 20, "cancelled"), _control(538, 36, 20, "selected"),
    ]


def test_recorded_sequential_down_cases_each_count_only_their_own_task():
    """Attempt 1 judged down_reject failed: the shared window (20, 21] counted down_accept's task 21."""
    run = {"callback": 2, "task_baseline": 20, "restarts": 6, "recall_tasks": [], "cases": {
        "down_accept": {**_case(19, 35, 20, 21, tasks_before_answer=20), "next_request": {"request_id": 37}},
        "down_reject": {**_case(20, 36, 21, 21, tasks_before_answer=21), "next_request": {"request_id": 38}},
    }}
    verdicts = _verify([_pwd(21)], _a1_down_messages(), run, _A1_STATUSES)
    assert verdicts["down_accept"]["task_ids"] == [21] and verdicts["down_accept"]["pass"] is True
    assert verdicts["down_reject"]["task_ids"] == [] and verdicts["down_reject"]["pass"] is True
    assert verdicts["unattributed_pwd_tasks"] == []


def _a1_queued_accept(**extra):
    # Recorded: request 31 in the `/mode supervised` feature chat 17, card 499 accepted, the guarded
    # call's card 497 cancelled, no Continue/Stop card (the original decision arrived first), 0 tasks.
    case = _case(17, 31, 20, 20, decision={"action": "accept", "chat_request_id": 31,
                                            "input_request_message_id": 499,
                                            "response": {"error": None, "message_id": 499, "request_id": 31,
                                                         "status": "success"}},
                 mythic_saw_running=True)
    case["next_request"] = {"request_id": 33}
    return {**case, **extra}


def test_recorded_queued_accept_delivered_first_with_no_task_fails_not_unrun():
    run = {"callback": 2, "task_baseline": 20, "restarts": 4, "recall_tasks": [], "cases": {
        "feature": {"channel_id": 17, "mode_override": "supervised"}, "queued_accept": _a1_queued_accept()}}
    messages = [_approval(499, 31, 17, "accepted"), _tool(497, 31, 17, "cancelled", "tooluse_niSahvcAqhG6zfn1Mea9ch")]
    verdict = _verify([], messages, run, _A1_STATUSES)["queued_accept"]
    assert verdict["delivery_order"] == "original_first" and verdict["pwd_tasks"] == 0
    assert verdict["pass"] is False


def test_recorded_queued_accept_with_startup_first_is_judged_by_the_same_rule():
    """Synthetic mutation of the recorded rows: on-start's Continue/Stop card came first and was retired."""
    run = {"callback": 2, "task_baseline": 20, "restarts": 4, "recall_tasks": [], "cases": {
        "feature": {"channel_id": 17, "mode_override": "supervised"},
        "queued_accept": _a1_queued_accept(tasks_at_terminal=21, tasks_after=21)}}
    offered = [_approval(499, 31, 17, "accepted"), _tool(497, 31, 17, "completed", "tooluse_niSahvcAqhG6zfn1Mea9ch"),
               _control(500, 31, 17, "cancelled")]
    ran = _verify([_pwd(21)], offered + [_task_card(510, 31, 17, 21)], run, _A1_STATUSES)["queued_accept"]
    assert ran["delivery_order"] == "startup_offer_first" and ran["pass"] is True
    idle = {**run, "cases": {**run["cases"], "queued_accept": _a1_queued_accept()}}
    nothing = _verify([], offered, idle, _A1_STATUSES)["queued_accept"]
    assert nothing["delivery_order"] == "startup_offer_first" and nothing["pass"] is False


def test_an_unattributed_task_fails_the_run():
    """Task 22 lies in no case's window (recorded ids, synthetic windows): the run fails."""
    run = {"callback": 2, "task_baseline": 20, "restarts": 6, "recall_tasks": [], "cases": {
        "feature": {"channel_id": 17, "mode_override": "supervised",
                    "recall": {"request_id": 30, "status": "complete"}},
        "legacy": {"channel_id": 16, "recall": {"request_id": 29, "status": "complete"}},
        "down_accept": {**_case(19, 35, 20, 21, tasks_before_answer=20), "next_request": {"request_id": 37}},
        "down_reject": {**_case(20, 36, 21, 21, tasks_before_answer=21), "next_request": {"request_id": 38}},
    }}
    messages = _a1_down_messages() + [
        {"channel_id": 17, "chat_request_id": 30, "message": "You asked me to remember amber-seven."},
        {"channel_id": 16, "chat_request_id": 29, "message": "You asked me to remember amber-seven."}]
    verdicts = _verify([_pwd(21), _pwd(22)], messages, run, _A1_STATUSES)
    assert verdicts["down_accept"]["pass"] is True and verdicts["down_reject"]["pass"] is True
    assert verdicts["unattributed_pwd_tasks"] == [22] and verdicts["pass"] is False
    # Overlapping windows: one task in two cases is not attributed to either.
    overlap = {**run, "cases": {**run["cases"], "down_reject": {
        **_case(20, 36, 20, 21, tasks_before_answer=20), "next_request": {"request_id": 38}}}}
    both = _verify([_pwd(21)], messages, overlap, _A1_STATUSES)
    assert both["unattributed_pwd_tasks"] == [21] and both["down_accept"]["pass"] is False
