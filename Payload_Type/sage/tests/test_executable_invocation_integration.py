"""Assembled fake-control-plane coverage for enrolled executable invocation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from ai.langgraph import command_builder, command_parameter_schema, executable_contracts, mythic_tools
from ai.langgraph.model import Model, STOP_REASON_TERMINAL_BLOCKER, _TurnAuthorityToolMiddleware
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract


SAGE_ROOT = Path(__file__).resolve().parents[1]
CERTIFY_SOURCE = SAGE_ROOT / "ttps" / "contracts" / "certify.json"


def _schema(*filenames: str):
    return [
        {
            "name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
            "choices": list(filenames), "parameter_group_name": "Default", "required": True,
        },
        {
            "name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
            "choices": [], "parameter_group_name": "Default", "required": False,
        },
        {
            "name": "assembly_file", "cli_name": "New Assembly", "type": "File",
            "choices": [], "parameter_group_name": "New Assembly", "required": True,
        },
        {
            "name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
            "choices": [], "parameter_group_name": "New Assembly", "required": False,
        },
    ]


def _apollo_dynamic_schema():
    """Apollo's real execute_assembly projection: get_files populates empty choices."""
    schema = _schema()
    schema[0]["dynamic_query_function"] = "get_files"
    return schema


def test_apollo_dynamic_choices_enroll_and_reject_changed_choices(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._fetch_command_schema = MythicTools._fetch_command_schema.__get__(client, MythicTools)
    monkeypatch.setattr(command_parameter_schema, "DEFAULT_RESOLVER",
                        command_parameter_schema.CommandSchemaResolver())
    raw = _apollo_dynamic_schema()
    for index, param in enumerate(raw):
        param.update(id=65 + index, ui_position=index % 2 + 1,
                     description="Captured Apollo command parameter",
                     display_name=param["cli_name"], default_value="", verifier_regex="")
        param.setdefault("dynamic_query_function", "")
    producer = {
        "Default": {"example": "Apollo dynamic selector", "parameters": raw[:2]},
        "New Assembly": {"example": "Apollo file upload", "parameters": raw[2:]},
    }
    monkeypatch.setattr(mythic_tools.mythic, "get_command_parameter_options",
                        AsyncMock(return_value=producer))
    choices = ["Certify.exe"]

    async def dynamic_query(_client, query, variables):
        assert "dynamicQueryFunction" in query
        assert variables == {
            "callback_display_id": 7, "command": "execute_assembly",
            "payload_type": "apollo", "parameter_name": "assembly_name",
            "other_parameters": {},
        }
        return {"dynamicQueryFunction": {
            "status": "success", "parameter_name": "assembly_name",
            "choices": list(choices), "complex_choices": [],
        }}

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", dynamic_query)
    enrolled = asyncio.run(client._select_enrolled_executable(
        "apollo", "execute_assembly", 7, "Certify.exe", "enum-cas"
    ))
    assert enrolled["status"] == "enrolled"
    prepared = asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly", "callback_display_id": 7,
        "parameters": {"Assembly": "Certify.exe", "Arguments": {
            "verb": "enum-cas", "options": {},
        }},
    }))
    assert prepared["parameters"] == {
        "Assembly": "Certify.exe", "Arguments": "enum-cas",
    }
    cached = client._cmd_schema_cache[("apollo", "execute_assembly")]
    assert cached[0]["choices"] == []
    with patch.object(client, "_turn_authority_issue_blocker", return_value=""), patch.object(
        client, "_issue_mythic_task", AsyncMock(return_value={"display_id": 11})
    ) as issue, patch.object(mythic_tools.mythic, "waitfor_for_task_output", AsyncMock(
        return_value="enumerated"
    )):
        result = asyncio.run(client.issue_task_and_waitfor_task_output(**prepared))
    assert "enumerated" in result
    assert issue.call_count == 1
    assert issue.call_args.args[:3] == (
        "execute_assembly", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
    )
    assert cached[0]["choices"] == []
    choices.clear()
    with patch.object(client, "_turn_authority_issue_blocker", return_value=""), patch.object(
        mythic_tools.mythic, "issue_task", AsyncMock()
    ) as issue:
        result = asyncio.run(client.issue_task_and_waitfor_task_output(**prepared))
    assert "schema changed after approval" in result
    issue.assert_not_called()


