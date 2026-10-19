"""An approved capability may register only its immediate callback command's schema-selected file."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.langgraph import mythic_tools as mt
from ai.langgraph import engagement_state
from ai.langgraph.request_contract import action_spec_from_tool_call, build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from sage_chat.hitl import (
    approval_action_digest, approval_action_fingerprint,
    approval_proposal_digest, approval_selection_digest,
)


@pytest.fixture
def case(monkeypatch, tmp_path):
    action = {"name": "example-capability", "intent": {"callback_id": "7"}}
    inputs = {"callback_id": "7"}
    root = {"name": "execute_capability", "args": {"action": action, "inputs": inputs}}
    contract = build_request_contract(
        request_id="request-file", channel_id="channel-file", operation_id="operation-file",
        mode="supervised", autonomous_solve=True,
        requested_actions=(action_spec_from_tool_call(root),),
    )
    digest = approval_action_digest([root])
    ids = [approval_action_fingerprint(root)]
    claim = {
        "approval_id": "approved-file", "request_id": contract.request_id,
        "request_contract_digest": contract.digest, "tool_name": "execute_capability",
        "selection_mode": "single", "actions": [root], "approved_actions": [root],
        "approved_action_ids": ids, "action_digest": digest,
        "proposal_digest": approval_proposal_digest(contract.digest, digest),
        "selection_digest": approval_selection_digest(contract.digest, digest, ids),
    }
    client = mt.MythicTools(agent_task_id=1)
    client.client = object()
    client.set_request_contract(contract)
    client.set_turn_authority(authority_from_request_contract(contract))
    client.set_approval_claim(claim)
    events = []
    registered = []

    async def schema(command, callback_id):
        assert command == "custom-runner" and callback_id == 7
        return [
            {"name": "registered_ref", "cli_name": "tool", "type": "ChooseOne",
             "parameter_group_name": "Default", "required": True, "choices": list(registered)},
            {"name": "file", "cli_name": "file", "type": "File",
             "parameter_group_name": "New File", "required": True},
        ]

    async def register(filename, content):
        events.append(("register", filename))
        registered.append(filename)
        assert content == b"inert local fixture"
        return "file-uuid", False

    async def issue(**kwargs):
        events.append(("task", kwargs["command_name"], kwargs["callback_display_id"]))
        return {"display_id": 1}

    async def output(**_kwargs):
        return "Completed successfully"

    async def preflight(_action, _inputs, _callback_id, _timeout, _capabilities):
        result = await client._issue_capability_callback_command(
            {"command": "custom-runner", "parameters": {"tool": "Example.exe"}},
            "custom-runner", {"tool": "Example.exe"}, 7, None, "example-capability",
        )
        return {"status": "achieved" if result[0] == "Completed successfully" else "failed",
                "reason": result[0], "issued": []}

    monkeypatch.setattr(mt.ttp_library, "TOOLS_DIR", tmp_path)
    (tmp_path / "Example.exe").write_bytes(b"inert local fixture")
    action_obj = SimpleNamespace(name="example-capability", target="", intent={"callback_id": "7"}, effects=[])
    monkeypatch.setattr(client, "_capability_tool_action", lambda *_args: action_obj)
    monkeypatch.setattr(client, "_ensure_capability_transaction_context", lambda value, _inputs: (value, ""))
    monkeypatch.setattr(client, "_capability_achieved_effects", lambda: set())
    monkeypatch.setattr(client, "_capability_action_effects_achieved", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(client, "_augment_capability_runtime_inputs", AsyncMock())
    monkeypatch.setattr(client, "_bind_capability_mythic_adapter", AsyncMock())
    monkeypatch.setattr(client, "_ensure_capability_executor_proof_target", AsyncMock())
    monkeypatch.setattr(client, "_capability_host_scoped_precondition_failure", lambda *_args: None)
    monkeypatch.setattr(client, "_capability_artifact_scoped_precondition_failure", lambda *_args: None)
    monkeypatch.setattr(client, "_capability_executor_injected_blocker", lambda *_args: None)
    monkeypatch.setattr(client, "_execute_capability_account_context_prerequisite", AsyncMock(return_value={"issued": []}))
    monkeypatch.setattr(client, "_execute_capability_current_context_preflight", preflight)
    monkeypatch.setattr(client, "_fetch_command_schema", schema)
    monkeypatch.setattr(client, "_find_uploaded_file_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_register_file_dedup", register)
    monkeypatch.setattr(client, "_invalidate_command_schema_cache", AsyncMock())
    monkeypatch.setattr(client, "_authenticate_live_command", AsyncMock(return_value={"status": "available"}))
    monkeypatch.setattr(client, "_callback_tasking_liveness_blocker", AsyncMock(return_value=""))
    monkeypatch.setattr(client, "_action_footprint", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_supervised_task_state", AsyncMock(return_value={"task_completed": True, "task_status": "completed"}))
    monkeypatch.setattr(mt.mythic, "issue_task", issue)
    monkeypatch.setattr(mt.mythic, "waitfor_for_task_output", output)
    return SimpleNamespace(client=client, action=action, inputs=inputs, root=root,
                           contract=contract, claim=claim, events=events, registered=registered)


def test_approved_capability_registers_schema_selected_file_then_tasks(case):
    result = json.loads(asyncio.run(case.client.execute_capability(case.action, case.inputs)))
    assert result["ok"] is True
    assert case.events == [("register", "Example.exe"), ("task", "custom-runner", 7)]


def test_registered_choice_skips_upload(case):
    case.registered.append("Example.exe")
    result = json.loads(asyncio.run(case.client.execute_capability(case.action, case.inputs)))
    assert result["ok"] is True
    assert case.events == [("task", "custom-runner", 7)]


def test_command_without_registered_file_selector_tasks_without_upload(case, monkeypatch):
    monkeypatch.setattr(case.client, "_fetch_command_schema", AsyncMock(return_value=[]))
    result = json.loads(asyncio.run(case.client.execute_capability(case.action, case.inputs)))
    assert result["ok"] is True
    assert case.events == [("task", "custom-runner", 7)]


def test_direct_upload_does_not_borrow_capability_approval(case):
    with pytest.raises(PermissionError, match="does not cover this exact effect"):
        asyncio.run(case.client.ensure_tool_uploaded("Example.exe"))
    assert case.events == []


@pytest.mark.parametrize("mutation", [
    "filename", "filename-case", "filename-path", "claim", "request", "capability",
    "callback", "command", "parameters", "child-task",
])
def test_capability_registration_rejects_changed_scope(case, monkeypatch, mutation):
    original_upload = case.client.ensure_tool_uploaded
    original_preflight = case.client._ensure_registered_file_available

    if mutation in {"command", "parameters", "callback"}:
        async def altered_preflight(command, parameters, callback_id):
            if mutation == "command":
                command = "other-runner"
            elif mutation == "parameters":
                parameters = {"tool": "Other.exe"}
            else:
                callback_id = 8
            return await original_preflight(command, parameters, callback_id)

        async def schema(_command, _callback_id):
            return [
                {"name": "registered_ref", "cli_name": "tool", "type": "ChooseOne",
                 "parameter_group_name": "Default", "required": True, "choices": []},
                {"name": "file", "cli_name": "file", "type": "File",
                 "parameter_group_name": "New File", "required": True},
            ]

        monkeypatch.setattr(case.client, "_ensure_registered_file_available", altered_preflight)
        monkeypatch.setattr(case.client, "_fetch_command_schema", schema)
    else:
        async def altered_upload(filename):
            if mutation.startswith("filename"):
                filename = {"filename": "Other.exe", "filename-case": "example.exe",
                            "filename-path": "../Example.exe"}[mutation]
            elif mutation == "claim":
                case.client._active_approval_claim["approval_id"] = "other-approval"
            elif mutation == "request":
                case.client._active_approval_claim["request_id"] = "other-request"
            elif mutation == "capability":
                mt._capability_command_context.get()["capability"] = "other-capability"
            elif mutation == "child-task":
                return await asyncio.create_task(original_upload(filename))
            return await original_upload(filename)

        monkeypatch.setattr(case.client, "ensure_tool_uploaded", altered_upload)

    result = json.loads(asyncio.run(case.client.execute_capability(case.action, case.inputs)))
    assert result["ok"] is False, result
    assert case.events == []


def test_supported_capability_plan_registers_its_real_runner_file(monkeypatch, tmp_path):
    """Use the actual capability planner and callback issuer; replace only external effects."""
    action = {"capability": "gpo-controlled-system-exec", "domain": "fixture.test",
              "gpo": "fixture-policy", "callback_id": "7"}
    inputs = {"callback_id": "7", "allow_proof_only": True, "command": "cmd.exe",
              "arguments": "/c whoami", "wait_seconds": 0, "proof_retries": 0}
    root = {"name": "execute_capability", "args": {"action": action, "inputs": inputs}}
    contract = build_request_contract(
        request_id="request-plan", channel_id="channel-plan", operation_id="operation-plan",
        mode="supervised", autonomous_solve=True,
        requested_actions=(action_spec_from_tool_call(root),),
    )
    digest = approval_action_digest([root])
    ids = [approval_action_fingerprint(root)]
    claim = {"approval_id": "approved-plan", "request_id": contract.request_id,
             "request_contract_digest": contract.digest, "tool_name": "execute_capability",
             "selection_mode": "single", "actions": [root], "approved_actions": [root],
             "approved_action_ids": ids, "action_digest": digest,
             "proposal_digest": approval_proposal_digest(contract.digest, digest),
             "selection_digest": approval_selection_digest(contract.digest, digest, ids)}
    client = mt.MythicTools(agent_task_id=1)
    client.client = object()
    client.set_request_contract(contract)
    client.set_turn_authority(authority_from_request_contract(contract))
    client.set_approval_claim(claim)
    client._engagement_footholds = [engagement_state.Foothold(
        callback_id="7", agent="apollo", host="fixture-host", forest="fixture.test",
        identity="FIXTURE\\operator", integrity="high", alive=True,
        source="offline-fixture", timestamp="2026-01-01T00:00:00Z",
    )]
    events = []
    registered = []

    async def schema(command, callback_id):
        assert callback_id == 7
        if command != "execute_assembly":
            return []
        return [
            {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
             "parameter_group_name": "Default", "required": True, "choices": list(registered)},
            {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
             "parameter_group_name": "Default", "required": False},
            {"name": "assembly_file", "type": "File", "parameter_group_name": "New File",
             "required": True},
        ]

    async def register(filename, content):
        assert content == b"inert local fixture"
        registered.append(filename)
        events.append(("register", filename))
        return "file-uuid", False

    async def issue(**kwargs):
        events.append(("task", kwargs["command_name"], kwargs["callback_display_id"]))
        return {"display_id": len(events)}

    monkeypatch.setattr(mt.ttp_library, "TOOLS_DIR", tmp_path)
    (tmp_path / "SharpGPOAbuse.exe").write_bytes(b"inert local fixture")
    monkeypatch.setattr(client, "_fetch_command_schema", schema)
    monkeypatch.setattr(client, "_find_uploaded_file_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_register_file_dedup", register)
    monkeypatch.setattr(client, "_invalidate_command_schema_cache", AsyncMock())
    monkeypatch.setattr(client, "_authenticate_live_command", AsyncMock(return_value={"status": "available"}))
    monkeypatch.setattr(client, "_callback_tasking_liveness_blocker", AsyncMock(return_value=""))
    monkeypatch.setattr(client, "_action_footprint", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_supervised_task_state", AsyncMock(return_value={"task_completed": True, "task_status": "success"}))
    monkeypatch.setattr(mt.mythic, "issue_task", issue)
    monkeypatch.setattr(mt.mythic, "waitfor_for_task_output", AsyncMock(return_value="inert incomplete result"))
    result = json.loads(asyncio.run(client.execute_capability(action, inputs)))
    assert isinstance(result, dict)
    assert events[:2] == [("register", "SharpGPOAbuse.exe"), ("task", "execute_assembly", 7)], (events, result)
