"""A native card binds the same deterministic task arguments the sink sees."""

import asyncio
import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph.model import Model, _TurnAuthorityToolMiddleware
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph import command_parameter_schema
from mythic import mythic
from mythic import mythic_utilities
from ai.langgraph.request_contract import build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from sage_chat.hitl import (approval_action_digest, approval_action_fingerprint,
                            approval_proposal_digest, approval_selection_digest,
                            build_approval_request)
from sage_chat import session
from sage_chat.headless import build_chat_request
from tests.test_supervised_objective_native import driver


TOOL = "issue_task_and_waitfor_task_output"
RAW = {"command": "dcsync", "callback_display_id": 2, "timeout": 60,
       "parameters": {"domain": "lab.invalid",
                      "dc": "dc.lab.invalid", "user": "krbtgt"}}
SCHEMA = [{"name": key, "cli_name": key, "type": "String",
           "parameter_group_name": "Default", "required": True,
           "default_value": "", "choices": []} for key in ("Domain", "DC", "User")]


def _tools():
    tools = MythicTools(agent_task_id="offline-approval")
    tools.client = object()
    return tools


def _claim(tools, action):
    contract = build_request_contract(
        request_id="request-prepared", channel_id="channel-prepared",
        operation_id="operation-prepared", mode="supervised", autonomous_solve=False,
        requested_actions=(),
    )
    # The producer inserts its prepared action into this request before the card.
    from ai.langgraph.request_contract import action_spec_from_tool_call
    contract = contract.amend(requested_actions=(action_spec_from_tool_call(action),))
    tools.set_turn_authority(authority_from_request_contract(contract))
    tools.set_request_contract(contract)
    digest = approval_action_digest([action])
    action_id = approval_action_fingerprint(action)
    tools.set_approval_claim({
        "approval_id": "approved-prepared", "request_id": contract.request_id,
        "request_contract_digest": contract.digest, "tool_name": TOOL,
        "selection_mode": "single", "actions": [action],
        "approved_actions": [action], "approved_action_ids": [action_id],
        "action_digest": digest,
        "proposal_digest": approval_proposal_digest(contract.digest, digest),
        "selection_digest": approval_selection_digest(contract.digest, digest, [action_id]),
    })
    return contract


def test_retained_dcsync_prepared_before_card_and_exact_after_restore(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "dcsync"}))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=SCHEMA))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    model = Model.__new__(Model)
    model.mode = "supervised"
    model._native_chat_explicit_hitl = True
    model.mythic_client = tools
    model._request_dynamic_proposals = True
    model._request_contract = build_request_contract(
        request_id="request-prepared", channel_id="channel-prepared",
        operation_id="operation-prepared", mode="supervised", autonomous_solve=False)
    model.install_request_contract = lambda contract: setattr(model, "_request_contract", contract)
    middleware = _TurnAuthorityToolMiddleware(model, agent_name="Mythic_Operator")
    monkeypatch.setattr(middleware, "_after_model_update",
        lambda state: model.bind_supervised_request_proposal(state["messages"][-1].tool_calls))
    message = AIMessage(content="", tool_calls=[{"name": TOOL, "args": copy.deepcopy(RAW), "id": "call-1"}])
    asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    prepared = copy.deepcopy(message.tool_calls[0])  # checkpointed tool call, not ambient cache
    card = build_approval_request([prepared])
    assert prepared["args"]["parameters"] == {
        "Domain": "lab.invalid", "DC": "dc.lab.invalid",
        "User": "LAB\\krbtgt",
    }
    assert prepared["args"]["visibility_context"]["_sage_prepared_action"] is True
    assert "lab.invalid" in card["description"]
    assert "krbtgt" in card["description"]
    assert model._request_contract.requested_actions
    _claim(tools, prepared)
    assert tools._approval_effect_blocker(TOOL, prepared["args"]) == ""
    changed = copy.deepcopy(prepared["args"])
    changed["parameters"]["User"] = "LAB\\other"
    assert "does not cover this exact effect" in tools._approval_effect_blocker(TOOL, changed)
    # A later resolver binding cannot excuse a material post-approval change.
    tools._last_argres_binding = {"command": "dcsync", "callback_display_id": 2,
        "original": prepared["args"]["parameters"], "resolved": changed["parameters"]}
    assert "does not cover this exact effect" in tools._approval_effect_blocker(TOOL, changed)


def test_preparation_failure_creates_no_guarded_card(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "missing", "payload_type": "apollo"}))
    model = SimpleNamespace(mode="supervised", _native_chat_explicit_hitl=True,
                            mythic_client=tools)
    middleware = _TurnAuthorityToolMiddleware(model)
    monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
    message = AIMessage(content="", tool_calls=[{"name": TOOL, "args": copy.deepcopy(RAW), "id": "call-1"}])
    asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    assert message.tool_calls == []
    assert "Approval was not requested" in message.content


def test_explicit_token_is_refused_before_approval():
    tools = _tools()
    action = {**RAW, "token_id": 42}
    with pytest.raises(ValueError, match="explicit token_id tasking is unsupported"):
        asyncio.run(tools._prepare_supervised_issue_action(action))


def test_unknown_schema_fails_before_card_but_known_empty_schema_prepares(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "whoami"}))
    schema = AsyncMock(return_value=None)
    monkeypatch.setattr(tools, "_fetch_command_schema", schema)
    action = {"command": "whoami", "callback_display_id": 2, "parameters": ""}
    with pytest.raises(ValueError, match="schema is unavailable"):
        asyncio.run(tools._prepare_supervised_issue_action(action))
    schema.return_value = []
    prepared = asyncio.run(tools._prepare_supervised_issue_action(action))
    assert prepared["parameters"] == ""
    assert prepared["visibility_context"]["_sage_prepared_action"] is True


