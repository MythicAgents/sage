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


def _tool(message_id, request_id, channel, status):
    return {"id": message_id, "channel_id": channel, "chat_request_id": request_id, "message": "Tool",
            "metadata": {"special_type": "tool_use", "tool_use": {"status": status}}}


def _case(channel, request_id, before, after, **extra):
    return {"channel_id": channel, "card_request": {"request_id": request_id}, "tasks_before": before,
            "tasks_after": after, "duplicate": "refused: RuntimeError",
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


def test_queued_cases_pass_only_when_the_race_arose_and_the_original_won_once():
    run = {"callback": 2, "task_baseline": 16, "restarts": 4, "recall_tasks": [], "cases": {
        "queued_accept": _case(9, 90, 16, 17),
        "queued_reject": _case(10, 100, 17, 17),
    }}
    messages = [
        _approval(1, 90, 9, "accepted"), _tool(2, 90, 9, "completed"), _control(3, 90, 9, "cancelled"),
        {"id": 4, "channel_id": 9, "chat_request_id": 90, "message": "pwd returned C:\\Users\\lab."},
        _approval(5, 100, 10, "rejected"), _tool(6, 100, 10, "cancelled"), _control(7, 100, 10, "cancelled"),
    ]
    statuses = {90: "complete", 91: "complete", 100: "complete", 101: "complete"}
    verdicts = _verify([_pwd(17)], messages, run, statuses)
    assert verdicts["queued_accept"]["pass"] is True and verdicts["queued_accept"]["seam_exact_proposal"]
    assert verdicts["queued_reject"]["pass"] is True
    # No Continue/Stop card: on-start never saw the decision first, so the race did not arise.
    unelicited = [m for m in messages if m["id"] not in (3, 7)]
    assert _verify([_pwd(17)], unelicited, run, statuses)["queued_accept"]["pass"] == "not run"
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


def test_down_cases_pass_only_when_continue_applies_the_saved_decision_once():
    run = {"callback": 2, "task_baseline": 16, "restarts": 5, "recall_tasks": [], "cases": {
        "down_accept": _case(12, 120, 16, 17, tasks_before_answer=16),
        "down_reject": _case(13, 130, 17, 17, tasks_before_answer=17),
    }}
    messages = [
        _approval(1, 120, 12, "accepted"), _tool(2, 120, 12, "completed"), _control(3, 120, 12, "selected"),
        _approval(4, 130, 13, "rejected"), _tool(5, 130, 13, "cancelled"), _control(6, 130, 13, "selected"),
    ]
    statuses = {120: "complete", 121: "complete", 130: "complete", 131: "complete"}
    verdicts = _verify([_pwd(17)], messages, run, statuses)
    assert verdicts["down_accept"]["pass"] is True and verdicts["down_reject"]["pass"] is True
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
            "request_status_after_start": status, "killed_after_task": [{"display_id": 19}],
            "next_request": {"request_id": 110, "status": "complete"}}}}
    messages = [{"channel_id": 11, "chat_request_id": 110,
                 "message": "Sage will not resend it. Mythic task 19 on callback 2 (`pwd`) is completed."}]
    assert _verify([_pwd(19)], messages, run("complete"), {110: "complete"})["crash"]["pass"] is True
    assert _verify([_pwd(19)], messages, run("streaming"), {110: "complete"})["crash"]["pass"] is False
