"""Task-preparation refusals are repaired inside the worker's turn.

Live shape (supervised, Apollo `execute_assembly`): after `ensure_tool_uploaded` returned a Mythic file UUID, the
worker put that UUID on the registered `Assembly` selector. Preparation refused it with
`invalid ChooseOne param 'Assembly' ... choices: Rubeus.exe, SharpHound.exe`, the refusal became the worker's final
message, and the Supervisor had to re-delegate. These tests run the real preparer and the real approval middleware;
only the Mythic control-plane reads (schema, file metadata, live command) are fixtures.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from ai.langgraph.model import Model, STOP_REASON_TERMINAL_BLOCKER, _TurnAuthorityToolMiddleware
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract

RUBEUS_UUID = "3a9f2a38-5c1e-4b8e-9d2f-7a6b5c4d3e21"

# Apollo `execute_assembly` as Mythic reports it: a registered-name selector group and a new-file upload group.
APOLLO_EXECUTE_ASSEMBLY = [
    {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
     "choices": ["Rubeus.exe", "SharpHound.exe"], "parameter_group_name": "Default", "required": True},
    {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
     "choices": [], "parameter_group_name": "Default", "required": False},
    {"name": "assembly_file", "cli_name": "New Assembly", "type": "File",
     "choices": [], "parameter_group_name": "New Assembly", "required": True},
    {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
     "choices": [], "parameter_group_name": "New Assembly", "required": False},
]


def _client(metadata: dict | None = None):
    client = MythicTools(agent_task_id="test")
    client.client = object()
    uploaded = {
        "rubeus.exe": {"id": 1, "agent_file_id": RUBEUS_UUID, "filename_utf8": "Rubeus.exe", "complete": True},
        "sharphound.exe": {"id": 2, "agent_file_id": "00000000-0000-0000-0000-000000000002",
                           "filename_utf8": "SharpHound.exe", "complete": True},
    }

    async def schema(command, _callback_display_id, **_kwargs):
        return APOLLO_EXECUTE_ASSEMBLY if command == "execute_assembly" else []

    async def find(filename):
        return uploaded.get(str(filename).casefold())

    client._fetch_command_schema = schema
    client._find_uploaded_file_by_name = find
    client._authenticate_live_command = AsyncMock(
        return_value={"status": "available", "command": "execute_assembly"}
    )
    row = {
        "agent_file_id": RUBEUS_UUID, "filename_utf8": "Rubeus.exe", "complete": True,
        "deleted": False, "is_payload": False, "is_download_from_agent": False,
    }
    if metadata is not None:
        row.update(metadata)
    client._get_file_metadata = AsyncMock(
        side_effect=lambda file_uuid: dict(row) if file_uuid == row["agent_file_id"] else None
    )
    client._issue_mythic_task = AsyncMock(side_effect=AssertionError("preparation must not issue a task"))
    return client


def _prepare(client, parameters):
    return asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly", "callback_display_id": 2, "parameters": parameters,
    }))


@pytest.mark.parametrize("key", ["Assembly", "New Assembly"])
def test_a_registered_file_uuid_prepares_to_its_registered_name(key):
    client = _client()
    prepared = _prepare(client, {key: RUBEUS_UUID, "Arguments": "kerberoast /nowrap"})
    assert prepared["parameters"]["Assembly"] == "Rubeus.exe"
    assert prepared["parameters"]["Arguments"] == "kerberoast /nowrap"
    assert RUBEUS_UUID not in str(prepared["parameters"])
    client._issue_mythic_task.assert_not_awaited()


@pytest.mark.parametrize("metadata, uuid", [
    ({"filename_utf8": "Other.exe"}, RUBEUS_UUID),          # a real file, but not a registered choice
    (None, "3a9f2a38-5c1e-4b8e-9d2f-7a6b5c4d3e22"),         # near-match UUID Mythic does not know
    ({"deleted": True}, RUBEUS_UUID),
    ({"is_payload": True}, RUBEUS_UUID),
    ({"complete": False}, RUBEUS_UUID),
    ({"is_download_from_agent": True}, RUBEUS_UUID),
])
def test_a_file_uuid_without_an_exact_registered_name_is_still_refused(metadata, uuid):
    client = _client(metadata)
    with pytest.raises(ValueError, match="invalid ChooseOne param 'Assembly'"):
        _prepare(client, {"Assembly": uuid, "Arguments": "kerberoast"})


def _model(client, *, loop_breaker=None):
    model = Model.__new__(Model)
    model.mode = "supervised"
    model._native_chat_explicit_hitl = True
    model._request_contract = None
    model.mythic_client = client
    model.state = {"_request_id": ""}
    model._loop_breaker = loop_breaker
    model._stop_requested = False
    model.install_request_contract(build_request_contract(
        request_id="inert-request", channel_id="1", operation_id="inert",
        mode="supervised", autonomous_solve=False,
    ))
    return model


def _propose(model, monkeypatch, parameters, call_id="call-1", agent="Mythic_Operator"):
    middleware = _TurnAuthorityToolMiddleware(model, agent_name=agent)
    monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
    message = AIMessage(content="", tool_calls=[{
        "name": "issue_task_and_waitfor_task_output",
        "args": {"command": "execute_assembly", "callback_display_id": 2, "parameters": parameters},
        "id": call_id, "type": "tool_call",
    }])
    update = asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    return update or {}, message


@pytest.mark.parametrize("parameters, refusal", [
    ({"Arguments": "kerberoast"}, "missing required param(s) for group 'Default': Assembly"),
    ({"Assembly": "3a9f2a38-0000-0000-0000-000000000000", "Arguments": "kerberoast"},
     "invalid ChooseOne param 'Assembly'"),
])
def test_a_schema_refusal_returns_to_the_worker_within_the_repair_budget(monkeypatch, parameters, refusal):
    client = _client()
    model = _model(client)
    for _ in range(3):
        update, message = _propose(model, monkeypatch, parameters)
        assert update.get("jump_to") == "model"
        assert message.tool_calls == []
        assert "[task preparation] Approval was not requested" in message.content
        assert refusal in message.content
    # Budget spent: the note still reaches the operator, and the turn is not re-run again.
    update, message = _propose(model, monkeypatch, parameters)
    assert "jump_to" not in update
    assert refusal in message.content
    client._issue_mythic_task.assert_not_awaited()


def test_a_schema_refusal_is_not_re_run_for_another_agent_or_a_stopped_request(monkeypatch):
    client = _client()
    model = _model(client)
    assert "jump_to" not in _propose(model, monkeypatch, {"Arguments": "x"}, agent="Generalist")[0]
    model.install_request_contract(model._request_contract.stop())
    assert "jump_to" not in _propose(model, monkeypatch, {"Arguments": "x"})[0]


def test_the_same_schema_refusal_repeated_without_progress_trips_the_loop_breaker(monkeypatch):
    from ai.langgraph import worker_outcome

    client = _client()
    model = _model(client, loop_breaker=worker_outcome.LoopBreakerState())
    assert _propose(model, monkeypatch, {"Arguments": "x"}, "call-1")[0]["jump_to"] == "model"
    assert model._stop_requested is False
    halted = _propose(model, monkeypatch, {"Arguments": "x"}, "call-2")[0]
    assert model._stop_requested is True
    assert model._stop_reason == STOP_REASON_TERMINAL_BLOCKER
    assert "jump_to" not in halted


# --- an enrolled executable reached by file UUID must prepare exactly as it does by name ---------------------------

CERTIFY_UUID = "00000000-0000-0000-0000-000000000001"


def _certify(tmp_path, monkeypatch):
    from tests.test_executable_invocation_integration import _fixture

    client, *_ = _fixture(tmp_path, monkeypatch)
    client._get_file_metadata = AsyncMock(side_effect=lambda file_uuid: {
        "id": 1, "agent_file_id": CERTIFY_UUID, "filename_utf8": "Certify.exe", "complete": True,
        "deleted": False, "is_payload": False, "is_download_from_agent": False,
    } if file_uuid == CERTIFY_UUID else None)
    return client


def _prepare_certify(client, assembly, arguments):
    return asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly", "callback_display_id": 7,
        "parameters": {"Assembly": assembly, "Arguments": arguments},
    }))


def test_an_enrolled_executable_by_uuid_prepares_the_same_card_as_by_name(tmp_path, monkeypatch):
    (tmp_path / "by-name").mkdir()
    (tmp_path / "by-uuid").mkdir()
    by_name = _prepare_certify(_certify(tmp_path / "by-name", monkeypatch), "Certify.exe", "cas --filter-vulnerable")
    by_uuid = _prepare_certify(_certify(tmp_path / "by-uuid", monkeypatch), CERTIFY_UUID, "cas --filter-vulnerable")
    assert by_uuid["parameters"] == by_name["parameters"]
    assert by_uuid["parameters"]["Arguments"].startswith("enum-cas")
    assert by_uuid["visibility_context"]["_sage_prepared_binding"]["executable"] == \
        by_name["visibility_context"]["_sage_prepared_binding"]["executable"]


def test_an_enrolled_executable_approved_by_uuid_issues_the_action_its_card_showed(tmp_path, monkeypatch):
    from unittest.mock import patch

    from ai.langgraph import mythic_tools

    client = _certify(tmp_path, monkeypatch)
    enrolled = asyncio.run(client._select_enrolled_executable(
        "apollo", "execute_assembly", 7, "Certify.exe", "enum-cas"
    ))
    assert enrolled["status"] == "enrolled"
    prepared = _prepare_certify(client, CERTIFY_UUID, {"verb": "enum-cas", "options": {}})
    assert prepared["parameters"] == {"Assembly": "Certify.exe", "Arguments": "enum-cas"}
    with patch.object(client, "_turn_authority_issue_blocker", return_value=""), patch.object(
        client, "_issue_mythic_task", AsyncMock(return_value={"display_id": 11}),
    ) as issue, patch.object(mythic_tools.mythic, "waitfor_for_task_output", AsyncMock(return_value="enumerated")):
        result = asyncio.run(client.issue_task_and_waitfor_task_output(**prepared))
    assert "enumerated" in result
    assert issue.call_args.args[:3] == ("execute_assembly", prepared["parameters"], 7)


def test_an_enrolled_executable_by_uuid_with_an_unenrolled_verb_is_refused_before_a_card(tmp_path, monkeypatch):
    from ai.langgraph.executable_contracts import UnsupportedExecutableVerbError

    with pytest.raises(UnsupportedExecutableVerbError):
        _prepare_certify(_certify(tmp_path, monkeypatch), CERTIFY_UUID, "forge")


def test_a_uuid_for_an_older_upload_of_the_same_name_is_refused():
    """The card binds the file the name resolves to; a UUID naming a different upload of that name is not it."""
    client = _client()
    older = "3a9f2a38-1111-4b8e-9d2f-7a6b5c4d3e21"
    client._get_file_metadata = AsyncMock(side_effect=lambda file_uuid: {
        "agent_file_id": older, "filename_utf8": "Rubeus.exe", "complete": True,
        "deleted": False, "is_payload": False, "is_download_from_agent": False,
    } if file_uuid == older else None)
    with pytest.raises(ValueError, match="invalid ChooseOne param 'Assembly'"):
        _prepare(client, {"Assembly": older, "Arguments": "kerberoast"})


SHARPHOUND_UUID = "00000000-0000-0000-0000-000000000002"


def _sharphound(tmp_path, monkeypatch):
    from tests.test_executable_invocation_integration import _fixture

    client, _descriptor, _root, rows, _by_uuid = _fixture(
        tmp_path, monkeypatch, filenames=("Certify.exe", "SharpHound.exe"),
        contents={"Certify.exe": b"same exact enrolled bytes", "SharpHound.exe": b"other bytes"},
    )
    row = dict(rows["SharpHound.exe"], deleted=False, is_payload=False, is_download_from_agent=False)
    assert row["agent_file_id"] == SHARPHOUND_UUID
    client._get_file_metadata = AsyncMock(side_effect=lambda file_uuid: dict(row) if file_uuid == SHARPHOUND_UUID else None)
    return client


@pytest.mark.parametrize("assembly", ["SharpHound.exe", SHARPHOUND_UUID])
def test_sharphound_by_uuid_shows_and_issues_the_same_arguments_as_by_name(tmp_path, monkeypatch, assembly):
    from unittest.mock import patch

    from ai.langgraph import mythic_tools

    client = _sharphound(tmp_path, monkeypatch)
    prepared = _prepare_certify(client, assembly, "-c All -o C:\\temp")
    assert prepared["parameters"] == {"Assembly": "SharpHound.exe", "Arguments": "-c All --OutputDirectory C:\\temp"}
    with patch.object(client, "_turn_authority_issue_blocker", return_value=""), patch.object(
        client, "_issue_mythic_task", AsyncMock(return_value={"display_id": 11}),
    ) as issue, patch.object(mythic_tools.mythic, "waitfor_for_task_output", AsyncMock(return_value="ok")):
        asyncio.run(client.issue_task_and_waitfor_task_output(**prepared))
    assert issue.call_args.args[:3] == ("execute_assembly", prepared["parameters"], 7)


def test_mixed_selector_and_upload_parameters_are_refused_by_uuid_as_by_name(tmp_path, monkeypatch):
    (tmp_path / "by-name").mkdir()
    (tmp_path / "by-uuid").mkdir()
    extra = {"Arguments": "enum-cas", "New Assembly": "3a9f2a38-2222-4b8e-9d2f-7a6b5c4d3e21"}
    with pytest.raises(ValueError) as by_name:
        asyncio.run(_certify(tmp_path / "by-name", monkeypatch)._prepare_supervised_issue_action({
            "command": "execute_assembly", "callback_display_id": 7,
            "parameters": {"Assembly": "Certify.exe", **extra},
        }))
    with pytest.raises(ValueError) as by_uuid:
        asyncio.run(_certify(tmp_path / "by-uuid", monkeypatch)._prepare_supervised_issue_action({
            "command": "execute_assembly", "callback_display_id": 7,
            "parameters": {"Assembly": CERTIFY_UUID, **extra},
        }))
    assert str(by_uuid.value) == str(by_name.value)