@pytest.mark.parametrize("command", ["register_file", "register_assembly"])
def test_registration_existing_file_selector_rejects_bare_uuid_before_card(monkeypatch, command):
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": command}))
    monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=[
        {"name": "file", "cli_name": "File", "type": "File",
         "parameter_group_name": "Default", "required": True, "choices": []},
        {"name": "existingFile", "cli_name": "existingFile", "type": "ChooseOne",
         "parameter_group_name": "Use Existing File", "required": True, "choices": []},
    ]))
    action = {"command": command, "callback_display_id": 2,
              "parameters": {"existingFile": "11111111-2222-4333-8444-555555555555"}}
    with pytest.raises(ValueError, match="bare UUID"):
        asyncio.run(tools._prepare_supervised_issue_action(action))


def test_schema_revalidation_bypasses_live_schema_cache(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    monkeypatch.setattr(command_parameter_schema, "DEFAULT_RESOLVER",
                        command_parameter_schema.CommandSchemaResolver())
    current = [copy.deepcopy(SCHEMA)]
    remote = AsyncMock(side_effect=lambda **_kwargs: {
        "Default": {"parameters": copy.deepcopy(current[0])}})
    monkeypatch.setattr(mythic, "get_command_parameter_options", remote)

    async def probe():
        first = await tools._fetch_command_schema("dcsync", 2)
        current[0] = [*copy.deepcopy(SCHEMA), {"name": "Extra", "type": "String"}]
        cached = await tools._fetch_command_schema("dcsync", 2)
        refreshed = await tools._fetch_command_schema("dcsync", 2, force_refresh=True)
        return first, cached, refreshed

    first, cached, refreshed = asyncio.run(probe())
    assert first == cached == SCHEMA
    assert refreshed != first
    assert remote.await_count == 2


def test_forced_schema_reads_refetch_and_do_not_reuse_cached_schema_on_error(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    monkeypatch.setattr(command_parameter_schema, "DEFAULT_RESOLVER",
                        command_parameter_schema.CommandSchemaResolver())
    current = [copy.deepcopy(SCHEMA)]

    async def upstream(**_kwargs):
        if current[0] is None:
            raise RuntimeError("inert schema fetch failure")
        return {"Default": {"parameters": copy.deepcopy(current[0])}}

    remote = AsyncMock(side_effect=upstream)
    monkeypatch.setattr(mythic, "get_command_parameter_options", remote)
    fallback = AsyncMock(return_value={"commandparameters": []})
    monkeypatch.setattr(mythic_utilities, "graphql_post", fallback)

    async def probe():
        first = await tools._fetch_command_schema("dcsync", 2)
        current[0] = [{**row, "parameter_group_name": "New"} for row in SCHEMA]
        second = await tools._fetch_command_schema("dcsync", 2, force_refresh=True)
        third = await tools._fetch_command_schema("dcsync", 2, force_refresh=True)
        current[0] = None
        missing = await tools._fetch_command_schema("dcsync", 2, force_refresh=True)
        return first, second, third, missing

    first, second, third, missing = asyncio.run(probe())
    assert first == SCHEMA
    assert second == third != first
    assert missing is None
    assert remote.await_count == 4
    fallback.assert_awaited_once()


def test_divergent_provider_envelope_is_rejected_before_preparation(monkeypatch):
    tools = _tools()
    preparer = AsyncMock(side_effect=AssertionError("divergent proposal was prepared"))
    monkeypatch.setattr(tools, "_prepare_supervised_issue_action", preparer)
    model = SimpleNamespace(mode="supervised", _native_chat_explicit_hitl=True,
                            mythic_client=tools, _request_contract=object())
    middleware = _TurnAuthorityToolMiddleware(model)
    raw_call = {"id": "call-1", "type": "function", "function": {
        "name": TOOL, "arguments": json.dumps({**RAW, "timeout": 99})}}
    message = AIMessage(content="", tool_calls=[{
        "name": TOOL, "args": copy.deepcopy(RAW), "id": "call-1"}],
        additional_kwargs={"tool_calls": [raw_call]})
    asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    assert message.tool_calls == []
    assert "raw tool_calls diverge" in message.content
    preparer.assert_not_awaited()


def test_divergent_multi_call_envelope_cannot_become_valid_after_pruning(monkeypatch):
    tools = _tools()
    preparer = AsyncMock(side_effect=AssertionError("divergent proposal was prepared"))
    monkeypatch.setattr(tools, "_prepare_supervised_issue_action", preparer)
    model = SimpleNamespace(mode="supervised", _native_chat_explicit_hitl=True,
                            mythic_client=tools, _request_contract=object())
    middleware = _TurnAuthorityToolMiddleware(model)
    first = {"name": TOOL, "args": copy.deepcopy(RAW), "id": "call-1"}
    second = {"name": TOOL, "args": {**copy.deepcopy(RAW), "callback_display_id": 3},
              "id": "call-2"}
    raw_first = {"id": "call-1", "type": "function", "function": {
        "name": TOOL, "arguments": json.dumps(RAW)}}
    message = AIMessage(content="", tool_calls=[first, second],
                        additional_kwargs={"tool_calls": [raw_first]})
    asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    assert message.tool_calls == []
    assert "raw tool_calls diverge" in message.content
    preparer.assert_not_awaited()


def test_failed_first_preparation_drops_entire_guarded_batch(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "missing", "payload_type": "apollo"}))
    model = SimpleNamespace(mode="supervised", _native_chat_explicit_hitl=True,
                            mythic_client=tools)
    middleware = _TurnAuthorityToolMiddleware(model)
    monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
    message = AIMessage(content="", tool_calls=[
        {"name": TOOL, "args": copy.deepcopy(RAW), "id": "call-1"},
        {"name": TOOL, "args": {**copy.deepcopy(RAW), "callback_display_id": 3}, "id": "call-2"},
    ])
    asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    assert message.tool_calls == []
    assert "Approval was not requested" in message.content


