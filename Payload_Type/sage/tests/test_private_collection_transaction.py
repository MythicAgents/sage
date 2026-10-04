"""Approved collection through real task, registration, and ingest authorization.

Only external observations/effects are replaced. In particular, stubbing the task
method would hide its nested registered-file prerequisite from this regression.
"""

import asyncio
import copy
import functools
import io
import json
import socket
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.langgraph import engagement_state as es, model, mythic_tools as mt
from ai.langgraph.request_contract import (
    StopConditionKind, action_spec_from_tool_call, build_request_contract,
)
from ai.langgraph.turn_authority import authority_from_request_contract
from ai.mcp import MCPManager
from sage_chat.hitl import (
    approval_action_digest, approval_action_fingerprint,
    approval_proposal_digest, approval_selection_digest,
)


@pytest.fixture
def collection(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("collection authorization probe attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setenv("SAGE_ENGAGEMENT_GATE", "false")
    monkeypatch.setattr(mt, "SAGE_MYTHIC_TASK_TIMEOUT", 300)
    foothold = es.Foothold(
        callback_id="7", host="workstation", agent="apollo", identity="corp\\operator",
        forest="corp.test", alive=True, integrity="medium", source="offline-fixture", timestamp="",
    )
    state = es.EngagementState(objective="Verify the declared objective", footholds=[foothold])
    request = model._ControllerCollectionRequest(
        foothold=foothold, scope_domain="corp.test", reason="baseline",
        collection_key="corp.test|baseline", support="no verified collection exists",
    )
    root = {"name": "collect_graph", "args": {
        "callback_id": "7", "host": "workstation", "agent": "apollo",
        "identity": "corp\\operator", "scope_domain": request.scope_domain,
        "reason": request.reason, "collection_key": request.collection_key, "support": request.support,
    }}
    contract = build_request_contract(
        request_id="request-collection", channel_id="channel-test", operation_id="operation-test",
        mode="supervised", autonomous_solve=True,
        requested_actions=(action_spec_from_tool_call(root),),
    )
    action_digest = approval_action_digest([root])
    action_ids = [approval_action_fingerprint(root)]
    claim = {
        "approval_id": "approved-collection", "request_id": contract.request_id,
        "request_contract_digest": contract.digest, "tool_name": "collect_graph",
        "selection_mode": "single", "actions": [root], "approved_actions": [root],
        "approved_action_ids": action_ids, "action_digest": action_digest,
        "proposal_digest": approval_proposal_digest(contract.digest, action_digest),
        "selection_digest": approval_selection_digest(contract.digest, action_digest, action_ids),
    }
    client = mt.MythicTools()
    client.client = object()
    client.set_request_contract(contract)
    client.set_turn_authority(authority_from_request_contract(contract))
    client.set_approval_claim(claim)
    owner = object.__new__(model.Model)
    owner.mythic_client = client
    owner._turn_authority = client._turn_authority
    owner._queue_controller_verbose_event = lambda *_args, **_kwargs: None
    adapter = dict(owner._controller_collection_adapter(foothold))
    adapter.update({
        "collection_identity_command": "whoami", "collection_identity_parameters": "",
        "collection_ticket_command": "ticket_cache_list",
        "collection_ticket_parameters": {"luid": "", "getSystemTickets": False},
        "dotnet_runner_command": "execute_assembly", "dotnet_tool_param": "assembly_name",
        "dotnet_args_param": "assembly_arguments", "collection_ls_command": "ls",
        "collection_ls_path_param": "path", "collection_download_command": "download",
        "collection_download_path_param": "path",
    })
    owner._controller_collection_adapter = lambda _foothold: adapter
    events = []
    registered = []
    tasks = []
    selector_overrides = {}

    async def schema(command, callback):
        if command != adapter["dotnet_runner_command"]:
            return []
        return [
            {"name": adapter["dotnet_tool_param"], "type": "ChooseOne", "required": True,
             "parameter_group_name": "Default", "choices": list(registered), **selector_overrides},
            {"name": adapter["dotnet_args_param"], "type": "String", "required": False,
             "parameter_group_name": "Default"},
            {"name": "assembly_file", "type": "File", "required": True,
             "parameter_group_name": "New File"},
        ]

    async def register(filename, content):
        assert content == b"inert registered-file fixture"
        registered.append(filename)
        events.append(("register", filename))
        return "registered-file-test", False

    async def issue(**kwargs):
        tasks.append(kwargs)
        events.append(("task", kwargs["command_name"]))
        return {"display_id": len(tasks)}

    def artifact():
        tx = client._private_collection_transaction
        filename = "20261003120000_" + tx.zip_filename
        return {
            "agent_file_id": "collection-file-test", "filename_utf8": filename,
            "full_name": "C:\\Users\\Public\\" + filename,
            "complete": True, "deleted": False, "is_download_from_agent": True,
            "task": {"display_id": len(tasks), "command_name": adapter["collection_download_command"],
                     "callback": {"display_id": 7}},
        }

    async def output(*, task_display_id, **_kwargs):
        command = tasks[task_display_id - 1]["command_name"]
        if command == adapter["collection_identity_command"]:
            return "Local Identity: corp\\operator\nImpersonation Identity: corp\\operator"
        if command == adapter["collection_ticket_command"]:
            return '[{"client_name":"operator","client_realm":"CORP.TEST", "service_name":"krbtgt/CORP.TEST", "luid":"0x123", "current_luid":"0x123"}]'
        if command == adapter["collection_ls_command"]:
            row = artifact()
            return json.dumps({"files": [{"name": row["filename_utf8"], "full_name": row["full_name"], "is_file": True}]})
        return "Completed successfully"

    async def metadata(*_args):
        return artifact()

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("domains.json", json.dumps({"data": [], "meta": {"type": "domains", "count": 0, "version": 5}}))

    async def bloodhound(args):
        events.append(("ingest", args["info_type"]))
        return {"data": {"job_id": 41} if args["info_type"] == "upload_bytes" else {"status_message": "Complete"}}

    monkeypatch.setattr(mt.ttp_library, "TOOLS_DIR", tmp_path)
    (tmp_path / "SharpHound.exe").write_bytes(b"inert registered-file fixture")
    monkeypatch.setattr(client, "_fetch_command_schema", schema)
    monkeypatch.setattr(client, "_find_uploaded_file_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_register_file_dedup", register)
    monkeypatch.setattr(client, "_invalidate_command_schema_cache", AsyncMock())
    monkeypatch.setattr(client, "_authenticate_live_command", AsyncMock(return_value={"status": "available"}))
    monkeypatch.setattr(client, "_callback_tasking_liveness_blocker", AsyncMock(return_value=""))
    monkeypatch.setattr(client, "_action_footprint", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_supervised_task_state", AsyncMock(return_value={"task_completed": True, "task_status": "completed"}))
    monkeypatch.setattr(client, "_latest_download_for_callback", metadata)
    monkeypatch.setattr(client, "_get_file_metadata", metadata)
    monkeypatch.setattr(mt.mythic, "issue_task", issue)
    monkeypatch.setattr(mt.mythic, "waitfor_for_task_output", output)
    monkeypatch.setattr(mt.mythic, "download_file", AsyncMock(return_value=archive.getvalue()))
    monkeypatch.setattr(MCPManager, "get_connected_servers", lambda: ["offline-bloodhound"])
    monkeypatch.setattr(MCPManager, "is_bloodhound_server", lambda _server: True)
    monkeypatch.setattr(MCPManager, "get_tools_by_server", lambda _server: [SimpleNamespace(name="file_upload", ainvoke=bloodhound)])
    monkeypatch.setattr(mt, "_bloodhound_collected_domains", AsyncMock(return_value=["corp.test"]))
    monkeypatch.setattr(client, "_refresh_graph_facts_if_stale", AsyncMock())
    monkeypatch.setattr(client, "_record_graph_built", AsyncMock())
    original_sleep = asyncio.sleep

    async def no_delay(_seconds):
        await original_sleep(0)

    monkeypatch.setattr(mt.asyncio, "sleep", no_delay)
    return SimpleNamespace(
        client=client, owner=owner, request=request, state=state, root=root, contract=contract,
        claim=claim, adapter=adapter, events=events, registered=registered, tasks=tasks,
        selector_overrides=selector_overrides,
    )


async def _collect(case):
    token = mt._task_visibility_context.set({"capability": "collect-graph"})
    try:
        return await case.owner._controller_collect(case.state, request=case.request)
    finally:
        mt._task_visibility_context.reset(token)


def test_approved_collection_registers_reserved_runner_and_ingests_once(collection):
    case = collection
    result = asyncio.run(_collect(case))
    assert result["ok"] is True, (result, case.events)
    assert result["status"] == "ingested"
    assert case.events == [
        ("task", "whoami"), ("task", "ticket_cache_list"), ("register", "SharpHound.exe"),
        ("task", "execute_assembly"), ("task", "ls"), ("task", "download"),
        ("ingest", "upload_bytes"), ("ingest", "status"),
    ]
    tx = case.client._private_collection_transaction
    assert [step["kind"] for step in tx._steps] == ["identity", "ticket", "runner", "ls", "download"]
    assert all(step["terminal_success"] for step in tx._steps)
    assert tx._artifact_consumed is True
    assert case.contract.stop_condition.kind == StopConditionKind.OBJECTIVE_PROVED
    assert case.client._active_approval_claim["approval_id"] == case.claim["approval_id"]


def _apollo_alias_schema(case):
    return [
        {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
         "required": True, "parameter_group_name": "Default", "choices": []},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "required": False, "parameter_group_name": "Default"},
        {"name": "assembly_file", "cli_name": "assembly_file", "type": "File",
         "required": True, "parameter_group_name": "New Assembly"},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "required": False, "parameter_group_name": "New Assembly"},
    ]


def _install_apollo_alias_schema(case, monkeypatch, *, replace_step=False, other_aliases=False):
    original_schema = case.client._fetch_command_schema

    async def schema(command, callback):
        if other_aliases and command in {"ls", "download"}:
            return [{"name": "path", "cli_name": "Path" if command == "ls" else "File",
                     "type": "String", "required": True, "parameter_group_name": "Default"}]
        if command != "execute_assembly":
            return await original_schema(command, callback)
        if replace_step:
            tx = case.client._private_collection_transaction
            tx._steps[-1] = dict(tx._steps[-1])
        return _apollo_alias_schema(case)

    monkeypatch.setattr(case.client, "_fetch_command_schema", schema)


def test_real_apollo_cli_aliases_issue_exact_runner_and_ingest(collection, monkeypatch):
    case = collection
    _install_apollo_alias_schema(case, monkeypatch)
    result = asyncio.run(_collect(case))
    assert result["ok"] is True, (result, case.events)
    assert case.tasks[2]["parameters"]["Assembly"] == "SharpHound.exe"
    assert "bloodhound_" in case.tasks[2]["parameters"]["Arguments"]
    assert set(case.tasks[2]["parameters"]) == {"Assembly", "Arguments"}
    runner = case.client._private_collection_transaction._steps[2]
    assert "assembly_name" in runner["parameters"]
    assert "Assembly" in runner["resolved_parameters"]


def test_collection_binding_accepts_declared_aliases_for_listing_and_download(collection, monkeypatch):
    case = collection
    _install_apollo_alias_schema(case, monkeypatch, other_aliases=True)
    result = asyncio.run(_collect(case))
    assert result["ok"] is True, (result, case.events)
    assert set(case.tasks[3]["parameters"]) == {"Path"}
    assert set(case.tasks[4]["parameters"]) == {"File"}
    steps = case.client._private_collection_transaction._steps
    assert "path" in steps[3]["parameters"] and "Path" in steps[3]["resolved_parameters"]
    assert "path" in steps[4]["parameters"] and "File" in steps[4]["resolved_parameters"]


@pytest.mark.parametrize("mutation", (
    "value", "added", "dropped", "ambiguous", "step_replaced",
    "stale_binding", "transaction_replaced",
))
def test_collection_alias_binding_rejects_changed_resolution(collection, monkeypatch, mutation):
    case = collection
    _install_apollo_alias_schema(case, monkeypatch, replace_step=mutation == "step_replaced")
    if mutation == "ambiguous":
        original_schema = case.client._fetch_command_schema

        async def ambiguous_schema(command, callback):
            rows = await original_schema(command, callback)
            if command == "execute_assembly":
                rows = [*rows, {"name": "assembly_name", "cli_name": "Other",
                                "type": "String", "required": False,
                                "parameter_group_name": "Default"}]
            return rows

        monkeypatch.setattr(case.client, "_fetch_command_schema", ambiguous_schema)
    if mutation in {"stale_binding", "transaction_replaced"}:
        original_bind = mt._PrivateCollectionTransaction.bind_schema_resolution

        def tamper_binding(self, *args, **kwargs):
            blocker = original_bind(self, *args, **kwargs)
            if not blocker:
                if mutation == "stale_binding":
                    mt._collection_schema_binding_context.set(None)
                else:
                    case.client._private_collection_transaction = copy.copy(self)
            return blocker

        monkeypatch.setattr(mt._PrivateCollectionTransaction, "bind_schema_resolution", tamper_binding)
    if mutation in {"value", "added", "dropped"}:
        original_resolver = mt.command_builder.resolve_params

        def altered(schema, supplied, *, command=""):
            resolved = original_resolver(schema, supplied, command=command)
            if command == "execute_assembly" and resolved.ok:
                resolved.params = dict(resolved.params)
                if mutation == "value":
                    resolved.params["Arguments"] += " --changed"
                elif mutation == "added":
                    resolved.params["Unexpected"] = "default"
                else:
                    del resolved.params["Arguments"]
            return resolved

        monkeypatch.setattr(mt.command_builder, "resolve_params", altered)
    result = asyncio.run(_collect(case))
    assert result["status"] == "task_issue_failed", (result, case.events)
    assert "schema resolution" in result["reason"] or mutation in {
        "stale_binding", "transaction_replaced",
    }, result
    assert not any(event[0] == "task" and event[1] in ("execute_assembly", "ls", "download")
                   for event in case.events)


def test_collection_alias_binding_cannot_be_borrowed_by_sibling_task(collection, monkeypatch):
    case = collection
    _install_apollo_alias_schema(case, monkeypatch)
    original_bind = mt._PrivateCollectionTransaction.bind_schema_resolution
    foreign_reads = []
    foreign_tasks = []

    def observe_foreign(self, *args, **kwargs):
        blocker = original_bind(self, *args, **kwargs)
        if not blocker:
            step = self._current_step()

            async def foreign():
                foreign_reads.append(self._issued_parameters(step))

            foreign_tasks.append(asyncio.create_task(foreign()))
        return blocker

    monkeypatch.setattr(mt._PrivateCollectionTransaction, "bind_schema_resolution", observe_foreign)

    async def run():
        result = await _collect(case)
        await asyncio.gather(*foreign_tasks)
        return result

    assert asyncio.run(run())["ok"] is True
    assert foreign_reads
    assert foreign_reads == [step["parameters"] for step in
                           case.client._private_collection_transaction._steps if "resolved_parameters" in step]


@pytest.mark.parametrize("state", (
    {},
    {"task_completed": False, "task_status": "processing"},
    {"task_completed": True, "task_status": "error"},
))
def test_private_runner_requires_actual_terminal_success_before_listing(collection, monkeypatch, state):
    case = collection
    _install_apollo_alias_schema(case, monkeypatch)

    async def task_state(_task_id, _callback_id, command):
        if command == "execute_assembly":
            return state
        return {"task_completed": True, "task_status": "completed"}

    monkeypatch.setattr(case.client, "_supervised_task_state", task_state)
    result = asyncio.run(_collect(case))
    assert result["status"] in {"task_nonterminal", "task_failed"}, result
    tx = case.client._private_collection_transaction
    runner = tx._steps[2]
    assert runner["task_id"] == "3"
    assert runner["terminal_success"] is False
    assert tx._phase == "after_probe"
    assert not any(event[0] == "task" and event[1] in ("ls", "download") for event in case.events)


def test_collection_registration_uses_adapter_and_schema_selector_alias(collection):
    case = collection
    case.adapter.update({"dotnet_runner_command": "custom-runner", "dotnet_tool_param": "tool",
                         "dotnet_args_param": "arguments"})
    case.selector_overrides.update({"name": "registered_ref", "cli_name": "tool"})
    result = asyncio.run(_collect(case))
    assert result["ok"] is True, (result, case.events)
    assert case.registered == ["SharpHound.exe"]
    assert case.tasks[2]["command_name"] == "custom-runner"
    assert case.tasks[2]["parameters"]["tool"] == "SharpHound.exe"


@pytest.mark.parametrize("mutation", [
    "filename", "filename-case", "filename-space", "filename-path", "phase", "step-callback",
    "preflight-callback", "step-parameters", "step-identity", "step-issued", "transaction", "no-transaction", "claim-id",
    "claim-request", "claim-digest", "closed-claim", "provenance", "child-task",
])
def test_collection_registration_rejects_near_matches(collection, monkeypatch, mutation):
    case = collection
    original = case.client.ensure_tool_uploaded

    @functools.wraps(original)
    async def changed(filename):
        tx = case.client._private_collection_transaction
        if mutation.startswith("filename"):
            filename = {"filename": "OtherTool.exe", "filename-case": filename.lower(),
                        "filename-space": " " + filename, "filename-path": "../" + filename}[mutation]
        elif mutation == "phase":
            tx._phase = "listing"
        elif mutation == "step-callback":
            tx._current_step()["callback_id"] = "8"
        elif mutation == "preflight-callback":
            # The same filename from another preflight must not borrow ambient authority.
            monkeypatch.setattr(case.client, "ensure_tool_uploaded", original)
            return await case.client._ensure_registered_file_available(
                tx._current_step()["command"], json.loads(tx._current_step()["parameters"]), 8,
            )
        elif mutation == "step-parameters":
            tx._current_step()["parameters"] = '{}'
        elif mutation == "step-identity":
            tx._steps[-1] = dict(tx._steps[-1])
        elif mutation == "step-issued":
            tx._current_step()["task_id"] = "already-issued"
        elif mutation == "transaction":
            case.client._private_collection_transaction = copy.copy(tx)
        elif mutation == "no-transaction":
            case.client._private_collection_transaction = None
        elif mutation.startswith("claim-"):
            key = {"claim-id": "approval_id", "claim-request": "request_id",
                   "claim-digest": "request_contract_digest"}[mutation]
            case.client._active_approval_claim[key] = "different"
        elif mutation == "closed-claim":
            case.client.clear_approval_claim()
        elif mutation == "provenance":
            context = mt._task_visibility_context.set({"capability": "other"})
            try:
                return await original(filename)
            finally:
                mt._task_visibility_context.reset(context)
        elif mutation == "child-task":
            return await asyncio.create_task(original(filename))
        return await original(filename)

    monkeypatch.setattr(case.client, "ensure_tool_uploaded", changed)
    result = asyncio.run(_collect(case))
    assert result["status"] == "tool_preflight_failed", result
    assert case.events == [("task", "whoami"), ("task", "ticket_cache_list")]
    assert case.registered == []


@pytest.mark.parametrize("when", ["before", "reserved", "failed-registration", "finished"])
def test_collection_registration_cannot_escape_preflight(collection, monkeypatch, when):
    case = collection
    original = case.client.ensure_tool_uploaded

    async def scenario():
        if when in {"reserved", "failed-registration"}:
            @functools.wraps(original)
            async def fail(filename):
                if when == "reserved":
                    raise RuntimeError("inert interrupted registration")
                return await original(filename)

            async def failed_effect(*_args):
                raise RuntimeError("inert registration failure")

            monkeypatch.setattr(case.client, "ensure_tool_uploaded", fail)
            monkeypatch.setattr(case.client, "_register_file_dedup", failed_effect)
            result = await _collect(case)
            assert result["status"] == "tool_preflight_failed"
        elif when == "finished":
            assert (await _collect(case))["ok"] is True
        assert mt._collection_file_preflight_context.get() is None
        context = mt._task_visibility_context.set({"capability": "collect-graph"})
        try:
            with pytest.raises(PermissionError, match="does not cover this exact effect"):
                await original("SharpHound.exe")
        finally:
            mt._task_visibility_context.reset(context)

    asyncio.run(scenario())
    assert case.registered == (["SharpHound.exe"] if when == "finished" else [])


def test_completed_collection_cannot_consume_artifact_or_approval_again(collection):
    case = collection

    async def scenario():
        assert (await _collect(case))["ok"] is True
        events = list(case.events)
        repeated = await _collect(case)
        assert repeated["status"] == "blocked" and "already active" in repeated["reason"]
        tx = case.client._private_collection_transaction
        context = mt._task_visibility_context.set({"capability": "collect-graph"})
        try:
            duplicate = json.loads(await case.client.ingest_collection(
                file_uuid=tx._artifact["file_uuid"], callback_display_id=7,
                file_name=tx._artifact["filename"], collection_scope_domain="corp.test",
            ))
        finally:
            mt._task_visibility_context.reset(context)
        assert duplicate["status"] == "blocked"
        assert case.events == events

    asyncio.run(scenario())
