"""Tests for the issue_task circuit breaker + empty-parameter normalization in MythicTools.

Root cause covered: on 2026-06-01 a single Sage run hit 2.47M tokens because argument-less
Apollo commands (rev2self/whoami) failed for every empty-parameter encoding and the model
re-issued them in an unbounded loop. These tests pin the two fixes:
  1. empty params ({}, '', '""', None) normalize to ""
  2. a command that fails twice is short-circuited on the 3rd identical attempt
Run: cd Payload_Type/sage && python3 -m pytest tests/test_circuit_breaker.py -q
"""
import asyncio
import sys
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


def test_is_task_failure_output_signatures():
    assert _is_task_failure_output("[-] failed to parse arguments for rev2self")
    assert _is_task_failure_output("Error issuing command: Failed to create task")
    assert _is_task_failure_output("don't match any parameters")
    assert not _is_task_failure_output("Reverted identity to NORTH\\samwell.tarly")
    assert not _is_task_failure_output("")


def test_empty_params_normalized_to_empty_string():
    seen = {}

    async def fake_issue(mythic, command_name, parameters, callback_display_id, timeout):
        seen["parameters"] = parameters
        return "ok"

    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "issue_task_and_waitfor_task_output", fake_issue):
        for empty in ({}, "", "{}", '""', None):
            seen.clear()
            asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", empty, 11))
            assert seen["parameters"] == "", f"{empty!r} should normalize to ''"


def test_circuit_breaker_trips_after_two_failures():
    calls = {"n": 0}

    async def always_fails(mythic, command_name, parameters, callback_display_id, timeout):
        calls["n"] += 1
        return "[-] failed to parse arguments for rev2self: rev2self takes no command line arguments."

    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "issue_task_and_waitfor_task_output", always_fails):
        r1 = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", {}, 11))
        r2 = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))   # same after normalization
        r3 = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", '""', 11))  # same after normalization
    assert "failed to parse" in r1 and "failed to parse" in r2
    assert r3.startswith("STOP"), "3rd identical attempt must be short-circuited"
    assert calls["n"] == 2, "Mythic must be called only twice; the 3rd is blocked locally"


def test_success_resets_failure_counter():
    state = {"fail": True}

    async def flaky(mythic, command_name, parameters, callback_display_id, timeout):
        return "Error: Failed to create task" if state["fail"] else "Reverted identity"

    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "issue_task_and_waitfor_task_output", flaky):
        asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))  # fail -> count 1
        state["fail"] = False
        asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))  # success -> reset
        state["fail"] = True
        # After reset we must get two more real attempts before the breaker trips again.
        r = asyncio.run(mt.issue_task_and_waitfor_task_output("rev2self", "", 11))  # fail -> count 1
    assert not r.startswith("STOP"), "counter should have reset on the intervening success"


def test_different_commands_tracked_independently():
    async def always_fails(mythic, command_name, parameters, callback_display_id, timeout):
        return "[-] failed to parse arguments"

    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "issue_task_and_waitfor_task_output", always_fails):
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