def test_multiple_proposals_keep_first_prepared_call_and_deferred_result(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "dcsync"}))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=SCHEMA))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    model = Model.__new__(Model)
    model.mode = "supervised"
    model._native_chat_explicit_hitl = True
    model.mythic_client = tools
    model._request_dynamic_proposals = True
    model._denied_action_keys = ()
    model._request_contract = build_request_contract(
        request_id="request-multiple", channel_id="channel-multiple",
        operation_id="operation-multiple", mode="supervised", autonomous_solve=False)
    model._turn_authority = authority_from_request_contract(model._request_contract)
    def install(contract):
        model._request_contract = contract
        model._turn_authority = authority_from_request_contract(contract)
    model.install_request_contract = install
    middleware = _TurnAuthorityToolMiddleware(model, agent_name="Mythic_Operator")
    message = AIMessage(content="", tool_calls=[
        {"name": TOOL, "args": copy.deepcopy(RAW), "id": "call-1"},
        {"name": TOOL, "args": {**copy.deepcopy(RAW), "callback_display_id": 3}, "id": "call-2"},
    ])
    result = asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0]["args"]["parameters"]["User"] == "LAB\\krbtgt"
    assert [item.tool_call_id for item in result["messages"][1:]] == ["call-2"]
    assert len(model._request_contract.requested_actions) == 1