@pytest.mark.parametrize("response", [
    {"status": "error", "parameter_name": "assembly_name", "choices": ["Certify.exe"]},
    {"status": "success", "parameter_name": "assembly_name", "choices": []},
    {"status": "success", "parameter_name": "wrong", "choices": ["Certify.exe"]},
    {"status": "success", "parameter_name": "assembly_name", "choices": [1]},
    {"status": "success", "parameter_name": "assembly_name", "choices": ["Other.exe"]},
])
def test_apollo_dynamic_choices_deny_missing_or_invalid_selection(tmp_path, monkeypatch, response):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._fetch_command_schema = AsyncMock(return_value=_apollo_dynamic_schema())
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", AsyncMock(
        return_value={"dynamicQueryFunction": response}
    ))
    result = asyncio.run(client._select_enrolled_executable(
        "apollo", "execute_assembly", 7, "Certify.exe", "enum-cas"
    ))
    assert result["status"] == "unsupported"
    assert client.get_enrolled_issue_schema() is None


def test_dynamic_selector_reaches_existing_verb_contract(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._fetch_command_schema = AsyncMock(return_value=_apollo_dynamic_schema())
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", AsyncMock(
        return_value={"dynamicQueryFunction": {
            "status": "success", "parameter_name": "assembly_name", "choices": ["Certify.exe"],
        }}
    ))
    result = asyncio.run(client._select_enrolled_executable(
        "apollo", "execute_assembly", 7, "Certify.exe", "forge"
    ))
    assert result["status"] == "unsupported"
    assert "verb" in result["error"].lower()
    assert "enrolled verbs for this artifact: enum-cas, enum-templates, request, request-download" in result["error"]
    assert client.get_enrolled_issue_schema() is None


def test_invalid_certify_verb_fails_supervised_preparation_before_task(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._issue_mythic_task = AsyncMock()
    with pytest.raises(executable_contracts.UnsupportedExecutableError) as caught:
        asyncio.run(client._prepare_supervised_issue_action({
            "command": "execute_assembly", "callback_display_id": 7,
            "parameters": {"Assembly": "Certify.exe", "Arguments": {
                "verb": "forge", "options": {},
            }},
        }))
    assert "enrolled verbs for this artifact: enum-cas, enum-templates, request, request-download" in str(caught.value)
    prepared = asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly", "callback_display_id": 7,
        "parameters": {"Assembly": "Certify.exe", "Arguments": {
            "verb": "enum-templates", "options": {"filter-vulnerable": True},
        }},
    }))
    assert prepared["parameters"] == {
        "Assembly": "Certify.exe", "Arguments": "enum-templates --filter-vulnerable",
    }
    client._issue_mythic_task.assert_not_awaited()


def test_legacy_certify_verb_is_repaired_through_supervised_preparation(tmp_path, monkeypatch):
    # The regression that killed the channel-3 ADCS solve: the Operator proposed the
    # legacy Certify verb `cas`. A declared contract alias now rewrites it to the
    # enrolled `enum-cas` before the approval card, with auditable provenance — the
    # action prepares normally instead of looping on an unenrolled-verb rejection.
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._issue_mythic_task = AsyncMock()
    prepared = asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly", "callback_display_id": 7,
        "parameters": {"Assembly": "Certify.exe", "Arguments": {
            "verb": "cas", "options": {"filter-vulnerable": True},
        }},
    }))
    assert prepared["parameters"] == {
        "Assembly": "Certify.exe", "Arguments": "enum-cas --filter-vulnerable",
    }
    binding = prepared["visibility_context"]["_sage_prepared_binding"]["executable"]
    assert binding["repaired_from"] == "cas"
    assert binding["invocation"]["verb"] == "enum-cas"
    client._issue_mythic_task.assert_not_awaited()


