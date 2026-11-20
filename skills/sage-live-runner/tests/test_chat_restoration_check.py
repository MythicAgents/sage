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


def _verify(tasks, messages, run):
    async def execute(_client, query, variables=None):
        if "RestorationTasks" in query:
            return {"task": [t for t in tasks if t["display_id"] > variables["after"]]}
        if "RestorationMessages" in query:
            return {"chat_message": [m for m in messages if m["channel_id"] == variables["ch"]]}
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


def test_verify_passes_only_cases_meeting_their_rules():
    run = {"callback": 2, "task_baseline": 16, "restarts": 5, "recall_tasks": [], "cases": {
        "feature": {"channel_id": 7, "recall": {"request_id": 70, "status": "complete"}},
        "legacy": {"channel_id": 8, "recall": {"request_id": 80, "status": "complete"}},
        "accept": {"channel_id": 9, "tasks_before": 16, "tasks_after": 17, "duplicate": "refused: RuntimeError"},
        "reject": {"channel_id": 10, "tasks_before": 17, "tasks_after": 18, "duplicate": "refused: RuntimeError"},
        "crash": {"channel_id": 11, "tasks_before": 18, "tasks_after": 19, "request_status_after_start": "complete",
                  "killed_after_task": [{"display_id": 19}], "next_request": {"request_id": 110, "status": "complete"}},
    }}
    messages = [
        {"channel_id": 7, "chat_request_id": 70, "message": "You asked me to remember amber-seven."},
        {"channel_id": 8, "chat_request_id": 80, "message": runner.REFUSAL + " Start a new chat."},
        {"channel_id": 11, "chat_request_id": 110,
         "message": "Sage will not resend it. Mythic task 19 on callback 2 (`pwd`) is completed."},
    ]
    # accept made task 17; a task 18 inside the reject window; crash made task 19.
    verdicts = _verify([_pwd(17), _pwd(18), _pwd(19)], messages, run)
    assert verdicts["feature"]["pass"] is True
    assert verdicts["legacy"]["pass"] is False  # a refusal is not restored history
    assert verdicts["accept"]["pass"] is True  # later cases' tasks are outside its window
    assert verdicts["reject"]["pass"] is False  # a task appeared after a rejection
    assert verdicts["crash"]["pass"] is True
    assert verdicts["down"] == "not run" and verdicts["within_task_budget"] is True
    assert verdicts["approved_pwd_tasks"] == 3


def _card(message_id, request_id, approval_id, status, digest="d" * 64):
    return {"id": message_id, "channel_id": 12, "chat_request_id": request_id, "message": "Approve pwd",
            "metadata": {"special_type": "input_requested", "input_requested": {
                "status": status, "data": {"sage_approval_context": {
                    "approval_id": approval_id, "action_digest": digest}}}}}


def _down_run(**case):
    return {"callback": 2, "task_baseline": 16, "recall_tasks": [], "cases": {"down": {
        "channel_id": 12, "card_request": {"request_id": 120}, "tasks_before": 16, "tasks_before_answer": 16,
        "tasks_after": 17, "request_status_after_answer": "complete",
        "next_request": {"request_id": 121, "status": "complete"}, **case}}}


def _down_messages(new_digest="d" * 64, new_id="new"):
    return [
        _card(200, 120, "old", "accepted"),
        {"id": 201, "channel_id": 12, "chat_request_id": 120,
         "message": "Your decision on the earlier card reached Mythic while Sage was down and was not applied."},
        _card(202, 120, new_id, "accepted", new_digest),
        {"id": 203, "channel_id": 12, "chat_request_id": 120, "message": "pwd returned C:\\Users\\lab."},
        {"id": 204, "channel_id": 12, "chat_request_id": 121, "message": "Nothing is pending."},
    ]


def test_down_case_passes_only_on_a_reask_of_the_same_action_answered_once():
    assert _verify([_pwd(17)], _down_messages(), _down_run())["down"]["pass"] is True
    # The stored decision applied before the new card was answered.
    assert _verify([_pwd(17)], _down_messages(), _down_run(tasks_before_answer=17))["down"]["pass"] is False
    # A new card for a different action, or reusing the old approval id, is not a re-ask.
    assert _verify([_pwd(17)], _down_messages(new_digest="e" * 64), _down_run())["down"]["pass"] is False
    assert _verify([_pwd(17)], _down_messages(new_id="old"), _down_run())["down"]["pass"] is False
    # No card at all: the restart left the request without a re-ask.
    assert _verify([], _down_messages()[:2], _down_run(tasks_after=16))["down"]["pass"] is False


def test_crash_case_needs_the_request_to_end_without_operator_action():
    run = {"callback": 2, "task_baseline": 18, "recall_tasks": [], "cases": {"crash": {
        "channel_id": 11, "tasks_before": 18, "tasks_after": 19, "request_status_after_start": "streaming",
        "killed_after_task": [{"display_id": 19}], "next_request": {"request_id": 110, "status": "complete"}}}}
    messages = [{"channel_id": 11, "chat_request_id": 110,
                 "message": "Sage will not resend it. Mythic task 19 on callback 2 (`pwd`) is completed."}]
    assert _verify([_pwd(19)], messages, run)["crash"]["pass"] is False