@pytest.mark.parametrize(
    "case", ["dcsync", "sharphound", "inline_registered", "inline_file",
             "register_file", "register_assembly"]
)
def test_native_approval_reaches_registered_task_sink_with_prepared_args(monkeypatch, tmp_path, case):
    """Checkpointed HITL resume carries prepared args through the real task tool."""
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr("socket.socket.connect", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("offline approval test attempted network access")))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("offline approval test attempted network access")))
    tools = _tools()
    issued = []
    posted = []
    if case == "dcsync":
        raw, command, schema = copy.deepcopy(RAW), "dcsync", SCHEMA
    elif case in {"sharphound", "inline_registered", "inline_file"}:
        command = "execute_assembly" if case == "sharphound" else "inline_assembly"
        file_uuid = "11111111-2222-4333-8444-555555555555"
        raw = {"command": command, "callback_display_id": 2, "timeout": 60,
               "parameters": (
                   {"assembly_name": "SharpHound.exe",
                    "assembly_arguments": r"-c All -o C:\Temp"}
                   if case == "sharphound" else
                   {"assembly_file": (
                        "sharphound.EXE" if case == "inline_registered" else file_uuid
                    ), "assembly_arguments": "triage"}
               )}
        schema = [
            {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
             "parameter_group_name": "Default", "required": True,
             "default_value": "", "choices": ["SharpHound.exe"]},
            {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
             "parameter_group_name": "Default", "required": False,
             "default_value": "", "choices": []},
            {"name": "assembly_file", "cli_name": "AssemblyFile", "type": "File",
             "parameter_group_name": "New Assembly", "required": True,
             "default_value": "", "choices": []},
        ]
    else:
        command = case
        raw = {"command": command, "callback_display_id": 2, "timeout": 60,
               "parameters": {"file": "11111111-2222-4333-8444-555555555555"}}
        schema = [
            {"name": "file", "cli_name": "File", "type": "File",
             "parameter_group_name": "Default", "required": True,
             "default_value": "", "choices": []},
            {"name": "existingFile", "cli_name": "existingFile", "type": "ChooseOne",
             "parameter_group_name": "Use Existing File", "required": True,
             "default_value": "", "choices": []},
        ]
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": command}))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=schema))
    if case in {"sharphound", "inline_registered"}:
        monkeypatch.setattr(tools, "_find_uploaded_file_by_name", AsyncMock(return_value={
            "id": 77, "agent_file_id": "inert-file-77", "filename_utf8": "SharpHound.exe",
            "md5": "inert-md5", "sha1": "inert-sha1", "complete": True}))
        monkeypatch.setattr(
            tools, "_download_registered_file_bytes_bounded",
            AsyncMock(return_value=b"inert unenrolled SharpHound bytes"),
        )
    elif case == "inline_file":
        monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
        monkeypatch.setattr(tools, "_get_file_metadata", AsyncMock(return_value={
            "id": 88, "agent_file_id": file_uuid, "filename_utf8": "SharpDPAPI.exe",
            "md5": "file-md5", "sha1": "file-sha1", "complete": True,
            "deleted": False, "is_payload": False, "is_download_from_agent": False,
        }))
        monkeypatch.setattr(tools, "_download_registered_file_bytes_bounded",
                            AsyncMock(return_value=b"unknown SharpDPAPI fixture bytes"))
    if case in {"register_file", "register_assembly"}:
        monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    else:
        monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    if case == "inline_file":
        async def inert_create_task(*, variables, **_kwargs):
            posted.append(copy.deepcopy(variables))
            return {"createTask": {"status": "success", "display_id": 901}}
        monkeypatch.setattr(mythic_utilities, "graphql_post", inert_create_task)
        monkeypatch.setattr(mythic, "waitfor_for_task_output",
                            AsyncMock(return_value={"response": "inert completion"}))
        monkeypatch.setattr(tools, "_supervised_task_state", AsyncMock(return_value={
            "task_completed": True, "task_status": "completed",
        }))
    else:
        async def inert_sink(command, parameters, callback_display_id, **kwargs):
            issued.append((command, parameters, callback_display_id, kwargs))
            raise RuntimeError("inert task sink reached")
        monkeypatch.setattr(tools, "_issue_mythic_task", inert_sink)
    prompt = "Run the specified LAB check on callback 2."
    fake = driver.ScriptedModel(messages=iter([
        driver.call("transfer_to_Mythic_Operator", {
            "handoff_title": "LAB check", "handoff_instruction": prompt}, 1),
        driver.call(TOOL, raw, 2),
        AIMessage(content="The task result was handled."),
        AIMessage(content="The request is complete."),
    ]))

    async def episode():
        model = Model(
            provider="openai", model="offline-model", system_prompt="offline test",
            config={"configurable": {"api_key": "offline-only",
                                    "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="offline-prepared", mode="supervised",
            autonomous_solve=False, operation_id=4, channel_id=9, apitoken_id=0,
        )
        model.mythic_client = tools
        model._payload_names = []
        model.memory = InMemorySaver()
        model._get_base_chat_model = lambda *args, **kwargs: fake
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = driver.DriverChat(model)
        first = build_chat_request(prompt, channel_id=9, operation_id=4,
                                   request_id=1, config={"mode": "supervised",
                                                         "autonomous_solve": False})
        session.bind_channel_thread_id(first, model)
        try:
            await chat.chat(first)
            chat._used = True
            cards = [item["metadata"]["input_requested"] for item in chat.emissions
                     if (item.get("metadata") or {}).get("special_type") == "input_requested"]
            assert len(cards) == 1
            assert issued == []
            card_args = cards[0]["data"]["arguments"]
            if case == "dcsync":
                assert card_args["parameters"] == {
                    "Domain": "lab.invalid", "DC": "dc.lab.invalid", "User": "LAB\\krbtgt"}
            elif case == "sharphound":
                assert card_args["parameters"] == {
                    "Assembly": "SharpHound.exe",
                    "Arguments": r"-c All --OutputDirectory C:\Temp",
                }
                assert card_args["visibility_context"]["_sage_prepared_binding"]["file"]["agent_file_id"] == "inert-file-77"
            elif case == "inline_registered":
                assert card_args["parameters"] == {
                    "Assembly": "SharpHound.exe", "Arguments": "triage",
                }
                assert card_args["visibility_context"]["_sage_prepared_binding"]["group"] is None
            elif case == "inline_file":
                assert card_args["parameters"] == {
                    "AssemblyFile": file_uuid, "Arguments": "triage",
                }
                assert card_args["visibility_context"]["_sage_prepared_binding"]["file"]["agent_file_id"] == file_uuid
                assert card_args["visibility_context"]["_sage_prepared_binding"]["group"] == "New Assembly"
            else:
                assert card_args["parameters"] == {"File": "11111111-2222-4333-8444-555555555555"}
                assert card_args["visibility_context"]["_sage_prepared_binding"]["group"] is None
            second = build_chat_request("", channel_id=9, operation_id=4,
                                        request_id=2, config={"mode": "supervised",
                                                              "autonomous_solve": False})
            second.InputResponse = driver.input_response("accept", cards[0])
            await chat.chat(second)
            if case == "inline_file":
                task_posts = [variables for variables in posted if variables.get("command")]
                assert len(task_posts) == 1
                assert task_posts[0]["command"] == command
                assert json.loads(task_posts[0]["params"]) == card_args["parameters"]
                assert task_posts[0]["parameter_group_name"] == "New Assembly"
            else:
                assert len(issued) == 1
                assert issued[0][:3] == (command, card_args["parameters"], 2)
            if case in {"inline_registered", "register_file", "register_assembly"}:
                # The Mythic transport omits the group variable for its Default group.
                assert issued[0][3]["parameter_group_name"] is None
        finally:
            await session.drop_channel_session(first)

    asyncio.run(episode())


@pytest.mark.parametrize("defect", [
    "not_uuid", "missing", "uuid", "incomplete", "deleted", "payload", "download", "hash",
])
def test_inline_file_preparation_rejects_missing_or_wrong_file_identity(monkeypatch, defect):
    tools = _tools()
    file_uuid = "11111111-2222-4333-8444-555555555555"
    schema = [
        {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
         "parameter_group_name": "Default", "required": True, "choices": ["Known.exe"]},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "parameter_group_name": "Default", "required": False, "choices": []},
        {"name": "assembly_file", "cli_name": "AssemblyFile", "type": "File",
         "parameter_group_name": "New Assembly", "required": True, "choices": []},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "parameter_group_name": "New Assembly", "required": False, "choices": []},
    ]
    identity = {
        "id": 88, "agent_file_id": file_uuid, "filename_utf8": "Fresh.exe",
        "md5": "md5", "sha1": "sha1", "complete": True, "deleted": False,
        "is_payload": False, "is_download_from_agent": False,
    }
    if defect == "missing":
        identity = None
    elif defect == "uuid":
        identity["agent_file_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    elif defect == "incomplete":
        identity["complete"] = False
    elif defect == "deleted":
        identity["deleted"] = True
    elif defect == "payload":
        identity["is_payload"] = True
    elif defect == "download":
        identity["is_download_from_agent"] = True
    elif defect == "hash":
        identity["sha1"] = ""
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "inline_assembly"}))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=schema))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    metadata = AsyncMock(return_value=identity)
    monkeypatch.setattr(tools, "_get_file_metadata", metadata)

    async def probe():
        return await tools._prepare_supervised_issue_action({
            "command": "inline_assembly", "callback_display_id": 2,
            "parameters": {"assembly_file": "not-a-uuid" if defect == "not_uuid" else file_uuid,
                           "assembly_arguments": "triage"},
        })

    with pytest.raises(ValueError, match="inline assembly File"):
        asyncio.run(probe())
    if defect == "not_uuid":
        metadata.assert_not_awaited()


