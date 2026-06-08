import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import access_reconciler  # noqa: E402
import engagement_state  # noqa: E402
import intent_classifier  # noqa: E402
import mythic_tools  # noqa: E402
from mythic_tools import MythicTools  # noqa: E402


def _make_tools() -> MythicTools:
    mt = MythicTools(agent_task_id="test")
    mt.client = object()
    return mt


@contextmanager
def _split_issue(output, calls=None, display_id=4242):
    """Patch the split issue path (issue_task + waitfor_for_task_output). issue_task increments
    calls['issue'] (if given) and returns a task with `display_id`; the wait returns `output`."""
    async def fake_issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        if calls is not None:
            calls["issue"] = calls.get("issue", 0) + 1
        return {"display_id": display_id}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        return output

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue_task), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", fake_waitfor):
        yield


def _foothold(host="WINTERFELL", forest="north.local"):
    return engagement_state.Foothold(
        callback_id="50",
        agent="apollo",
        host=host,
        forest=forest,
        identity="NORTH\\arya",
        integrity="high",
        alive=True,
        source="test",
        timestamp="2026-06-06T12:00:00Z",
    )


def _seed_hop(mt: MythicTools, technique: str, target: str) -> None:
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        technique,
        target,
        "achieved",
        {"source": "test", "task_id": "seed"},
        "2026-06-06T12:00:00Z",
    )
    mt._engagement_hops = state.hops


def test_flag_off_no_op_does_not_invoke_gate():
    calls = {"issue": 0}
    mt = _make_tools()
    with patch.object(mythic_tools, "ENGAGEMENT_GATE_ENABLED", False), \
        patch.object(mt, "_engagement_gate", side_effect=AssertionError("gate should not run")), \
        _split_issue("normal result", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))

    assert result == "normal result"
    assert calls["issue"] == 1


def test_gate_skip_short_circuits_existing_gpo_effect():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return []

    mt = _make_tools()
    _seed_hop(mt, "gpo-abuse", "winterfell")
    with patch.object(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True), \
        patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName winterfell",
                11,
            )
        )

    assert "skipped" in result
    assert calls["issue"] == 0


def test_gate_defer_short_circuits_missing_essos_preconditions():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="WINTERFELL", forest="north.local")]

    mt = _make_tools()
    with patch.object(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True), \
        patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "standin",
                '--object "CN=Domain,CN=System,DC=essos,DC=local" --grant NORTH\\arya',
                11,
            )
        )

    assert "deferred" in result
    assert calls["issue"] == 0


def test_gate_proceed_records_successful_hop():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return []

    def fake_gate_decision(technique, target, state):
        return engagement_state.GateDecision.PROCEED, "unit-test proceed"

    mt = _make_tools()
    with patch.object(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True), \
        patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(engagement_state, "gate_decision", fake_gate_decision), \
        _split_issue("task completed with useful output", calls, display_id=2712):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName NewGPO",
                11,
            )
        )

    assert result == "task completed with useful output"
    assert calls["issue"] == 1
    assert len(mt._engagement_hops) == 1
    hop = mt._engagement_hops[0]
    assert hop.technique == "gpo-abuse"
    assert hop.target == "newgpo"
    assert hop.status == "achieved"
    # The hop must capture the Mythic task display_id that proved the effect.
    assert hop.evidence.get("mythic_task_id") == 2712


def test_gate_failure_fails_open_and_issues_normally():
    calls = {"issue": 0}
    mt = _make_tools()
    with patch.object(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True), \
        patch.object(intent_classifier, "classify_tool_call", side_effect=RuntimeError("boom")), \
        _split_issue("normal result", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName NewGPO",
                11,
            )
        )

    assert result == "normal result"
    assert calls["issue"] == 1
