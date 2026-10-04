"""Native task completion uses exact Mythic metadata, not output classification."""
import asyncio
import inspect
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools


def _make_tools():
    mt = mythic_tools.MythicTools(agent_task_id="test-task")
    mt.client = object()
    return mt


@contextmanager
def _split_issue(output):
    async def wait(**kwargs):
        return output() if callable(output) else output
    with patch.object(mythic_tools.mythic, "issue_task", AsyncMock(return_value={"display_id": 4242})), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", wait):
        yield


def _row(**updates):
    return {"display_id": 4242, "callback": {"display_id": 11},
            "command_name": "whoami", "completed": True, "status": "completed", **updates}


@pytest.mark.parametrize("completed,status", [(True, "completed"), (False, "processing"), (True, "error")])
def test_exact_metadata_preserves_terminal_and_status_separately(completed, status):
    mt = _make_tools()
    query = AsyncMock(return_value={"task": [_row(completed=completed, status=status)]})
    with patch.object(mythic_tools.mythic, "execute_custom_query", query):
        result = asyncio.run(mt._supervised_task_state(4242, 11, "whoami"))
    assert result == {"task_completed": completed, "task_status": status}
    assert query.call_args.kwargs["variables"] == {"id": 4242}


@pytest.mark.parametrize("rows", [[], [_row(), _row()], [_row(display_id=4243)],
    [_row(callback={"display_id": 12})], [_row(command_name="shell")],
    [_row(completed="true")], [_row(completed=1)], [None]])
def test_missing_or_mismatched_metadata_is_unknown(rows):
    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "execute_custom_query", AsyncMock(return_value={"task": rows})):
        assert asyncio.run(mt._supervised_task_state(4242, 11, "whoami")) == {
            "task_completed": None, "task_status": ""}


def test_query_failure_is_unknown():
    mt = _make_tools()
    with patch.object(mythic_tools.mythic, "execute_custom_query", AsyncMock(side_effect=TimeoutError)):
        assert asyncio.run(mt._supervised_task_state(4242, 11, "whoami"))["task_completed"] is None


def test_partial_successful_output_does_not_prove_completion_and_binding_is_captured():
    mt = _make_tools()
    mt._request_contract = SimpleNamespace(request_id="request-one", digest="contract-one")
    mt._active_approval_claim = {"request_id": "request-one", "request_contract_digest": "contract-one",
                                 "approval_id": "approval-one"}
    events = []
    mt.set_execution_observer(lambda event: events.append(event.copy()))

    def later_output():
        mt._request_contract = SimpleNamespace(request_id="request-two", digest="contract-two")
        mt._active_approval_claim = None
        return "Primary identity: EXAMPLE\\user"

    with _split_issue(later_output), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""), \
         patch.object(mt, "_record_contract_task_terminal"), \
         patch.object(mt, "_supervised_task_state", AsyncMock(return_value={
             "task_completed": False, "task_status": "processing"})):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))
    assert "Primary identity" in result
    assert len(events) == 2
    assert events[0]["status"] == "started"
    # A successful-looking partial output cannot close a task Mythic still reports as processing.
    assert events[1]["status"] == "error"
    assert events[1]["task_completed"] is False
    assert events[1]["task_status"] == "processing"
    assert events[1]["terminal_status"] == "processing"
    for event in events:
        assert event["request_id"] == "request-one"
        assert event["request_contract_digest"] == "contract-one"
        assert event["approval_id"] == "approval-one"


def test_shell_service_config_emits_truthful_completed_event():
    output = ("[SC] QueryServiceConfig SUCCESS\r\n"
              "SERVICE_NAME: MSSQL$SQLEXPRESS\r\n"
              "ERROR_CONTROL : 1 NORMAL\r\n"
              "SERVICE_START_NAME : example\\service\r\n")
    mt = _make_tools()
    events = []
    mt.set_execution_observer(lambda event: events.append(event.copy()))
    with _split_issue(output), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("shell", "sc.exe qc service", 11))
    assert result.startswith(output)
    assert [event["status"] for event in events] == ["started", "completed"]
    assert events[1]["terminal_status"] == "completed"
    assert events[1]["result_preview"].startswith(output)

    events.clear()
    with _split_issue(output + "Error: service query failed\r\n"), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""):
        asyncio.run(mt.issue_task_and_waitfor_task_output("shell", "sc.exe qc service", 11))
    assert [event["status"] for event in events] == ["started", "error"]


def test_unapproved_path_does_not_add_metadata_query():
    mt = _make_tools()
    with _split_issue("ok"), patch.object(mt, "_supervised_task_state", AsyncMock()) as query:
        asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))
    query.assert_not_called()