@pytest.mark.parametrize("drift", ["argument", "uuid", "hash", "bytes", "group", "schema"])
def test_inline_file_postapproval_identity_drift_denies_before_sink(monkeypatch, drift):
    tools = _tools()
    file_uuid = "11111111-2222-4333-8444-555555555555"
    schema = [
        {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
         "parameter_group_name": "Default", "required": True, "choices": ["Known.exe"]},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "parameter_group_name": "Default", "required": False, "choices": []},
        {"name": "assembly_file", "cli_name": "AssemblyFile", "type": "File",
         "parameter_group_name": "New Assembly", "required": True, "choices": []},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "parameter_group_name": "New Assembly", "required": False, "choices": []},
    ]
    identity = [{
        "id": 88, "agent_file_id": file_uuid, "filename_utf8": "Fresh.exe",
        "md5": "md5", "sha1": "sha1", "complete": True, "deleted": False,
        "is_payload": False, "is_download_from_agent": False,
    }]
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "inline_assembly"}))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=schema))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_get_file_metadata",
                        AsyncMock(side_effect=lambda _uuid: copy.deepcopy(identity[0])))
    content = [b"unknown assembly fixture bytes"]
    monkeypatch.setattr(tools, "_download_registered_file_bytes_bounded",
                        AsyncMock(side_effect=lambda _uuid: content[0]))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    sink = AsyncMock(side_effect=AssertionError("drifted inline File reached sink"))
    monkeypatch.setattr(tools, "_issue_mythic_task", sink)

    async def probe():
        prepared = await tools._prepare_supervised_issue_action({
            "command": "inline_assembly", "callback_display_id": 2,
            "parameters": {"assembly_file": file_uuid, "assembly_arguments": "triage"},
        })
        _claim(tools, {"name": TOOL, "args": prepared})
        if drift == "argument":
            prepared["parameters"]["AssemblyFile"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        elif drift == "uuid":
            identity[0]["agent_file_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        elif drift == "hash":
            identity[0]["md5"] = "new-md5"
        elif drift == "bytes":
            content[0] = b"changed unknown assembly fixture bytes"
        elif drift == "group":
            schema[2]["parameter_group_name"] = "Other Assembly"
        else:
            schema[2]["required"] = False
        return await tools.issue_task_and_waitfor_task_output(**prepared)

    result = asyncio.run(probe())
    if drift == "argument":
        assert "does not cover this exact effect" in result
    elif drift in {"group", "schema"}:
        assert "task schema changed after approval" in result
    else:
        assert "inline assembly File identity changed after approval" in result
    sink.assert_not_awaited()


@pytest.mark.parametrize("valid_identity", [True, False])
def test_unprepared_inline_file_issue_validates_identity_before_grouped_sink(
    monkeypatch, valid_identity,
):
    tools = _tools()
    file_uuid = "11111111-2222-4333-8444-555555555555"
    parameters = {"assembly_file": file_uuid, "assembly_arguments": "triage"}
    action_args = {
        "command": "inline_assembly", "callback_display_id": 2,
        "parameters": parameters, "timeout": 60, "token_id": None,
    }
    schema = [
        {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
         "parameter_group_name": "Default", "required": True, "choices": ["Known.exe"]},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "parameter_group_name": "Default", "required": False, "choices": []},
        {"name": "assembly_file", "cli_name": "AssemblyFile", "type": "File",
         "parameter_group_name": "New Assembly", "required": True, "choices": []},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "parameter_group_name": "New Assembly", "required": False, "choices": []},
    ]
    identity = ({
        "id": 88, "agent_file_id": file_uuid, "filename_utf8": "Fresh.exe",
        "md5": "md5", "sha1": "sha1", "complete": True, "deleted": False,
        "is_payload": False, "is_download_from_agent": False,
    } if valid_identity else None)
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "inline_assembly"}))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=schema))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_get_file_metadata", AsyncMock(return_value=identity))
    monkeypatch.setattr(tools, "_download_registered_file_bytes_bounded",
                        AsyncMock(return_value=b"unknown assembly fixture bytes"))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    seen = []

    async def sink(command, resolved, callback_display_id, **kwargs):
        seen.append((command, resolved, callback_display_id, kwargs))
        raise RuntimeError("inert task sink reached")

    monkeypatch.setattr(tools, "_issue_mythic_task", sink)
    _claim(tools, {"name": TOOL, "args": action_args})
    result = asyncio.run(tools.issue_task_and_waitfor_task_output(**action_args))

    if valid_identity:
        assert len(seen) == 1
        assert seen[0][:3] == (
            "inline_assembly", {"AssemblyFile": file_uuid, "Arguments": "triage"}, 2,
        )
        assert seen[0][3]["parameter_group_name"] == "New Assembly"
    else:
        assert "inline assembly File UUID has no exact Mythic metadata identity" in result
        assert seen == []


@pytest.mark.parametrize("drift", ["schema", "file"])
def test_postapproval_binding_drift_denies_real_issue_before_sink(monkeypatch, drift):
    tools = _tools()
    if drift == "schema":
        command, parameters, schema = "dcsync", copy.deepcopy(RAW["parameters"]), copy.deepcopy(SCHEMA)
    else:
        command, parameters = "install_package", {"Package": "sample.exe"}
        schema = [
            {"name": "Package", "cli_name": "Package", "type": "ChooseOne",
             "parameter_group_name": "Default", "required": True,
             "default_value": "", "choices": ["sample.exe"]},
            {"name": "Binary", "cli_name": "Binary", "type": "File",
             "parameter_group_name": "Upload", "required": False,
             "default_value": "", "choices": []},
        ]
    live_schema = [copy.deepcopy(schema)]
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": command}))
    if drift == "schema":
        monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
        monkeypatch.setattr(command_parameter_schema, "DEFAULT_RESOLVER",
                            command_parameter_schema.CommandSchemaResolver())
        upstream = AsyncMock(side_effect=lambda **_kwargs: {
            "Default": {"parameters": copy.deepcopy(live_schema[0])}})
        monkeypatch.setattr(mythic, "get_command_parameter_options", upstream)
    else:
        monkeypatch.setattr(tools, "_fetch_command_schema",
                            AsyncMock(side_effect=lambda *_a, **_k: copy.deepcopy(live_schema[0])))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    identity = {"id": 1, "agent_file_id": "file-1", "filename_utf8": "sample.exe",
                "md5": "old-md5", "sha1": "old-sha1", "complete": True}
    live_identity = [identity]
    monkeypatch.setattr(tools, "_find_uploaded_file_by_name",
                        AsyncMock(side_effect=lambda _name: copy.deepcopy(live_identity[0])))
    sink = AsyncMock(side_effect=AssertionError("drifted task reached sink"))
    monkeypatch.setattr(tools, "_issue_mythic_task", sink)

    async def probe():
        prepared_args = await tools._prepare_supervised_issue_action({
            "command": command, "parameters": parameters,
            "callback_display_id": 2, "timeout": 60})
        action = {"name": TOOL, "args": prepared_args}
        _claim(tools, action)
        if drift == "schema":
            live_schema[0][0]["parameter_group_name"] = "Alternate"
        else:
            live_identity[0] = {**identity, "id": 2, "agent_file_id": "file-2",
                                "md5": "new-md5", "sha1": "new-sha1"}
        return await tools.issue_task_and_waitfor_task_output(**prepared_args)

    result = asyncio.run(probe())
    assert ("schema changed after approval" if drift == "schema"
            else "registered file identity changed after approval") in result
    sink.assert_not_awaited()
    if drift == "schema":
        assert upstream.await_count == 2


