"""Tests for the failed-task no-progress guard in MythicTools.

Run: cd Payload_Type/sage && python3 -m pytest tests/test_no_progress_guard.py -q
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402
from mythic_tools import (  # noqa: E402
    MythicTools,
    _TASK_FAILURE_SIGNATURES,
    _READ_FAILURE_SIGNATURES,
    _is_task_failure_output,
    _is_failed_read_output,
)


def _make_tools() -> MythicTools:
    mt = MythicTools(agent_task_id="test-task")
    mt.client = object()  # truthy so the client-None guard passes
    return mt


# Broad runtime-failure signatures belong to the READ-GUARD scope only.
_BROAD = (
    "exception has been thrown by the target of an invocation",
    "unexpected error",
    "traceback (most recent call last)",
)


def test_broad_signatures_are_read_scope_only_not_breaker_scope():
    """Cato finding (2026-06-01): broad phrases must NOT feed the issue_task circuit breaker (a
    success output quoting them would false-trip it). They live in the read-guard scope only."""
    for sig in _BROAD:
        assert sig in _READ_FAILURE_SIGNATURES
        assert _is_failed_read_output(sig.upper())          # read-guard detects (case-insensitive)
        assert sig not in _TASK_FAILURE_SIGNATURES          # breaker scope stays narrow
        assert _is_task_failure_output(sig.upper()) is False  # breaker does NOT trip on them


def test_failed_task_output_is_clamped_on_second_fetch():
    async def failed_output(mythic, task_display_id):
        return [{"id": 1, "responses": "Traceback (most recent call last): boom"}]

    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "get_all_task_output_by_id", failed_output):
        r1 = asyncio.run(mt.get_all_task_output_by_task_id(301))
        r2 = asyncio.run(mt.get_all_task_output_by_task_id(301))

    assert "Traceback (most recent call last)" in r1
    assert "STOP RE-READING" in r2
    assert "301" in r2


def test_success_output_is_never_clamped():
    async def success_output(mythic, task_display_id):
        return [{"id": 1, "responses": "whoami => DA\\nuser"}]

    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "get_all_task_output_by_id", success_output):
        results = [asyncio.run(mt.get_all_task_output_by_task_id(302)) for _ in range(5)]

    for result in results:
        assert "whoami => DA" in result
        assert "STOP RE-READING" not in result