def test_legacy_certify_option_syntax_is_normalized_through_supervised_preparation(tmp_path, monkeypatch):
    # The channel-3 (2026-10-18) residual: the Operator proposed a raw Certify argument string using
    # legacy slash-option syntax `/domain:essos.local`, which looped on an "unexpected executable
    # argument" rejection (verb-only aliasing did not cover options). The Normalizer rewrites the
    # legacy option syntax to the enrolled `--domain essos.local` before the approval card, with
    # auditable provenance, so the action prepares instead of looping.
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._issue_mythic_task = AsyncMock()
    raw = "enum-cas /domain:essos.local --filter-vulnerable"
    prepared = asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly", "callback_display_id": 7,
        "parameters": {"Assembly": "Certify.exe", "Arguments": raw},
    }))
    args = prepared["parameters"]["Arguments"]
    assert "--domain essos.local" in args
    assert "--filter-vulnerable" in args
    assert "/domain:" not in args
    binding = prepared["visibility_context"]["_sage_prepared_binding"]["executable"]
    assert binding["normalized_from"] == raw
    assert binding["invocation"]["verb"] == "enum-cas"
    client._issue_mythic_task.assert_not_awaited()


def test_invalid_certify_verb_feedback_reaches_model_without_approval(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._issue_mythic_task = AsyncMock()
    model = SimpleNamespace(mode="supervised", _native_chat_explicit_hitl=True,
                            mythic_client=client)
    middleware = _TurnAuthorityToolMiddleware(model, agent_name="Mythic_Operator")
    monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
    message = AIMessage(content="", tool_calls=[{
        "name": "issue_task_and_waitfor_task_output",
        "args": {"command": "execute_assembly", "callback_display_id": 7,
                 "parameters": {"Assembly": "Certify.exe", "Arguments": "forge"}},
        "id": "invalid-certify-verb", "type": "tool_call",
    }])
    asyncio.run(middleware.aafter_model({"messages": [message]}, None))
    assert message.tool_calls == []
    assert "Approval was not requested" in message.content
    assert "enrolled verbs for this artifact: enum-cas, enum-templates, request, request-download" in message.content
    client._issue_mythic_task.assert_not_awaited()


def test_unsupported_verb_repair_budget_is_bounded_and_request_scoped(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._issue_mythic_task = AsyncMock()
    model = Model.__new__(Model)
    model.mode = "supervised"
    model._native_chat_explicit_hitl = True
    model._request_contract = None
    model.mythic_client = client
    model.state = {"_request_id": ""}
    # Isolate the Tier-2 budget here; the Tier-3 loop-breaker accrual is exercised separately below.
    model._loop_breaker = None

    def install(request_id):
        model.install_request_contract(build_request_contract(
            request_id=request_id, channel_id="1", operation_id="inert",
            mode="supervised", autonomous_solve=False,
        ))

    def prepare(verb, *, sibling=False, agent="Mythic_Operator"):
        # A fresh middleware per call proves the budget lives on the Model's request-scoped ledger, not on
        # any middleware instance: a Supervisor↔Operator handoff rebuilds the middleware but keeps the count.
        middleware = _TurnAuthorityToolMiddleware(model, agent_name=agent)
        monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
        message = AIMessage(content="", tool_calls=[{
            "name": "issue_task_and_waitfor_task_output",
            "args": {"command": "execute_assembly", "callback_display_id": 7,
                     "parameters": {"Assembly": "Certify.exe", "Arguments": {
                         "verb": verb, "options": {},
                     }}},
            "id": "inert-preparation", "type": "tool_call",
        }])
        if sibling:
            message.tool_calls.append({
                "name": "get_all_command_args_for_payloadtype",
                "args": {"payload": "apollo", "command": "execute_assembly",
                         "callback_display_id": 7},
                "id": "inert-read-only-sibling", "type": "tool_call",
            })
        update = asyncio.run(middleware.aafter_model({"messages": [message]}, None))
        return update, message

    install("inert-request-1")
    assert "jump_to" not in prepare(7)[0]  # A malformed verb type is terminal, not an unsupported verb.

    # Each unenrolled verb earns a forced re-run until the bounded budget is spent. The budget survives a
    # rebuilt middleware and a same-id contract amend (both keep the logical request_id, hence the ledger).
    first, first_message = prepare("forge")
    assert first["jump_to"] == "model"
    assert first_message.tool_calls == []
    assert "Approval was not requested" in first_message.content
    assert prepare("forge")[0]["jump_to"] == "model"
    model.install_request_contract(model._request_contract.amend())
    assert prepare("forge")[0]["jump_to"] == "model"
    # _UNSUPPORTED_VERB_REPAIR_BUDGET forced re-runs spent -> no further jump this request.
    assert "jump_to" not in prepare("forge")[0]
    assert "jump_to" not in prepare(7)[0]

    # A terminal request never forces a re-run even with budget nominally left.
    install("inert-request-2")
    model.install_request_contract(model._request_contract.stop())
    assert "jump_to" not in prepare("forge")[0]

    # A mixed turn that keeps a read-only sibling must not force a re-run (and so must not burn budget):
    # the next request gets its full allowance.
    install("inert-request-3")
    mixed, mixed_message = prepare("forge", sibling=True)
    assert "jump_to" not in mixed
    assert [call["name"] for call in mixed_message.tool_calls] == [
        "get_all_command_args_for_payloadtype"]
    assert prepare("forge")[0]["jump_to"] == "model"
    assert prepare("forge")[0]["jump_to"] == "model"
    assert prepare("forge")[0]["jump_to"] == "model"
    assert "jump_to" not in prepare("forge")[0]

    # A new logical request resets the budget.
    install("inert-request-4")
    assert prepare("forge")[0]["jump_to"] == "model"
    client._issue_mythic_task.assert_not_awaited()


def test_repeated_unsupported_verb_accrues_to_loop_breaker_and_halts(tmp_path, monkeypatch):
    """Tier 3: a task-prep rejection is invisible to the issued-task path, so it must feed the control-state
    loop-breaker directly. The SAME unenrolled verb re-proposed with no progress trips a clean terminal
    halt (the channel-3 Certify `cas` loop), while distinct verbs stay first-sight and are bounded only by
    the repair budget."""
    from ai.langgraph import worker_outcome as _wo

    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._issue_mythic_task = AsyncMock()
    model = Model.__new__(Model)
    model.mode = "supervised"
    model._native_chat_explicit_hitl = True
    model._request_contract = None
    model.mythic_client = client
    model.state = {"_request_id": ""}
    model._loop_breaker = _wo.LoopBreakerState()
    model._stop_requested = False
    model.install_request_contract(build_request_contract(
        request_id="inert-request", channel_id="1", operation_id="inert",
        mode="supervised", autonomous_solve=False,
    ))

    def prepare(verb, call_id):
        # Distinct ids model the fresh tool-call id a real re-proposal carries (the breaker's per-turn
        # dedup key); reusing one id would be a same-turn re-fire and must not count.
        middleware = _TurnAuthorityToolMiddleware(model, agent_name="Mythic_Operator")
        monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
        message = AIMessage(content="", tool_calls=[{
            "name": "issue_task_and_waitfor_task_output",
            "args": {"command": "execute_assembly", "callback_display_id": 7,
                     "parameters": {"Assembly": "Certify.exe", "Arguments": {
                         "verb": verb, "options": {},
                     }}},
            "id": call_id, "type": "tool_call",
        }])
        return asyncio.run(middleware.aafter_model({"messages": [message]}, None))

    # First sight of this blocker: forced re-run, solve continues.
    assert prepare("forge", "call-1")["jump_to"] == "model"
    assert model._stop_requested is False
    # Same unenrolled verb, fresh id, no progress since -> terminal blocker halt, no further re-run.
    halted = prepare("forge", "call-2")
    assert model._stop_requested is True
    assert model._stop_reason == STOP_REASON_TERMINAL_BLOCKER
    assert "jump_to" not in halted
    client._issue_mythic_task.assert_not_awaited()


def test_unsupported_verb_retry_requires_active_request_projection(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    model = Model.__new__(Model)
    model.mode = "supervised"
    model._native_chat_explicit_hitl = True
    model._request_contract = None
    model.mythic_client = client
    model.state = {"_request_id": ""}
    model.install_request_contract(build_request_contract(
        request_id="inert-request", channel_id="1", operation_id="inert",
        mode="supervised", autonomous_solve=False,
    ))

    def attempt():
        middleware = _TurnAuthorityToolMiddleware(model, agent_name="Mythic_Operator")
        monkeypatch.setattr(middleware, "_after_model_update", lambda state: None)
        message = AIMessage(content="", tool_calls=[{
            "name": "issue_task_and_waitfor_task_output",
            "args": {"command": "execute_assembly", "callback_display_id": 7,
                     "parameters": {"Assembly": "Certify.exe", "Arguments": "forge"}},
            "id": "inert-preparation", "type": "tool_call",
        }])
        return asyncio.run(middleware.aafter_model({"messages": [message]}, None))

    model.state["_request_id"] = "other-request"
    assert "jump_to" not in attempt()
    model.state["_request_id"] = "inert-request"
    model._stop_requested = True
    assert "jump_to" not in attempt()
    model._stop_requested = False
    model._turn_authority = None
    assert "jump_to" not in attempt()
    model.install_request_contract(model._request_contract.stop())
    assert "jump_to" not in attempt()
    # None of the refused projections spent the repair budget, so the ledger recorded no retry.
    assert not model._ensure_request_event_ledger("inert-request").actual_events(
        kind="unsupported_verb_repair", phase="retry")


def test_dynamic_choice_query_failure_denies_before_file_download(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._fetch_command_schema = AsyncMock(return_value=_apollo_dynamic_schema())
    client._download_registered_file_bytes_bounded.reset_mock()
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", AsyncMock(
        side_effect=RuntimeError("control plane unavailable")
    ))
    result = asyncio.run(client._select_enrolled_executable(
        "apollo", "execute_assembly", 7, "Certify.exe", "enum-cas"
    ))
    assert result["status"] == "unsupported"
    assert "choices are unavailable" in result["error"]
    client._download_registered_file_bytes_bounded.assert_not_called()


def _fixture(tmp_path: Path, monkeypatch, *, filenames=("Certify.exe",), contents=None):
    contents = contents or {name: b"same exact enrolled bytes" for name in filenames}
    enrolled_sha1 = hashlib.sha1(next(iter(contents.values()))).hexdigest()
    descriptor = json.loads(CERTIFY_SOURCE.read_text(encoding="utf-8"))
    descriptor["binary"]["sha1"] = enrolled_sha1
    root = tmp_path / "contracts"
    root.mkdir()
    (root / "certify.json").write_text(json.dumps(descriptor), encoding="utf-8")
    monkeypatch.setattr(mythic_tools, "_EXECUTABLE_CONTRACT_ROOT", root)

    rows = {
        name: {
            "id": index + 1,
            "agent_file_id": f"00000000-0000-0000-0000-{index + 1:012d}",
            "filename_utf8": name,
            "md5": hashlib.md5(content).hexdigest(),
            "sha1": hashlib.sha1(content).hexdigest(),
            "complete": True,
        }
        for index, (name, content) in enumerate(contents.items())
    }
    by_uuid = {rows[name]["agent_file_id"]: content for name, content in contents.items()}

    client = MythicTools(agent_task_id="executable-integration")
    client.client = SimpleNamespace(http="https://", server_ip="mythic.test", server_port=443)
    client._resolve_payload_type = AsyncMock(return_value="apollo")
    client._authenticate_live_command = AsyncMock(
        return_value={"status": "available", "command": "execute_assembly"}
    )
    client._fetch_command_schema = AsyncMock(return_value=_schema(*filenames))
    client._fetch_command_metadata = AsyncMock(return_value={})
    client._find_uploaded_file_by_name = AsyncMock(side_effect=lambda name: copy.deepcopy(rows.get(name)))
    client._download_registered_file_bytes_bounded = AsyncMock(
        side_effect=lambda agent_file_id: by_uuid[agent_file_id]
    )
    client._callback_tasking_liveness_blocker = AsyncMock(return_value=None)
    client._validate_command_parameters = AsyncMock(return_value=None)
    return client, descriptor, root, rows, by_uuid


def test_discovery_emits_full_strict_schema_and_prepare_renders_native_parameters(tmp_path, monkeypatch):
    client, descriptor, _, _, _ = _fixture(tmp_path, monkeypatch)

    async def grouped(*_args, **_kwargs):
        return {"Default": {"parameters": _schema("Certify.exe")[:2]}}

    monkeypatch.setattr(mythic_tools.command_parameter_schema, "get_command_schema", grouped)
    result = json.loads(asyncio.run(client.get_all_command_args_for_payloadtype(
        "apollo", "execute_assembly", 7, "Certify.exe", "request"
    )))
    assert result["executable_contract"]["status"] == "enrolled"
    schema = client.get_enrolled_issue_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["command"]["enum"] == ["execute_assembly"]
    assert schema["properties"]["callback_display_id"]["enum"] == [7]
    parameters = schema["properties"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["Assembly"]["enum"] == ["Certify.exe"]
    invocation = parameters["properties"]["Arguments"]
    assert invocation["properties"]["verb"]["enum"] == ["request"]
    options = invocation["properties"]["options"]
    assert set(options["required"]) == set(options["properties"])
    assert options["properties"]["template"]["type"] == "string"
    assert {item["type"] for item in options["properties"]["output-pem"]["anyOf"]} == {
        "boolean", "null"
    }

    prepared = asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly",
        "callback_display_id": 7,
        "parameters": {
            "Assembly": "Certify.exe",
            "Arguments": {
                "verb": "request",
                "options": {
                    **{key: None for key in options["properties"]},
                    "ca": r"SERVER\CA",
                    "template": "Template One",
                    "upn": ["one@example.test", "two@example.test"],
                    "output-pem": True,
                },
            },
        },
    }))
    assert prepared["parameters"] == {
        "Assembly": "Certify.exe",
        "Arguments": (
            'request --ca SERVER\\CA --template "Template One" --upn '
            "one@example.test two@example.test --key-size 2048 --output-pem"
        ),
    }
    binding = prepared["visibility_context"]["_sage_prepared_binding"]["executable"]
    assert binding["invocation"]["artifact_sha1"] == descriptor["binary"]["sha1"]
    assert binding["invocation"]["argv"][0] == "request"