def _registered_file_guard_tools(
    monkeypatch,
    choices=("mimikatz",),
    command="run_as",
    selector=("Binary", "Binary"),
):
    """Guard harness: the resolver is stubbed because the guard branch is reachable
    in production through schemas whose choice lists are not populated (the
    'false means unknown' case); these tests target the guard's remedy message."""
    from ai.langgraph import command_builder

    schema = [
        {"name": selector[0], "cli_name": selector[1], "type": "ChooseOne",
         "parameter_group_name": "Default", "required": True,
         "default_value": "", "choices": list(choices)},
        {"name": "Payload", "cli_name": "Payload", "type": "File",
         "parameter_group_name": "Upload", "required": False,
         "default_value": "", "choices": []},
    ]
    tools = _tools()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": command}))
    monkeypatch.setattr(
        tools, "_fetch_enrolled_executable_schema",
        AsyncMock(return_value=copy.deepcopy(schema)))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    monkeypatch.setattr(
        tools, "_bind_mythic_credential_parameters",
        AsyncMock(side_effect=lambda _command, params, _cb, param_schema=None: params))

    class _Resolved:
        ok = True
        group = "Default"

        def __init__(self, params):
            self.params = params

    monkeypatch.setattr(
        command_builder, "resolve_params",
        lambda _schema, parameters, command=None: _Resolved(dict(parameters)))
    return tools


def test_registered_file_guard_error_names_the_registered_choices(monkeypatch):
    """A not-among-choices guard error enumerates the exact registered identifiers."""
    tools = _registered_file_guard_tools(
        monkeypatch, choices=("mimikatz",), command="execute_assembly",
        selector=("Assembly", "assembly_name"))
    monkeypatch.setattr(tools, "_find_uploaded_file_by_name", AsyncMock(return_value=None))

    async def probe():
        return await tools._prepare_supervised_issue_action({
            "command": "execute_assembly", "parameters": {"assembly_name": "mimikatz.exe"},
            "callback_display_id": 2, "timeout": 60})

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(probe())
    message = str(excinfo.value)
    assert "'mimikatz'" in message, (
        f"the guard error must enumerate the registered identifiers: {message}"
    )
    assert "no approval card will appear" in message
    assert "re-checking registrations" in message


def test_registered_file_guard_error_without_choice_list_names_the_schema_remedy(monkeypatch):
    """When the schema exposes no choices, the error sends the model to the schema, not a loop."""
    tools = _registered_file_guard_tools(
        monkeypatch, choices=(), command="execute_assembly",
        selector=("Assembly", "assembly_name"))
    monkeypatch.setattr(tools, "_find_uploaded_file_by_name", AsyncMock(return_value=None))

    async def probe():
        return await tools._prepare_supervised_issue_action({
            "command": "execute_assembly", "parameters": {"assembly_name": "mimikatz.exe"},
            "callback_display_id": 2, "timeout": 60})

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(probe())
    message = str(excinfo.value)
    assert "exposed no choice list" in message
    assert "command's own schema choices" in message
    assert "do not guess a filename" in message


def test_registered_file_identity_error_names_the_upload_remedy(monkeypatch):
    """A registered choice with no uploaded file names the operator-decision upload path."""
    tools = _registered_file_guard_tools(monkeypatch, choices=("mimikatz.exe",))
    monkeypatch.setattr(tools, "_find_uploaded_file_by_name", AsyncMock(return_value=None))

    async def probe():
        return await tools._prepare_supervised_issue_action({
            "command": "run_as", "parameters": {"Binary": "mimikatz.exe"},
            "callback_display_id": 2, "timeout": 60})

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(probe())
    message = str(excinfo.value)
    assert "registered choice for this command" in message
    assert "uploading it is an operator decision" in message.lower()
    assert "report this blocker to the operator" in message


def test_agent_resolved_executable_reference_prepares_without_file_binding(monkeypatch):
    """execute_pe's pe_name may name an agent-resolved (embedded) executable.

    Observed live (acceptance run): the same pe_name=mimikatz.exe passed preparation
    and executed when the schema fetch returned no choices, and was blocked pre-card
    when choices were populated — cache-state-dependent semantics. The target agent
    resolves the name itself, so preparation must not demand a Mythic file binding.
    """
    tools = _registered_file_guard_tools(
        monkeypatch, choices=("mimikatz",), command="execute_pe",
        selector=("PE", "pe_name"))

    async def probe():
        return await tools._prepare_supervised_issue_action({
            "command": "execute_pe",
            "parameters": {
                "pe_name": "mimikatz.exe",
                "pe_arguments": '"lsadump::dcsync /domain:sevenkingdoms.local '
                                '/dc:kingslanding.sevenkingdoms.local '
                                '/user:sevenkingdoms\\\\krbtgt" exit'},
            "callback_display_id": 2, "timeout": 60})

    prepared = asyncio.run(probe())
    binding = prepared["visibility_context"]["_sage_prepared_binding"]
    assert binding["file"] is None, (
        "an agent-resolved executable reference must prepare with no Mythic file "
        f"binding (the empty-schema shape): {binding}"
    )
    assert prepared["parameters"]["pe_name"] == "mimikatz.exe"


