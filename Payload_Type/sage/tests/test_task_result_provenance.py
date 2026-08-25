"""Request-bound Mythic task-result provenance (ISC-71)."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import patch

from ai.langgraph import mythic_tools
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract


def _contract(request_id: str):
    return build_request_contract(
        request_id=request_id,
        channel_id="channel-58",
        operation_id="operation-4",
        mode="conversation",
        autonomous_solve=False,
    )


def _tools(request_id: str = "chat:58:request:current") -> MythicTools:
    tools = MythicTools(agent_task_id="test")
    tools.client = object()
    tools.set_request_contract(_contract(request_id))
    return tools


def _encoded_output(task_id: int, text: str):
    return [{
        "task_id": task_id,
        "response_text": base64.b64encode(text.encode()).decode(),
    }]


def _read_output(tools: MythicTools, task_id: int, text: str = "task output"):
    async def fake_output(*, mythic, task_display_id):
        assert task_display_id == task_id
        return _encoded_output(task_id, text)

    async def not_completed(_task_id):
        return False

    with (
        patch.object(mythic_tools.mythic, "get_all_task_output_by_id", fake_output),
        patch.object(tools, "_is_task_completed", not_completed),
    ):
        return json.loads(asyncio.run(tools.get_all_task_output_by_task_id(task_id)))


def test_manual_prior_task_output_is_structured_and_marked_historical():
    tools = _tools()

    result = _read_output(tools, 24, "manual prior task output")

    assert result["provenance"] == {
        "current_request_id": "chat:58:request:current",
        "issued_by_current_request": False,
        "origin": "historical_or_external",
        "source_request_id": None,
        "task_id": 24,
    }
    assert result["task_output"] == [{
        "response_text": "manual prior task output",
        "task_id": 24,
    }]
    notice = tools._current_request_task_provenance_notice()
    assert "task 24" in notice
    assert "not issued by this request" in notice


def test_exact_current_task_is_current_and_needs_no_historical_notice():
    tools = _tools()
    tools._record_current_request_task(24)

    result = _read_output(tools, 24, "current task output")

    assert result["provenance"]["issued_by_current_request"] is True
    assert result["provenance"]["origin"] == "current_request"
    assert tools._current_request_task_provenance_notice() == ""


def test_completed_output_cache_recomputes_provenance_for_new_request():
    tools = _tools("request-a")
    tools._record_current_request_task(24)

    async def fake_output(*, mythic, task_display_id):
        return _encoded_output(task_display_id, "cached immutable output")

    async def completed(_task_id):
        return True

    with (
        patch.object(mythic_tools.mythic, "get_all_task_output_by_id", fake_output),
        patch.object(tools, "_is_task_completed", completed),
    ):
        first = json.loads(asyncio.run(tools.get_all_task_output_by_task_id(24)))
    assert first["provenance"]["issued_by_current_request"] is True

    tools.set_request_contract(_contract("request-b"))
    with patch.object(
        mythic_tools.mythic,
        "get_all_task_output_by_id",
        side_effect=AssertionError("completed output should come from the neutral cache"),
    ):
        second = json.loads(asyncio.run(tools.get_all_task_output_by_task_id(24)))

    assert second["task_output"] == first["task_output"]
    assert second["provenance"] == {
        "current_request_id": "request-b",
        "issued_by_current_request": False,
        "origin": "prior_sage_request",
        "source_request_id": "request-a",
        "task_id": 24,
    }


def test_completed_output_cache_does_not_decode_already_decoded_bytes_twice():
    tools = _tools("request-a")
    tools._record_current_request_task(24)

    async def fake_output(*, mythic, task_display_id):
        # The decoded text is itself valid base64. A second decode would silently change it to
        # "Test", so this catches request-neutral caches that rerun the wire decoder on a hit.
        return _encoded_output(task_display_id, "VGVzdA==")

    async def completed(_task_id):
        return True

    with (
        patch.object(mythic_tools.mythic, "get_all_task_output_by_id", fake_output),
        patch.object(tools, "_is_task_completed", completed),
    ):
        first = json.loads(asyncio.run(tools.get_all_task_output_by_task_id(24)))
        second = json.loads(asyncio.run(tools.get_all_task_output_by_task_id(24)))

    assert first["task_output"] == second["task_output"]
    assert second["task_output"][0]["response_text"] == "VGVzdA=="


def test_task_identity_is_exact_and_same_request_revision_preserves_lineage():
    tools = _tools("request-a")
    tools._record_current_request_task(11)

    task_one = _read_output(tools, 1)
    task_eleven = _read_output(tools, 11)
    tools.set_request_contract(tools._request_contract.amend())
    task_eleven_after_amend = _read_output(tools, 11)

    assert task_one["provenance"]["issued_by_current_request"] is False
    assert task_eleven["provenance"]["issued_by_current_request"] is True
    assert task_eleven_after_amend["provenance"]["issued_by_current_request"] is True


def test_callback_history_rows_carry_exact_request_provenance():
    tools = _tools("request-a")
    tools._record_current_request_task(11)

    async def fake_history(*, mythic, callback_display_id):
        assert callback_display_id == 7
        return [
            {"display_id": 1, "command_name": "manual"},
            {"display_id": 11, "command_name": "current"},
            {"display_id": "bad", "command_name": "malformed"},
        ]

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_history):
        result = json.loads(asyncio.run(tools.get_task_history_for_callback(7)))

    assert result[0]["request_provenance"]["issued_by_current_request"] is False
    assert result[1]["request_provenance"]["issued_by_current_request"] is True
    assert result[2]["request_provenance"]["origin"] == "unknown_task_identity"
    assert "prior or external Mythic task history" in tools._current_request_task_provenance_notice()


def test_unknown_callback_history_identity_does_not_claim_historical_task_inspection():
    tools = _tools("request-a")

    async def fake_history(*, mythic, callback_display_id):
        assert callback_display_id == 7
        return [
            {"display_id": "bad", "command_name": "malformed"},
            {"command_name": "missing-identity"},
        ]

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_history):
        result = json.loads(asyncio.run(tools.get_task_history_for_callback(7)))

    assert [
        row["request_provenance"]["origin"] for row in result
    ] == ["unknown_task_identity", "unknown_task_identity"]
    assert tools._current_request_task_provenance_notice() == ""


def test_successful_task_creation_binds_lineage_before_wait_failure():
    tools = _tools("request-a")

    async def fake_issue(*, mythic, command_name, parameters, callback_display_id, **_kwargs):
        return {"display_id": 4242}

    async def failed_wait(*, mythic, task_display_id, timeout=None):
        assert task_display_id == 4242
        raise RuntimeError("wait failed after Mythic accepted task")

    with (
        patch.object(mythic_tools.mythic, "issue_task", fake_issue),
        patch.object(mythic_tools.mythic, "waitfor_for_task_output", failed_wait),
        patch.object(tools, "_turn_authority_issue_blocker", return_value=None),
        patch.object(tools, "_bind_contract_task_issue_parameters", return_value=None),
    ):
        result = asyncio.run(
            tools.issue_task_and_waitfor_task_output("whoami", "", 7)
        )

    assert "wait failed" in result
    assert tools._task_request_provenance(4242)["issued_by_current_request"] is True


def test_plain_capability_cache_cannot_contaminate_structured_tool_output():
    tools = _tools("request-a")
    tools._task_output_cache[24] = "plain capability text"

    result = _read_output(tools, 24, "structured tool text")

    assert result["task_output"][0]["response_text"] == "structured tool text"