@pytest.mark.parametrize(
    "parameters, expected",
    [
        ("Certify.exe enum-cas --filter-vulnerable", "enum-cas --filter-vulnerable"),
        ('{"Assembly":"Certify.exe","Arguments":"enum-cas --filter-vulnerable"}', None),
        ({
            "Assembly": "Certify.exe",
            "Arguments": {"verb": "request-download", "options": {"ca": r"S\C", "id": 4}},
        }, "request-download --ca S\\C --id 4"),
    ],
)
def test_raw_outer_json_string_and_typed_forms_normalize_before_shared_sink(
    tmp_path, monkeypatch, parameters, expected
):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    if isinstance(parameters, str) and parameters.startswith("{"):
        parameters = json.loads(parameters)
        expected = "enum-cas --filter-vulnerable"
    normalized, binding = asyncio.run(client._validated_executable_parameters(
        "execute_assembly", parameters, 7, _schema("Certify.exe")
    ))
    assert normalized["Assembly"] == "Certify.exe"
    assert normalized["Arguments"] == expected
    assert binding["invocation"]["argument_string"] == expected


@pytest.mark.parametrize(
    "parameters",
    [
        "Certify.exe forge",
        "Certify.exe forge ",
        "Certify.exe forge\n",
        {"Assembly": "Certify.exe", "Arguments": "request --ca S\\C"},
        {"Assembly": "Certify.exe", "Arguments": "enum-cas", "other": "mixed"},
    ],
)
def test_known_raw_legacy_malicious_and_mixed_forms_fail_before_native_issue(tmp_path, monkeypatch, parameters):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    seen = {"issued": 0}

    async def fake_issue(*_args, **_kwargs):
        seen["issued"] += 1
        return {"display_id": 10}

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue):
        result = asyncio.run(client.issue_task_and_waitfor_task_output(
            "execute_assembly", parameters, 7
        ))
    assert result.startswith("STOP") or "construction failure" in result.lower()
    if isinstance(parameters, str) and parameters.startswith("Certify.exe forge") and "\n" not in parameters:
        assert "enrolled verbs for this artifact: enum-cas, enum-templates, request, request-download" in result
    assert seen["issued"] == 0