def test_issue_preflight_treats_unstaged_reference_as_agent_resolved(monkeypatch):
    """An unstaged by-name reference on a non-strict command must not dead-end.

    ensure_tool_uploaded distinguishes `missing` (not in Mythic's store and not locally
    staged — an upload cannot happen; the agent may resolve the name embedded) from
    `error` (a transport failure, which still blocks as designed). Observed live: the
    pre-card guard blocked the same value that a cold schema cache let through.
    """
    tools = _registered_file_guard_tools(
        monkeypatch, choices=("mimikatz",), command="execute_pe",
        selector=("PE", "pe_name"))
    attempts: list[str] = []

    async def fake_ensure(name):
        attempts.append(name)
        return json.dumps({
            "status": "missing", "binary_filename": name,
            "note": "Not in Mythic's file store and not found at tools/mimikatz.exe.",
        })

    monkeypatch.setattr(tools, "ensure_tool_uploaded", fake_ensure)

    result = asyncio.run(tools._ensure_registered_file_available(
        "execute_pe", {"pe_name": "mimikatz.exe"}, 2))

    assert attempts == ["mimikatz.exe"], "the upload attempt must still happen first"
    assert result is None, (
        f"an unstaged agent-resolved reference must not block the approved task: {result}"
    )


def test_issue_preflight_still_blocks_transport_upload_failures(monkeypatch):
    """A transport-level upload failure still blocks: only `missing` classifies as
    agent-resolved."""
    tools = _registered_file_guard_tools(
        monkeypatch, choices=("mimikatz",), command="execute_pe",
        selector=("PE", "pe_name"))

    async def fake_ensure(name):
        return json.dumps({
            "status": "error", "binary_filename": name,
            "error": "Mythic file upload /task_upload_file_webhook returned HTTP 404",
        })

    monkeypatch.setattr(tools, "ensure_tool_uploaded", fake_ensure)

    result = asyncio.run(tools._ensure_registered_file_available(
        "execute_pe", {"pe_name": "mimikatz.exe"}, 2))

    assert isinstance(result, str) and "Do not retry" in result


def test_strict_wrapper_still_blocks_unregistered_file_selection(monkeypatch):
    """The typed-executable wrappers stay strict: their bytes are contract-verified,
    so a by-name selection absent from the registered choices must still raise."""
    tools = _registered_file_guard_tools(
        monkeypatch, choices=("SharpKatz.exe",), command="execute_assembly",
        selector=("Assembly", "assembly_name"))
    monkeypatch.setattr(tools, "_find_uploaded_file_by_name", AsyncMock(return_value=None))

    async def probe():
        return await tools._prepare_supervised_issue_action({
            "command": "execute_assembly",
            "parameters": {"assembly_name": "mimikatz.exe", "arguments": "lsadump::dcsync"},
            "callback_display_id": 2, "timeout": 60})

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(probe())
    message = str(excinfo.value)
    assert "'SharpKatz.exe'" in message, (
        f"the strict wrapper must name the registered identifiers in its remedy: {message}"
    )
    assert "no approval card will appear" in message


@pytest.mark.parametrize("surface", ["callback", "payload_fallback"])
@pytest.mark.parametrize("regex", ["", r"^.{1,80}$"])
@pytest.mark.parametrize("change", [None, "verifier_regex", "required", "type", "parameter_group_name"])
def test_registered_file_seeded_schema_matches_forced_sdk_before_approved_sink(
    monkeypatch, surface, regex, change,
):
    """The live-surface query and SDK refresh must hash the same internal fields."""
    tools = _tools()
    monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    monkeypatch.setattr(command_parameter_schema, "DEFAULT_RESOLVER",
                        command_parameter_schema.CommandSchemaResolver())
    rows = [
        {"name": "file", "cli_name": "File", "type": "File", "description": "",
         "parameter_group_name": "Default", "required": True, "choices": [],
         "default_value": "", "dynamic_query_function": "", "verifier_regex": regex},
        {"name": "existingFile", "cli_name": "existingFile", "type": "ChooseOne",
         "description": "", "parameter_group_name": "Use Existing File", "required": True,
         "choices": [], "default_value": "", "dynamic_query_function": "get_files",
         "verifier_regex": ""},
    ]
    current = copy.deepcopy(rows)

    def projected(query):
        selection = re.search(r"commandparameters\s*\{([^{}]+)\}", query)
        assert selection, "fake backend requires an explicit command-parameter selection"
        keys = set(selection.group(1).split())
        return [{key: value for key, value in row.items() if key in keys} for row in rows]

    async def callback_query(_client, query, **_kwargs):
        if surface == "payload_fallback":
            return {"callback": []}
        return {"callback": [{"payload": {"payloadtype": {"name": "apollo"}},
                              "loadedcommands": [{"command": {
                                  "cmd": "register_assembly",
                                  "commandparameters": projected(query),
                              }}]}]}

    async def fallback_commands(_client, _payload, attr):
        return [{"cmd": "register_assembly", "commandparameters": projected(attr)}]

    async def sdk_schema(**_kwargs):
        return {
            "Default": {"parameters": [copy.deepcopy(current[0])]},
            "Use Existing File": {"parameters": [copy.deepcopy(current[1])]},
        }

    monkeypatch.setattr(mythic, "execute_custom_query", callback_query)
    monkeypatch.setattr(mythic, "get_all_commands_for_payloadtype", fallback_commands)
    monkeypatch.setattr(mythic, "get_command_parameter_options", sdk_schema)
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    sink = AsyncMock(side_effect=RuntimeError("inert task sink reached"))
    monkeypatch.setattr(tools, "_issue_mythic_task", sink)

    async def probe():
        payload, surface_commands, error = await tools._fetch_live_command_surface(5)
        assert payload == "apollo" and not error and surface_commands
        prepared = await tools._prepare_supervised_issue_action({
            "command": "register_assembly", "callback_display_id": 5, "timeout": 120,
            "parameters": {"file": "11111111-2222-4333-8444-555555555555"},
        })
        assert prepared["parameters"] == {"File": "11111111-2222-4333-8444-555555555555"}
        _claim(tools, {"name": TOOL, "args": prepared})
        if change:
            current[0][change] = {
                "verifier_regex": r"^[A-Z]+$", "required": False,
                "type": "String", "parameter_group_name": "Alternate",
            }[change]
        return await tools.issue_task_and_waitfor_task_output(**prepared)

    result = asyncio.run(probe())
    if change is None:
        assert "schema changed after approval" not in result
        sink.assert_awaited_once()
        assert "inert task sink reached" in result
    else:
        sink.assert_not_awaited()
        assert "schema changed after approval" in result


