"""Tests for the issue_task circuit breaker + empty-parameter normalization in MythicTools.

Root cause covered: on 2026-06-01 a single Sage run hit 2.47M tokens because argument-less
Apollo commands (rev2self/whoami) failed for every empty-parameter encoding and the model
re-issued them in an unbounded loop. These tests pin the two fixes:
  1. empty params ({}, '', '""', None) normalize to ""
  2. a command that fails twice is short-circuited on the 3rd identical attempt
Run: cd Payload_Type/sage && python3 -m pytest tests/test_circuit_breaker.py -q

The issue path was split (issue_task + waitfor_for_task_output) so a hop can capture its Mythic
task display_id; these tests mock that split via `_split_issue`.
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Import the module directly (mirrors test_ttp_library's path handling).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402
from mythic_tools import MythicTools, _is_task_failure_output  # noqa: E402


def _make_tools() -> MythicTools:
    mt = MythicTools(agent_task_id="test-task")
    mt.client = object()  # truthy so the client-None guard passes
    return mt


@contextmanager
def _split_issue(output, on_issue=None):
    """Patch the split issue path. `on_issue(parameters)` fires when a task is 'issued' (counters/capture);
    `output` is the task output (a str, or a zero-arg callable returning a str for stateful tests)."""
    async def fake_issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        if on_issue is not None:
            on_issue(parameters)
        return {"display_id": 4242}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        return output() if callable(output) else output

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue_task), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", fake_waitfor):
        yield


def test_is_task_failure_output_signatures():
    assert _is_task_failure_output("[-] failed to parse arguments for rev2self")
    assert _is_task_failure_output("Error issuing command: Failed to create task")
    assert _is_task_failure_output("don't match any parameters")
    assert not _is_task_failure_output("Reverted identity to NORTH\\samwell.tarly")
    assert not _is_task_failure_output("")


def test_empty_params_normalized_to_empty_string():
    seen = {}
    with _split_issue("ok", on_issue=lambda p: seen.__setitem__("parameters", p)):
        for empty in ({}, "", "{}", '""', None):
            seen.clear()
            # Fresh tools per case: these are independent normalization checks, not a real repeated-action
            # loop, so they must not accumulate the unproductive-success loop-guard streak.
            mt = _make_tools()
            asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", empty, 11))
            assert seen["parameters"] == "", f"{empty!r} should normalize to ''"


def test_circuit_breaker_trips_after_two_failures():
    calls = {"n": 0}
    fail = "[-] failed to parse arguments for rev2self: rev2self takes no command line arguments."
    mt = _make_tools()
    with _split_issue(fail, on_issue=lambda p: calls.__setitem__("n", calls["n"] + 1)):
        r1 = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", {}, 11))
        r2 = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))   # same after normalization
        r3 = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", '""', 11))  # same after normalization
    assert "failed to parse" in r1 and "failed to parse" in r2
    assert r3.startswith("STOP"), "3rd identical attempt must be short-circuited"
    assert calls["n"] == 2, "Mythic must be called only twice; the 3rd is blocked locally"


def test_success_resets_failure_counter():
    state = {"fail": True}
    mt = _make_tools()
    with _split_issue(lambda: "Error: Failed to create task" if state["fail"] else "Reverted identity"):
        asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))  # fail -> count 1
        state["fail"] = False
        asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))  # success -> reset
        state["fail"] = True
        # After reset we must get two more real attempts before the breaker trips again.
        r = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))  # fail -> count 1
    assert not r.startswith("STOP"), "counter should have reset on the intervening success"


def test_different_commands_tracked_independently():
    mt = _make_tools()
    with _split_issue("[-] failed to parse arguments"):
        asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))
        asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))
        # whoami is a different key — must NOT be blocked by rev2self's failures.
        r = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))
    assert not r.startswith("STOP")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all circuit-breaker tests passed")