def test_filename_identity_accepts_a_rebuilt_binary_and_ignores_an_unclaimed_name(tmp_path, monkeypatch):
    # #56: contract identity is the filename, not the bytes.
    exact = b"same exact enrolled bytes"
    client, _, root, rows, _ = _fixture(
        tmp_path, monkeypatch,
        filenames=("Certify.exe", "alias"),
        contents={"Certify.exe": exact, "alias": exact},
    )
    # An unclaimed filename is un-contracted (no typed binding), even with enrolled bytes.
    _normalized, binding = asyncio.run(client._validated_executable_parameters(
        "execute_assembly", {"Assembly": "alias", "Arguments": "enum-cas"}, 7,
        _schema("Certify.exe", "alias"),
    ))
    assert binding is None

    # A CI-rebuilt Certify.exe (same name, DIFFERENT bytes/hash) resolves its contract and validates,
    # instead of being hard-blocked -- the core #56 fix. The rebuild is simulated by Mythic's file
    # metadata reporting a NEW SHA-1 (the binary is never downloaded/rehashed). The binding records
    # that observed SHA-1.
    rebuilt_sha1 = hashlib.sha1(b"freshly rebuilt certify bytes with a new hash").hexdigest()
    assert rebuilt_sha1 != hashlib.sha1(exact).hexdigest()
    rows["Certify.exe"]["sha1"] = rebuilt_sha1
    client._find_uploaded_file_by_name = AsyncMock(
        side_effect=lambda name: copy.deepcopy(rows.get(name))
    )
    _normalized, binding = asyncio.run(client._validated_executable_parameters(
        "execute_assembly", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
        _schema("Certify.exe", "alias"),
    ))
    assert binding["file"]["filename_utf8"] == "Certify.exe"
    assert binding["file"]["sha1"] == rebuilt_sha1
    assert binding["invocation"]["verb"] == "enum-cas"