@pytest.mark.parametrize("surface", ["callback", "payload_fallback"])
@pytest.mark.parametrize("change", [
    None, "verifier_regex", "required", "type", "parameter_group_name",
    "choices", "default_value", "cli_name", "dynamic_query_function",
    "nested_order", "added", "removed", "duplicate", "missing",
])
def test_approved_schema_ignores_only_parameter_row_order(
    monkeypatch, surface, change,
):
    """A reordered producer schema reaches the approved sink; real drift does not."""
    tools = _tools()
    monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    monkeypatch.setattr(command_parameter_schema, "DEFAULT_RESOLVER",
                        command_parameter_schema.CommandSchemaResolver())
    rows = [
        {"name": name, "cli_name": name, "type": kind, "description": "",
         "parameter_group_name": "Default", "required": False, "choices": [],
         "default_value": default, "dynamic_query_function": "", "verifier_regex": ""}
        for name, kind, default in (
            ("base", "String", ""), ("query", "String", ""),
            ("attributes", "Array", "[]"), ("limit", "Number", "100"),
        )
    ]
    rows[0]["choices"] = ["one", "two"]
    # Callback/payload enumeration may return these rows in a different order
    # than the per-command SDK refresh, with every field otherwise identical.
    seeded = [rows[1], rows[0], rows[2], rows[3]]
    current = [copy.deepcopy(rows)]

    def projected(query):
        selection = re.search(r"commandparameters\s*\{([^{}]+)\}", query)
        assert selection
        keys = set(selection.group(1).split())
        return [{key: value for key, value in row.items() if key in keys}
                for row in seeded]

    async def callback_query(_client, query, **_kwargs):
        if surface == "payload_fallback":
            return {"callback": []}
        return {"callback": [{"payload": {"payloadtype": {"name": "apollo"}},
                              "loadedcommands": [{"command": {
                                  "cmd": "ldap_query", "commandparameters": projected(query),
                              }}]}]}

    async def fallback_commands(_client, _payload, attr):
        return [{"cmd": "ldap_query", "commandparameters": projected(attr)}]

    async def sdk_schema(**_kwargs):
        if current[0] is None:
            raise RuntimeError("inert schema read failure")
        return {"Default": {"parameters": copy.deepcopy(current[0])}}

    monkeypatch.setattr(mythic, "execute_custom_query", callback_query)
    monkeypatch.setattr(mythic, "get_all_commands_for_payloadtype", fallback_commands)
    monkeypatch.setattr(mythic, "get_command_parameter_options", sdk_schema)
    monkeypatch.setattr(mythic_utilities, "graphql_post",
                        AsyncMock(return_value={"commandparameters": []}))
    monkeypatch.setattr(tools, "_validate_command_parameters", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    sink = AsyncMock(side_effect=RuntimeError("inert task sink reached"))
    monkeypatch.setattr(tools, "_issue_mythic_task", sink)

    async def probe():
        payload, commands, error = await tools._fetch_live_command_surface(5)
        assert payload == "apollo" and commands and not error
        prepared = await tools._prepare_supervised_issue_action({
            "command": "ldap_query", "callback_display_id": 5, "timeout": 60,
            "parameters": {"query": "(objectClass=computer)",
                           "attributes": ["name,dnshostname"], "limit": "100"},
        })
        action = {"name": TOOL, "args": prepared}
        card = build_approval_request([action])
        assert "ldap_query" in card["description"]
        _claim(tools, action)
        assert tools._approval_effect_blocker(TOOL, prepared) == ""
        if change in {"added", "duplicate"}:
            current[0].append(copy.deepcopy(rows[0]) if change == "duplicate" else {
                **rows[0], "name": "extra", "cli_name": "extra"})
        elif change == "removed":
            current[0].pop(0)
        elif change == "missing":
            current[0] = None
        elif change == "nested_order":
            current[0][0]["choices"].reverse()
        elif change:
            current[0][0][change] = {
                "verifier_regex": "^[A-Z]+$", "required": True,
                "type": "Number", "parameter_group_name": "Other",
                "choices": ["one"], "default_value": "new default",
                "cli_name": "other", "dynamic_query_function": "get_other",
            }[change]
        return await tools.issue_task_and_waitfor_task_output(**prepared)

    result = asyncio.run(probe())
    if change is None:
        assert "schema changed after approval" not in result
        sink.assert_awaited_once()
        assert "inert task sink reached" in result
    else:
        sink.assert_not_awaited()
        assert "schema changed after approval" in result
