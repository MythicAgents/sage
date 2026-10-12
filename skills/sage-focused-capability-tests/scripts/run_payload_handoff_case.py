"""Inert assembled-graph Payload handoff measurement; inspection is offline by default.

Run the same file in each clean source checkout. Five local wire seeds establish the
Supervisor -> Operator -> Payload build -> Supervisor -> Operator path. Only the
post-handoff decisions use the configured model. The sole accepted action is a
mocked Apollo build; an upload proposal remains pending and is never accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import run_operator_prompt_matrix as matrix
from ai.langgraph import model as model_module
from ai.langgraph import command_parameter_schema
from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract


SAGE = matrix.SAGE
DEFAULT_CONFIG = SAGE / ".env.local"
IMPORTED_MODEL_PATH = Path(model_module.__file__).resolve()
CASE = "payload-handoff-apollo-v2"
FILE_UUID = "d9c43269-2cb5-4aa3-b893-219294b88b17"
PAYLOAD_UUID = "4fded21d-27c3-4a52-b05d-3e59c12cd75a"
FILENAME = "synthetic-apollo.exe"
DESTINATION = "C:\\Temp\\synthetic-apollo.exe"
C2_HOST = "https://c2.fixture.invalid"
C2_PARAMETERS = [{"c2profile": "http", "parameter_name": "callback_host",
                  "value": C2_HOST},
                 {"c2profile": "http", "parameter_name": "callback_port",
                  "value": "443"}]
PROMPT = ("In supervised mode, use callback 7 as the reference for a "
          "Windows Apollo Payload build, then propose uploading its resulting Mythic file to callback 7 "
          f"at {DESTINATION}. Stop for each native approval. The build result will identify "
          "the file; do not guess its UUID.")
BUILD_ARGS = {"payload_type_name": "apollo", "filename": FILENAME,
              "operating_system": "Windows", "c2_profiles": [{"c2_profile": "http",
                  "c2_profile_parameters": {"callback_host": C2_HOST, "callback_port": "443"}}],
              "build_parameters": [], "description": "Windows Apollo for callback 7"}
BUILD_RESULT = {"build_phase": "success", "uuid": PAYLOAD_UUID,
                "payload_uuid": PAYLOAD_UUID, "agent_file_id": FILE_UUID,
                "filename": FILENAME, "filemetum": {"agent_file_id": FILE_UUID,
                "filename_utf8": FILENAME, "complete": True},
                "_uuid_help": "Use agent_file_id for upload_file_by_file_uuid."}
RECOVERY_ROW = {"uuid": PAYLOAD_UUID, "build_phase": "success",
                "filename": FILENAME, "os": "Windows", "payloadtype": {"name": "apollo"},
                "filemetum": BUILD_RESULT["filemetum"], "agent_file_id": FILE_UUID}
UPLOADED_FILES = []  # Production excludes Mythic payload files from this inventory.
PAYLOAD_NAMES = ["apollo"]
CALLBACK_C2_CONFIG = {"callback": {"display_id": 7,
    "host": "fixture-host.invalid", "user": "fixture-user"},
    "c2_parameters": C2_PARAMETERS}
PAYLOAD_C2_CONFIG = {"payload_uuid": PAYLOAD_UUID, "filename": FILENAME,
    "file_id": None, "agent_file_id": FILE_UUID,
    "filemetum": BUILD_RESULT["filemetum"], "c2_parameters": C2_PARAMETERS}
DOWNLOAD_RESULT = {"payload_uuid": PAYLOAD_UUID, "filename": FILENAME,
    "file_id": None, "agent_file_id": FILE_UUID,
    "filemetum": BUILD_RESULT["filemetum"], "downloaded_bytes": 4096,
    "reuse_instruction": "Use agent_file_id with upload_file_by_file_uuid to redeploy this existing payload."}
APOLLO_C2_PROFILES = {"payloadtypec2profile": [{
    "payloadtype": {"name": "apollo"}, "c2profile": {
        "name": "http", "description": "synthetic inert C2 schema", "is_p2p": False,
        "c2profileparameters": [{"name": "callback_host", "description": "synthetic callback URL",
            "parameter_type": "String", "required": True, "default_value": "",
            "choices": []}]}}]}
UPLOAD_SCHEMA = [
    {"name": "remote_path", "cli_name": "Path", "type": "String", "choices": [],
     "parameter_group_name": "Default", "required": False},
    {"name": "remote_path", "cli_name": "Path", "type": "String", "choices": [],
     "parameter_group_name": "ExistingFile", "required": False},
    {"name": "file", "cli_name": "File", "type": "File", "choices": [],
     "parameter_group_name": "Default", "required": False},
    {"name": "filename", "cli_name": "fileName", "type": "ChooseOne", "choices": [],
     "parameter_group_name": "ExistingFile", "required": True},
]
SEEDS = (
    ("Supervisor", "transfer_to_Mythic_Operator", {"handoff_title": "Prepare Apollo payload",
      "handoff_instruction": PROMPT}),
    ("Mythic_Operator", "transfer_to_Mythic_Payload", {"handoff_title": "Build Apollo payload",
      "handoff_instruction": "Build the Windows Apollo payload using callback 7 as C2 reference; report its Mythic file identity."}),
    ("Mythic_Payload", "create_payload", BUILD_ARGS),
    ("Mythic_Payload", None, {"content": f"The Apollo build succeeded: {FILENAME}; "
      f"agent_file_id={FILE_UUID}; payload_uuid={PAYLOAD_UUID}."}),
    ("Supervisor", "transfer_to_Mythic_Operator", {"handoff_title": "Propose upload",
      "handoff_instruction": f"Resume the requested upload proposal for callback 7 at {DESTINATION}."}),
)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture_digest() -> str:
    return digest_bytes(json.dumps({"case": CASE, "prompt": PROMPT, "seeds": SEEDS,
        "build_result": BUILD_RESULT, "recovery_row": RECOVERY_ROW,
        "uploaded_files": UPLOADED_FILES, "download_result": DOWNLOAD_RESULT,
        "payload_names": PAYLOAD_NAMES, "callback_c2_config": CALLBACK_C2_CONFIG,
        "payload_c2_config": PAYLOAD_C2_CONFIG,
        "apollo_c2_profiles": APOLLO_C2_PROFILES,
        "upload_schema": UPLOAD_SCHEMA, "destination": DESTINATION},
        sort_keys=True).encode())


def proposal_classification(actions: list[dict]) -> dict:
    verdicts = []
    for action in actions:
        args = action.get("arguments", {})
        name = action.get("tool_name")
        if name not in {"upload_file_by_file_uuid", "issue_task_and_waitfor_task_output"} or not isinstance(args, dict):
            verdicts.append({"supported": False, "reason": "not_upload", "action": action})
            continue
        parameters = args.get("parameters")
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except ValueError:
                parameters = None
        base = (args.get("callback_display_id") == 7 and args.get("command") == "upload"
                and args.get("token_id") is None and isinstance(parameters, dict))
        if name == "upload_file_by_file_uuid":
            file_keys = set(parameters) & {"file", "File"} if isinstance(parameters, dict) else set()
            path_keys = set(parameters) & {"remote_path", "Path"} if isinstance(parameters, dict) else set()
            supported = (base and args.get("file_uuid") == FILE_UUID
                         and len(parameters) == 2 and len(file_keys) == len(path_keys) == 1
                         and parameters[next(iter(file_keys))] == FILE_UUID
                         and parameters[next(iter(path_keys))] == DESTINATION)
        else:
            visibility = args.get("visibility_context") or {}
            binding = visibility.get("_sage_prepared_binding") or {}
            supported = (base and set(parameters) == {"File", "Path"}
                         and parameters.get("File") == FILE_UUID
                         and parameters.get("Path") == DESTINATION
                         and visibility.get("_sage_prepared_action") is True
                         and binding.get("group") is None and binding.get("file") is None)
        verdicts.append({"supported": supported,
                         "reason": "exact_fixture_identity_and_target" if supported else "identity_or_target_mismatch",
                         "action": action})
    return {"proposals": verdicts,
            "supported": len(verdicts) == 1 and verdicts[0]["supported"]}


def _seed_response(request: httpx.Request, model: str, number: int, tool: str | None,
                   args: dict, usage: dict | None = None):
    usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool is None:
        body = json.loads(request.content)
        message = args["content"]
        if body.get("stream"):
            chunks = [{"id": f"local-seed-{number}", "object": "chat.completion.chunk",
                "created": 0, "model": model, "choices": [{"index": 0,
                "delta": {"role": "assistant", "content": message}, "finish_reason": "stop"}]},
                {"id": f"local-seed-{number}", "object": "chat.completion.chunk",
                 "created": 0, "model": model, "choices": [], "usage": usage}]
            content = "".join("data: " + json.dumps(row) + "\n\n" for row in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, request=request, content=content.encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, request=request, json={"id": f"local-seed-{number}",
            "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": message}}],
            "usage": usage})
    call = {"id": f"local-seed-{number}", "type": "function", "function": {
        "name": tool, "arguments": json.dumps(args, sort_keys=True)}}
    body = json.loads(request.content)
    if body.get("stream"):
        chunks = [
            {"id": f"local-seed-{number}", "object": "chat.completion.chunk", "created": 0,
             "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                 {"index": 0, **call}]}, "finish_reason": None}]},
            {"id": f"local-seed-{number}", "object": "chat.completion.chunk", "created": 0,
             "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"id": f"local-seed-{number}", "object": "chat.completion.chunk", "created": 0,
             "model": model, "choices": [], "usage": usage},
        ]
        content = "".join("data: " + json.dumps(row) + "\n\n" for row in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, request=request, content=content.encode(),
                              headers={"content-type": "text/event-stream"})
    return httpx.Response(200, request=request, json={"id": f"local-seed-{number}",
        "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [call]}}],
        "usage": usage})


def offline_scripted_continuation(request: httpx.Request, model: str, number: int,
                                  usage: dict | None = None):
    """Test-only response for either the direct upload or a complete Payload reuse visit."""
    body = json.loads(request.content)
    message_rows = body.get("messages", [])
    messages = json.dumps(message_rows, sort_keys=True)
    offered = {item.get("function", {}).get("name") for item in body.get("tools", [])}
    if "download_payload" in offered:
        completed_reads = {call.get("function", {}).get("name") for item in message_rows
            if item.get("role") == "assistant" for call in item.get("tool_calls", [])}
        for name, args in (
            ("get_all_payloads", {}),
            ("get_callback_c2_config", {"display_id": 7}),
            ("get_payload_c2_config", {"payload_uuid": PAYLOAD_UUID}),
            ("download_payload", {"payload_uuid": PAYLOAD_UUID}),
        ):
            if name not in completed_reads:
                if name not in offered:
                    raise matrix.FixtureGap("Payload reuse read not offered: " + name)
                return _seed_response(request, model, number, name, args, usage=usage)
        return _seed_response(request, model, number, None, {"content":
            f"Reused {FILENAME}; agent_file_id={FILE_UUID}; payload_uuid={PAYLOAD_UUID}."},
            usage=usage)
    if FILE_UUID not in messages:
        if "transfer_to_Mythic_Payload" not in offered:
            raise matrix.FixtureGap("Payload delegation not offered to Operator")
        return _seed_response(request, model, number, "transfer_to_Mythic_Payload", {
            "handoff_title": "Recover mocked Apollo payload",
            "handoff_instruction": "Find the existing Windows Apollo payload matching callback 7 C2, download it, and report its Mythic file UUID."
        }, usage=usage)
    if "upload_file_by_file_uuid" not in offered:
        raise matrix.FixtureGap("upload proposal not offered to Operator")
    return _seed_response(request, model, number, "upload_file_by_file_uuid", {
        "command": "upload", "parameters": {"file": FILE_UUID, "remote_path": DESTINATION},
        "file_uuid": FILE_UUID, "callback_display_id": 7}, usage=usage)


class SeededTransport(matrix.MatrixTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seed_index = 0
        self.local_seeds = []
        self.posthandoff_input_bytes = []

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        if self.seed_index < len(SEEDS):
            expected_role, tool, args = SEEDS[self.seed_index]
            role = ("Mythic_Payload" if any(item.get("function", {}).get("name") == "create_payload"
                    for item in body.get("tools", [])) else matrix.REAL._role_hint(body))
            offered = {item.get("function", {}).get("name") for item in body.get("tools", [])}
            if (request.method != "POST" or str(request.url) != self.endpoint
                    or body.get("model") != self.model
                    or body.get("max_completion_tokens", body.get("max_tokens")) != self.output_tokens
                    or not role.startswith(expected_role) or (tool is not None and tool not in offered)):
                raise matrix.REAL.CaseError("local_seed_wire_mismatch")
            self.seed_index += 1
            self.local_seeds.append({"number": self.seed_index, "role": role,
                "tool": tool, "arguments": args, "offered_tools": sorted(offered)})
            self.persist()
            return _seed_response(request, self.model, self.seed_index, tool, args)
        self.posthandoff_input_bytes.append(len(request.content))
        self.persist()
        return await super().handle_async_request(request)


def _account_provider_requests(requests: list[dict], calls: list[dict]) -> dict:
    """Reconcile every dispatched request, including the final or interrupted one."""
    by_id = {}
    for call in calls:
        by_id.setdefault(call.get("run_id"), []).append(call)
    total_input = total_output = total_tokens = 0
    unknown = []
    for request in requests:
        matches = by_id.get(request.get("callback_run_id"), [])
        call = matches[0] if request.get("callback_run_id") is not None and len(matches) == 1 else None
        usages = call.get("usage", []) if call and call.get("status") == "completed" else []
        if (request.get("status") != "returned" or request.get("http_status") != 200
                or len(usages) != 1 or any(
                    type(usages[0].get(key)) is not int or usages[0][key] < 0
                    for key in ("input_tokens", "output_tokens", "total_tokens"))
                or usages[0]["input_tokens"] + usages[0]["output_tokens"] != usages[0]["total_tokens"]):
            unknown.append(request["index"])
            continue
        total_input += usages[0]["input_tokens"]
        total_output += usages[0]["output_tokens"]
        total_tokens += usages[0]["total_tokens"]
    return {"requests": len(requests), "input_tokens": total_input,
            "output_tokens": total_output, "total_tokens": total_tokens,
            "unknown_request_indices": unknown, "usage_complete": not unknown}


async def run_case(profile: dict, output_dir: Path, _shared_ledger=None, tls_context=None,
                   ips: set[str] | None = None,
                   limits=None, config_path: Path | None = None):
    limits = limits or SimpleNamespace(max_wall=300)
    if os.environ.get("SAGE_RESOURCE_BUDGET_CONFIG") or os.environ.get("SAGE_RESOURCE_BUDGET_LEDGER"):
        raise ValueError("handoff case requires SAGE_RESOURCE_BUDGET_CONFIG and SAGE_RESOURCE_BUDGET_LEDGER unset")
    path = output_dir / "case.json"
    row = {"case": CASE, "tier": "assembled LangGraph HITL", "status": "started",
           "model_sha256": digest_bytes(IMPORTED_MODEL_PATH.read_bytes()),
           "imported_model_path": str(IMPORTED_MODEL_PATH),
           "config_path": str(config_path.resolve()) if config_path is not None else None,
           "fixture_sha256": fixture_digest(), "route": {"provider": profile["provider"],
           "model": profile["model"], "endpoint_sha256": digest_bytes(profile["API_ENDPOINT"].encode())},
           "fixture_scope": "mocked Apollo build, payload listing/C2/download metadata and empty ordinary uploaded-file inventory; no payload bytes or live C2",
           "local_seed_trace": [], "provider_requests": [], "model_calls": [],
           "fixture_reads": [], "effects": [], "accepted_local_builds": [], "proposals": []}
    def persist():
        matrix.atomic_json(path, row)
    persist()
    usage = matrix.MatrixUsage(persist)
    deadline_at = datetime.now(timezone.utc) + timedelta(seconds=limits.max_wall)
    transport = SeededTransport(profile["API_ENDPOINT"], profile["model"], tls_context,
        max_requests=None, output_tokens=2048, usage_recorder=usage,
        max_observed_tokens=None, request_seconds=None, require_complete_usage=True,
        utc_deadline=deadline_at,
        persist=persist)
    row["provider_requests"] = transport.requests
    row["model_calls"] = usage.calls
    row["transport_rejections"] = transport.rejections
    row["local_seed_trace"] = transport.local_seeds
    row["posthandoff_input_bytes"] = transport.posthandoff_input_bytes
    sink = MythicTools(agent_task_id="payload-handoff-case", operation_id=1, channel_id=1)
    sink.client = SimpleNamespace()
    async def no_effect(*_args, **_kwargs):
        raise RuntimeError("synthetic fixture prohibits target or payload effects")
    sink._issue_mythic_task = no_effect
    def blocked_effect(method):
        @functools.wraps(method)
        async def blocked(*args, **kwargs):
            return await no_effect(*args, **kwargs)
        return blocked
    for effect_name in ("issue_task_and_waitfor_task_output", "upload_file_by_file_uuid"):
        production_method = getattr(sink, effect_name)
        # get_tools infers the provider schema from these methods. Preserve the
        # production signature and docstring while direct calls still fail closed;
        # fixture_tools below intercepts every offered invocation before either
        # blocked method can run.
        setattr(sink, effect_name, blocked_effect(production_method))
    async def live_command(command, callback_id):
        return ({"status": "available", "command": "upload"} if command == "upload" and callback_id == 7
                else {"status": "missing", "payload_type": "apollo"})
    async def upload_schema(command, callback_id, **_kwargs):
        return UPLOAD_SCHEMA if command == "upload" and callback_id == 7 else None
    async def uploaded_file(name):
        return None  # Payload files are excluded from the operator-uploaded-file lookup.
    sink._authenticate_live_command = live_command
    sink._fetch_command_schema = upload_schema
    sink._find_uploaded_file_by_name = uploaded_file
    async def dispatch(name, args):
        if name == "create_payload":
            if args != BUILD_ARGS or row["accepted_local_builds"]:
                raise matrix.FixtureGap("unexpected synthetic build")
            row["accepted_local_builds"].append({"kind": "approved_local_setup",
                "arguments": args, "result": BUILD_RESULT})
            persist()
            return json.dumps(BUILD_RESULT, sort_keys=True)
        if name == "get_all_payloads":
            result = json.dumps([RECOVERY_ROW], sort_keys=True)
        elif name == "get_all_payload_info":
            result = json.dumps({"payloadtype": [{"name": "apollo", "supported_os": ["Windows"],
                "buildparameters": []}]}, sort_keys=True)
        elif name == "get_all_uploaded_files":
            result = json.dumps(UPLOADED_FILES, sort_keys=True)
        elif name == "get_payload_names":
            result = PAYLOAD_NAMES
        elif name == "get_callback_c2_config":
            if args.get("display_id") != 7:
                raise matrix.FixtureGap("unknown callback C2 config")
            result = json.dumps(CALLBACK_C2_CONFIG, sort_keys=True)
        elif name == "get_payload_c2_config":
            if args.get("payload_uuid") != PAYLOAD_UUID:
                raise matrix.FixtureGap("unknown fixture payload C2 config")
            result = json.dumps(PAYLOAD_C2_CONFIG, sort_keys=True)
        elif name == "download_payload":
            if args.get("payload_uuid") != PAYLOAD_UUID:
                raise matrix.FixtureGap("unknown fixture payload download")
            result = json.dumps(DOWNLOAD_RESULT, sort_keys=True)
        elif name == "get_c2_profiles_for_payload":
            if args.get("payload") != "apollo":
                raise matrix.FixtureGap("unknown fixture payload type C2 profiles")
            result = json.dumps(APOLLO_C2_PROFILES, sort_keys=True)
        elif name in {"list_callbacks", "get_all_active_callbacks"}:
            result = json.dumps({"callback": [{"display_id": 7,
                "host": CALLBACK_C2_CONFIG["callback"]["host"],
                "active": True, "payload": {"payloadtype": {"name": "apollo"}}}]})
        elif name in {"get_all_command_names_for_payloadtype", "get_all_commands_for_payloadtype"}:
            result = json.dumps({"command": [{"cmd": "upload", "description": "synthetic file upload"}]})
        elif name == "get_all_command_args_for_payloadtype":
            if args.get("payload") != "apollo" or args.get("command") != "upload":
                raise matrix.FixtureGap("unknown fixture command args")
            result = json.dumps({"payload_type": "apollo", "command": "upload",
                "parameter_groups": command_parameter_schema.group_flat_parameters(UPLOAD_SCHEMA)})
        else:
            raise matrix.FixtureGap("unavailable fixture tool: " + name)
        row["fixture_reads"].append({"tool": name, "arguments": args, "result": result})
        persist()
        return result
    def fixture_tools(names):
        tools = []
        for original in MythicTools.get_tools(sink, names):
            async def call(__name=original.name, **kwargs):
                return await dispatch(__name, kwargs)
            tools.append(StructuredTool.from_function(coroutine=call, name=original.name,
                description=original.description, args_schema=original.args_schema))
        return tools
    sink.get_tools = fixture_tools
    async def payload_type(callback_id):
        return "apollo" if callback_id == 7 else None
    sink._resolve_payload_type = payload_type
    async_client = httpx.AsyncClient(transport=transport, timeout=300, trust_env=False)
    sync_client = httpx.Client(transport=matrix.REAL.BoundedSyncTransport(), timeout=300, trust_env=False)
    factory = model_module.init_chat_model
    def bounded_factory(*args, **kwargs):
        if kwargs.get("model_provider") != "openai" or kwargs.get("model") != profile["model"]:
            raise matrix.REAL.CaseError("unexpected_model_construction")
        kwargs.update(http_client=sync_client, http_async_client=async_client,
                      max_retries=0, timeout=300, max_tokens=2048)
        llm = factory(*args, **kwargs)
        llm.callbacks = [usage]
        return llm
    task = asyncio.current_task()
    timed_out = False
    def deadline_cancel():
        nonlocal timed_out
        timed_out = True
        task.cancel()
    deadline_handle = asyncio.get_running_loop().call_later(
        max(0, (deadline_at - datetime.now(timezone.utc)).total_seconds()), deadline_cancel)
    try:
        with (patch.object(model_module, "init_chat_model", bounded_factory),
              patch.dict(os.environ, {"SAGE_EVAL_MAX_OUTPUT_TOKENS": "2048"}),
              matrix.REAL.socket_allowlist(ips, profile["API_ENDPOINT"])):
            model = Model(provider=profile["provider"], model=profile["model"], system_prompt="",
                config={"configurable": {"api_key": profile["API_KEY"],
                    "base_url": profile["API_ENDPOINT"]}}, task_id=0,
                agent_task_id="payload-handoff-case", mode="supervised",
                autonomous_solve=False, policy_mode="hybrid", operation_id=1,
                channel_id=1, apitoken_id=0)
            model.mythic_client = sink
            model._payload_names = ["apollo"]
            model.memory = InMemorySaver()
            model._native_chat_explicit_hitl = True
            model.command_name = "chat"
            async def inert_emitter(_content):
                return True
            model._response_emitter = inert_emitter
            model._rebuild_graph()
            contract = build_request_contract(request_id="chat:1:request:1", channel_id="1",
                operation_id="operation-1", mode="supervised", autonomous_solve=False)
            model.install_request_contract(contract)
            human = HumanMessage(content=PROMPT, additional_kwargs={
                "_request_id": contract.request_id, "_operator_input": True,
                "_operator_input_revision": 0})
            model_module._tag_msg(human, model._next_seq())
            model.state["messages"] = [human]
            model.state["supervisor_messages"].append(human)
            config = model._graph_run_config("payload-handoff-case")
            # This installed LangGraph accepts infinity (only checks >= 1, then
            # compares graph steps to current_step + recursion_limit + 1).
            # The external wall deadline is this measurement's stopping limit.
            config["recursion_limit"] = math.inf
            first = await model.graph.ainvoke(model.state, config)
            row["first_interrupt"] = first.get("__interrupt__") is not None
            row["build_approval_request"] = [getattr(item, "value", {})
                for item in first.get("__interrupt__", [])]
            if not row["first_interrupt"] or transport.seed_index != 3:
                raise matrix.FixtureGap("synthetic build did not pause at first native HITL")
            build_actions = [action for value in row["build_approval_request"]
                for action in value.get("action_requests", [])]
            if len(build_actions) != 1 or build_actions[0].get("name") != "create_payload" or \
                    build_actions[0].get("args") != BUILD_ARGS:
                raise matrix.FixtureGap("native build approval differs from exact synthetic setup")
            # One exact, local-only setup approval. The resumed tool returns mocked build metadata.
            second = await model.graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            row["second_interrupt"] = second.get("__interrupt__") is not None
            checkpoint = await model.graph.aget_state(config)
            row["next_nodes"] = list(checkpoint.next)
            state = checkpoint.values
            row["operator_context"] = [{"type": message.type, "content": message.content}
                for message in state.get("mythic_operator_messages", [])]
            row["local_seed_count"] = transport.seed_index
            actions = []
            for item in second.get("__interrupt__", []):
                value = getattr(item, "value", {})
                for action in value.get("action_requests", []) if isinstance(value, dict) else []:
                    actions.append({"tool_name": action.get("name"), "arguments": action.get("args")})
            row["classification"] = proposal_classification(actions)
            row["proposals"] = actions
            row["protocol_valid"] = (transport.seed_index == len(SEEDS)
                and len(row["accepted_local_builds"]) == 1 and not row["effects"])
            row["objective_success"] = bool(row["protocol_valid"] and row["classification"]["supported"])
            row["status"] = "observed" if row["second_interrupt"] else "no_pending_upload"
    except BaseException as exc:
        deadline_expired = (timed_out or datetime.now(timezone.utc) >= transport.utc_deadline
            or any(item.get("reason") == "utc_deadline_expired" for item in transport.rejections))
        unknown_usage = any(item.get("reason") == "unknown_observed_usage"
            for item in transport.rejections)
        row["status"] = ("time_limit" if deadline_expired else
            "unknown_provider_usage" if unknown_usage else
            "harness_gap" if isinstance(exc, matrix.FixtureGap) else
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
        row["error"] = type(exc).__name__ + ": " + str(exc)
        row["stop_reason"] = ("wall_time" if deadline_expired else
            "unknown_provider_usage" if unknown_usage else type(exc).__name__)
        row["protocol_valid"] = bool(deadline_expired and transport.seed_index == len(SEEDS)
            and len(row["accepted_local_builds"]) == 1 and not row["effects"])
        row["objective_success"] = False
    finally:
        deadline_handle.cancel()
        await async_client.aclose()
        sync_client.close()
        row["provider_accounting"] = _account_provider_requests(transport.requests, usage.calls)
        if not row["provider_accounting"]["usage_complete"]:
            row["usage_uncertain"] = True
            if row["status"] in {"observed", "no_pending_upload"}:
                row["status"] = "unknown_provider_usage"
                row["stop_reason"] = "unknown_provider_usage"
                row["protocol_valid"] = False
                row["objective_success"] = False
        persist()
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="provider profile (default: this checkout's Payload_Type/sage/.env.local)")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-endpoint-sha256")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--max-wall-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    source = digest_bytes(IMPORTED_MODEL_PATH.read_bytes())
    if not args.run:
        print(json.dumps({"status": "offline_inspection", "tier": "assembled LangGraph HITL",
            "config_path": str(args.config.resolve()),
            "source_sha256": source, "fixture_sha256": fixture_digest(),
            "max_real_calls": None,
            "max_observed_tokens": None,
            "max_wall_seconds": args.max_wall_seconds,
            "fixture_scope": "mocked Apollo build, payload listing/C2/download metadata, and empty ordinary uploaded-file inventory; no bytes or live C2",
            "local_setup_approval": "one mocked Apollo build", "target_effects": 0}, sort_keys=True))
        return 0
    try:
        if IMPORTED_MODEL_PATH != (SAGE / "ai/langgraph/model.py").resolve():
            raise ValueError("imported model.py does not match the selected checkout")
        if not 1 <= args.max_wall_seconds <= 300:
            raise ValueError("invalid per-arm wall limit")
        if not all((args.output_dir, args.expected_model,
                    args.expected_endpoint_sha256, args.expected_source_sha256)):
            raise ValueError("--run requires output and exact model, endpoint and source identities")
        profile = matrix.REAL.load_profile(args.config)
        if (profile["provider"] != "openai" or profile["model"] != args.expected_model
                or source != args.expected_source_sha256
                or digest_bytes(profile["API_ENDPOINT"].encode()) != args.expected_endpoint_sha256):
            raise ValueError("source or route identity mismatch")
        if os.environ.get("SAGE_RESOURCE_BUDGET_CONFIG") or os.environ.get("SAGE_RESOURCE_BUDGET_LEDGER"):
            raise ValueError("handoff case requires SAGE_RESOURCE_BUDGET_CONFIG and SAGE_RESOURCE_BUDGET_LEDGER unset")
        if args.output_dir.exists():
            raise ValueError("output directory already exists")
        ips = matrix.REAL.resolved_endpoint_ips(profile["API_ENDPOINT"])
        tls = matrix.REAL.sage_tls_context()
        args.output_dir.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="sage-payload-handoff-") as temp:
            from sage_chat import session
            with patch.object(session, "_CHANNEL_MARKER_DB_PATH", str(Path(temp) / "sage.db")):
                row = asyncio.run(run_case(profile, args.output_dir,
                    None, tls, ips, SimpleNamespace(max_wall=args.max_wall_seconds),
                    config_path=args.config))
        print(json.dumps({"status": row["status"], "classification": row.get("classification"),
            "source_sha256": source, "fixture_sha256": fixture_digest(),
            "config_path": str(args.config.resolve())}))
        return 0 if row["status"] in {"observed", "no_pending_upload", "time_limit"} else 1
    except Exception as exc:
        print(json.dumps({"status": "configuration_or_run_error", "type": type(exc).__name__,
                          "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