def _assert_async_rejected(coroutine, *needles):
    with pytest.raises((ValueError, executable_contracts.ExecutableContractError)) as caught:
        asyncio.run(coroutine)
    for needle in needles:
        assert needle in str(caught.value).lower()


def test_metadata_or_schema_failure_cannot_make_known_or_renamed_artifact_generic(tmp_path, monkeypatch):
    client, _, _, rows, _ = _fixture(
        tmp_path, monkeypatch, filenames=("Certify.exe", "renamed-tool")
    )
    client._authenticate_live_command = AsyncMock(
        side_effect=lambda command, _callback: {"status": "available", "command": command}
    )
    # The contract path never downloads the binary; identity is the filename plus Mythic's own SHA-1
    # metadata. A KNOWN (contracted) artifact whose registered row lacks a well-formed SHA-1 must fail
    # closed, not fall through to a generic handling that would bypass the typed schema.
    rows["Certify.exe"]["sha1"] = ""
    client._find_uploaded_file_by_name = AsyncMock(
        side_effect=lambda name: copy.deepcopy(rows.get(name))
    )
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", "Certify.exe enum-cas", 7, _schema("Certify.exe")
        ), "identity"
    )
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", "Certify.exe enum-cas", 7, None
        ), "schema", "unavailable"
    )