def _registered_issue_tool(mt):
    return mt.get_tools(["issue_task_and_waitfor_task_output"])[0]


async def _invoke_registered(mt, command="whoami", callback_display_id=11, timeout=1):
    return await _registered_issue_tool(mt).ainvoke({
        "command": command,
        "parameters": "",
        "callback_display_id": callback_display_id,
        "timeout": timeout,
    })


def test_registered_tool_adds_issuance_identity_without_changing_raw_json_body():
    mt = _make_tools()
    body = '{"value": "actual task output"}'
    with _split_issue(body), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""):
        raw = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11, timeout=1))
        presented = asyncio.run(_invoke_registered(mt))

    assert raw == body
    assert json.loads(raw) == {"value": "actual task output"}
    assert presented == (
        "[Mythic task issuance identity] task_display_id=4242 "
        "callback_display_id=11 requested_command=whoami\n\n" + body
    )


def test_registered_tool_preserves_original_schema_and_guarded_metadata():
    mt = _make_tools()
    raw = mythic_tools.StructuredTool.from_function(
        coroutine=mt.issue_task_and_waitfor_task_output,
        name="issue_task_and_waitfor_task_output",
        description=mt.issue_task_and_waitfor_task_output.__doc__,
    )
    presented = _registered_issue_tool(mt)

    assert presented.args == raw.args
    assert inspect.signature(presented.coroutine) == inspect.signature(
        mt.issue_task_and_waitfor_task_output
    )
    assert getattr(presented.coroutine, "_tool_safety") == mythic_tools.TOOL_SAFETY_GUARDED


def test_registered_tool_receipts_are_isolated_across_concurrent_calls():
    mt = _make_tools()
    first_waiting = asyncio.Event()
    second_issued = asyncio.Event()

    async def issue(command, _parameters, callback_id, **_kwargs):
        if command == "tasklist":
            return {"display_id": 9001}
        second_issued.set()
        return {"display_id": 9002}

    async def wait(*, task_display_id, **_kwargs):
        if task_display_id == 9001:
            first_waiting.set()
            await second_issued.wait()
        return f"body-for-{task_display_id}"

    async def scenario():
        with patch.object(mt, "_issue_mythic_task", issue), \
             patch.object(mythic_tools.mythic, "waitfor_for_task_output", wait), \
             patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
             patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""):
            first = asyncio.create_task(_invoke_registered(mt, "tasklist", 31))
            await first_waiting.wait()
            second = asyncio.create_task(_invoke_registered(mt, "netstat", 32))
            return await asyncio.gather(first, second)

    first, second = asyncio.run(scenario())
    assert "task_display_id=9001 callback_display_id=31 requested_command=tasklist" in first
    assert first.endswith("body-for-9001")
    assert "task_display_id=9002 callback_display_id=32 requested_command=netstat" in second
    assert second.endswith("body-for-9002")


def test_registered_tool_sequential_receipts_do_not_reuse_after_preissue_failure():
    mt = _make_tools()
    outcomes = iter((
        {"display_id": 9001},
        {"display_id": 9002},
        RuntimeError("rejected before issue"),
    ))

    async def issue(*_args, **_kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def wait(*, task_display_id, **_kwargs):
        return f"body-for-{task_display_id}"

    with patch.object(mt, "_issue_mythic_task", issue), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", wait), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""):
        first = asyncio.run(_invoke_registered(mt, "tasklist", 31))
        second = asyncio.run(_invoke_registered(mt, "netstat", 32))
        failed = asyncio.run(_invoke_registered(mt))

    assert "task_display_id=9001 callback_display_id=31 requested_command=tasklist" in first
    assert first.endswith("body-for-9001")
    assert "task_display_id=9002 callback_display_id=32 requested_command=netstat" in second
    assert second.endswith("body-for-9002")
    assert "9001" not in second
    assert "issuance identity" not in failed
    assert "9001" not in failed and "9002" not in failed
    assert "rejected before issue" in failed


def test_registered_timeout_identifies_current_issued_task_without_success_claim():
    mt = _make_tools()
    with patch.object(mt, "_issue_mythic_task", AsyncMock(return_value={"display_id": 9003})), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", AsyncMock(side_effect=asyncio.TimeoutError)), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""):
        result = asyncio.run(_invoke_registered(mt, "tasklist", 31))

    assert "task_display_id=9003 callback_display_id=31 requested_command=tasklist" in result
    assert "DEFERRED: task 9003" in result
    assert "task_display_id=4242" not in result
    assert "[Mythic task issuance identity]" in result
    assert "[Mythic task success" not in result
