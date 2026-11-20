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
        "crash": {"channel_id": 11, "tasks_before": 18, "tasks_after": 19, "next_request": {"request_id": 110}},
    }}
    messages = [
        {"channel_id": 7, "chat_request_id": 70, "message": "You asked me to remember amber-seven."},
        {"channel_id": 8, "chat_request_id": 80, "message": runner.REFUSAL + " Start a new chat."},
        {"channel_id": 11, "chat_request_id": 110, "message": "Sage will not resend it. Task 18 completed."},
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