def test_ambiguous_wrapper_cannot_hide_known_artifact_in_raw_or_structured_form(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    ambiguous = _schema("Certify.exe")
    ambiguous.append({
        "name": "extra_text", "cli_name": "Extra", "type": "String", "choices": [],
        "parameter_group_name": "Default", "required": False,
    })
    for parameters in (
        "Certify.exe enum-cas",
        {"Assembly": "Certify.exe", "Arguments": "enum-cas"},
    ):
        _assert_async_rejected(
            client._validated_executable_parameters(
                "execute_assembly", parameters, 7, ambiguous
            ), "ambiguous", "wrapper"
        )
def test_unknown_positive_artifact_preserves_generic_and_non_wrapper_is_unaffected(tmp_path, monkeypatch):
    client, _, _, rows, by_uuid = _fixture(tmp_path, monkeypatch)
    unknown = b"independently downloaded unknown binary"
    rows["other"] = {
        "id": 8, "agent_file_id": "00000000-0000-0000-0000-000000000008",
        "filename_utf8": "other", "complete": True,
    }
    by_uuid[rows["other"]["agent_file_id"]] = unknown
    client._find_uploaded_file_by_name = AsyncMock(side_effect=lambda name: copy.deepcopy(rows.get(name)))
    client._download_registered_file_bytes_bounded = AsyncMock(side_effect=lambda uuid: by_uuid[uuid])
    parameters = "other arbitrary --raw"
    assert asyncio.run(client._validated_executable_parameters(
        "execute_assembly", parameters, 7, _schema("other")
    )) == (parameters, None)
    client._fetch_command_schema = AsyncMock(return_value=None)
    assert asyncio.run(client._validated_executable_parameters("whoami", "", 7, None)) == ("", None)


def test_only_apollo_execute_and_inline_assembly_transport_are_enrolled(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._resolve_payload_type = AsyncMock(return_value="merlin")
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
            _schema("Certify.exe"),
        ), "only for apollo"
    )
    assert asyncio.run(client._validated_executable_parameters(
        "execute_pe", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
        _schema("Certify.exe"),
    ))[1] is None


def test_parser_output_is_construction_only_for_exact_enrolled_binding(tmp_path, monkeypatch):
    exact = b"same exact enrolled bytes"
    client, _, _, _, _ = _fixture(
        tmp_path, monkeypatch,
        filenames=("Certify.exe", "Alias.exe"),
        contents={"Certify.exe": exact, "Alias.exe": exact},
    )
    calls = {"issued": 0}

    async def fake_issue(*_args, **_kwargs):
        calls["issued"] += 1
        return {"display_id": calls["issued"]}

    async def fake_wait(*_args, **_kwargs):
        return "ERROR(S):\r\n  Required option 'unexpected' is missing.\r\n"

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue), patch.object(
        mythic_tools.mythic, "waitfor_for_task_output", fake_wait
    ):
        first = asyncio.run(client.issue_task_and_waitfor_task_output(
            "execute_assembly", "Certify.exe enum-cas", 7
        ))
        second = asyncio.run(client.issue_task_and_waitfor_task_output(
            "inline_assembly", "Certify.exe   enum-cas", 7
        ))
    assert "required option" in first.lower()
    # Filename identity (#56): the claimed "Certify.exe" is refused through the inline route and must
    # use the registered selector; it is never issued as a generic inline task.
    assert "STOP" in second and "enrolled executable" in second
    assert calls["issued"] == 1
    assert command_builder.classify_result(
        "execute_assembly", "Required option 'x' is missing."
    ) == "success"
    assert command_builder.enrolled_executable_parser_failure(
        "Required option 'x' is missing."
    )
