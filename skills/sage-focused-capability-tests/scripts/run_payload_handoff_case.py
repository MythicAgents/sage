"""Inert assembled-graph Payload handoff measurement; inspection is offline by default.

Run the same file in each clean source checkout. Five local wire seeds establish the
Supervisor -> Operator -> Payload build -> Supervisor -> Operator path. Only the
post-handoff decisions use the configured model. The sole accepted action is a
synthetic text-file build; an upload proposal remains pending and is never accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
from ai import resource_budget, resource_http
from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract


SAGE = matrix.SAGE
DEFAULT_CONFIG = SAGE / ".env.local"
IMPORTED_MODEL_PATH = Path(model_module.__file__).resolve()
CASE = "payload-handoff-text-v1"
FILE_UUID = "d9c43269-2cb5-4aa3-b893-219294b88b17"
PAYLOAD_UUID = "4fded21d-27c3-4a52-b05d-3e59c12cd75a"
FILENAME = "synthetic-handoff-note.txt"
DESTINATION = "C:\\Temp\\synthetic-handoff-note.txt"
PROMPT = ("In supervised mode, use callback 7 as the reference for an inert text-file "
          "Payload build, then propose uploading its resulting Mythic file to callback 7 "
          f"at {DESTINATION}. Stop for each native approval. The build result will identify "
          "the file; do not guess its UUID.")
BUILD_ARGS = {"payload_type_name": "apollo", "filename": FILENAME,
              "operating_system": "Windows", "c2_profiles": [],
              "build_parameters": [], "description": "inert text-only fixture"}
BUILD_RESULT = {"build_phase": "success", "uuid": PAYLOAD_UUID,
                "payload_uuid": PAYLOAD_UUID, "agent_file_id": FILE_UUID,
                "filename": FILENAME, "filemetum": {"agent_file_id": FILE_UUID,
                "filename_utf8": FILENAME, "complete": True},
                "_uuid_help": "Use agent_file_id for upload_file_by_file_uuid."}
RECOVERY_ROW = {"uuid": PAYLOAD_UUID, "build_phase": "success",
                "filename": FILENAME, "filemetum": BUILD_RESULT["filemetum"],
                "agent_file_id": FILE_UUID}
UPLOADED_FILE_ROW = {"agent_file_id": FILE_UUID, "filename_utf8": FILENAME,
                     "complete": True, "deleted": False, "is_payload": False}
UPLOAD_SCHEMA = [
    {"name": "file", "cli_name": "file", "type": "ChooseOne", "choices": [FILENAME],
     "parameter_group_name": "Default", "required": True},
    {"name": "remote_path", "cli_name": "remote_path", "type": "String", "choices": [],
     "parameter_group_name": "Default", "required": True},
    {"name": "file", "cli_name": "file", "type": "File", "choices": [],
     "parameter_group_name": "New File", "required": True},
    {"name": "remote_path", "cli_name": "remote_path", "type": "String", "choices": [],
     "parameter_group_name": "New File", "required": True},
]
SEEDS = (
    ("Supervisor", "transfer_to_Mythic_Operator", {"handoff_title": "Prepare text file",
      "handoff_instruction": PROMPT}),
    ("Mythic_Operator", "transfer_to_Mythic_Payload", {"handoff_title": "Build inert text file",
      "handoff_instruction": "Build an inert .txt fixture using callback 7 as reference; report the resulting file identity."}),
    ("Mythic_Payload", "create_payload", BUILD_ARGS),
    ("Mythic_Payload", None, {"content": f"The inert text build succeeded: {FILENAME}; "
      f"agent_file_id={FILE_UUID}; payload_uuid={PAYLOAD_UUID}."}),
    ("Supervisor", "transfer_to_Mythic_Operator", {"handoff_title": "Propose upload",
      "handoff_instruction": f"Resume the requested upload proposal for callback 7 at {DESTINATION}."}),
)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture_digest() -> str:
    return digest_bytes(json.dumps({"case": CASE, "prompt": PROMPT, "seeds": SEEDS,
        "build_result": BUILD_RESULT, "recovery_row": RECOVERY_ROW,
        "uploaded_file_row": UPLOADED_FILE_ROW,
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
                and args.get("token_id") is None and isinstance(parameters, dict)
                and parameters.get("remote_path") == DESTINATION)
        if name == "upload_file_by_file_uuid":
            supported = base and args.get("file_uuid") == FILE_UUID and parameters.get("file") == FILE_UUID
        else:
            binding = (args.get("visibility_context") or {}).get("_sage_prepared_binding") or {}
            supported = (base and parameters.get("file") == FILENAME
                         and binding.get("file", {}).get("agent_file_id") == FILE_UUID)
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
    """Test-only model response: recover absent UUID, then propose exact upload."""
    body = json.loads(request.content)
    messages = json.dumps(body.get("messages", []), sort_keys=True)
    offered = {item.get("function", {}).get("name") for item in body.get("tools", [])}
    if FILE_UUID not in messages:
        if "get_all_uploaded_files" not in offered:
            raise matrix.FixtureGap("recovery read not offered to Operator")
        return _seed_response(request, model, number, "get_all_uploaded_files", {}, usage=usage)
    if "upload_file_by_file_uuid" not in offered:
        raise matrix.FixtureGap("upload proposal not offered to Operator")
    return _seed_response(request, model, number, "upload_file_by_file_uuid", {
        "command": "upload", "parameters": {"file": FILE_UUID, "remote_path": DESTINATION},
        "file_uuid": FILE_UUID, "callback_display_id": 7}, usage=usage)


class SeededTransport(matrix.MatrixTransport):
    def __init__(self, *args, ledger, **kwargs):
        super().__init__(*args, **kwargs)
        if ledger is None:
            raise ValueError("shared resource ledger required")
        self.inner = resource_http.MeteredAsyncHTTPTransport(
            "provider", ledger=ledger, transport=self.inner,
            operation="payload-handoff-provider")
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
        if len(request.content) + self.output_tokens > self.inner.provider_token_reservation:
            self.rejections.append({"reason": "per_call_reservation_headroom",
                "request_bytes": len(request.content),
                "reserved_tokens": self.inner.provider_token_reservation})
            raise matrix.REAL.CaseError("per_call_reservation_headroom")
        return await super().handle_async_request(request)


async def run_case(profile: dict, output_dir: Path, shared_ledger, tls_context, ips: set[str],
                   limits=None, config_path: Path | None = None):
    limits = limits or SimpleNamespace(max_calls=4, max_tokens=98304, max_wall=120)
    path = output_dir / "case.json"
    row = {"case": CASE, "tier": "assembled LangGraph HITL", "status": "started",
           "model_sha256": digest_bytes(IMPORTED_MODEL_PATH.read_bytes()),
           "imported_model_path": str(IMPORTED_MODEL_PATH),
           "config_path": str(config_path.resolve()) if config_path is not None else None,
           "fixture_sha256": fixture_digest(), "route": {"provider": profile["provider"],
           "model": profile["model"], "endpoint_sha256": digest_bytes(profile["API_ENDPOINT"].encode())},
           "fixture_scope": "synthetic dual registration: mock build result and ordinary uploaded-file inventory; not native payload listing behavior",
           "local_seed_trace": [], "provider_requests": [], "model_calls": [],
           "fixture_reads": [], "effects": [], "accepted_local_builds": [], "proposals": []}
    def persist():
        matrix.atomic_json(path, row)
    persist()
    usage = matrix.MatrixUsage(persist)
    transport = SeededTransport(profile["API_ENDPOINT"], profile["model"], tls_context,
        max_requests=limits.max_calls, output_tokens=2048, usage_recorder=usage,
        max_observed_tokens=limits.max_tokens, request_seconds=30,
        utc_deadline=datetime.now(timezone.utc) + timedelta(seconds=limits.max_wall),
        persist=persist, ledger=shared_ledger)
    row["provider_token_reservation"] = transport.inner.provider_token_reservation
    transport.conservative_input_headroom = True
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
    sink.issue_task_and_waitfor_task_output = no_effect
    sink.upload_file_by_file_uuid = no_effect
    async def live_command(command, callback_id):
        return ({"status": "available", "command": "upload"} if command == "upload" and callback_id == 7
                else {"status": "missing", "payload_type": "apollo"})
    async def upload_schema(command, callback_id, **_kwargs):
        return UPLOAD_SCHEMA if command == "upload" and callback_id == 7 else None
    async def uploaded_file(name):
        return {"id": 1, "agent_file_id": FILE_UUID, "filename_utf8": FILENAME,
                "complete": True} if name == FILENAME else None
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
            # This inert .txt fixture is also registered as an ordinary uploaded file,
            # making the same read-only recovery path available in both source arms.
            result = json.dumps([UPLOADED_FILE_ROW], sort_keys=True)
        elif name in {"list_callbacks", "get_all_active_callbacks"}:
            result = json.dumps({"callback": [{"display_id": 7, "host": "SYNTHETIC",
                "active": True, "payload": {"payloadtype": {"name": "apollo"}}}]})
        elif name in {"get_all_command_names_for_payloadtype", "get_all_commands_for_payloadtype"}:
            result = json.dumps({"command": [{"cmd": "upload", "description": "synthetic file upload"}]})
        elif name == "get_all_command_args_for_payloadtype":
            result = json.dumps({"payload_type": "apollo", "command": "upload",
                "parameter_groups": {"Default": UPLOAD_SCHEMA[:2], "New File": UPLOAD_SCHEMA[2:]}})
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
    async_client = httpx.AsyncClient(transport=transport, timeout=30, trust_env=False)
    sync_client = httpx.Client(transport=matrix.REAL.BoundedSyncTransport(), timeout=30, trust_env=False)
    factory = model_module.init_chat_model
    def bounded_factory(*args, **kwargs):
        if kwargs.get("model_provider") != "openai" or kwargs.get("model") != profile["model"]:
            raise matrix.REAL.CaseError("unexpected_model_construction")
        kwargs.update(http_client=sync_client, http_async_client=async_client,
                      max_retries=0, timeout=30, max_tokens=2048)
        llm = factory(*args, **kwargs)
        llm.callbacks = [usage]
        return llm
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
                "_request_id": contract.request_id, "_operator_input": True})
            model.state["messages"] = [human]
            model.state["supervisor_messages"].append(human)
            config = model._graph_run_config("payload-handoff-case")
            config["recursion_limit"] = 32
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
            # One exact, local-only setup approval. The resumed build tool returns text metadata.
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
        row["status"] = "harness_gap" if isinstance(exc, matrix.FixtureGap) else "error"
        row["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        await async_client.aclose()
        sync_client.close()
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
    parser.add_argument("--max-real-calls", type=int, default=4)
    parser.add_argument("--max-observed-tokens", type=int, default=98304)
    parser.add_argument("--max-wall-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    source = digest_bytes(IMPORTED_MODEL_PATH.read_bytes())
    reservation = resource_http._provider_token_reservation()
    if not args.run:
        print(json.dumps({"status": "offline_inspection", "tier": "assembled LangGraph HITL",
            "config_path": str(args.config.resolve()),
            "source_sha256": source, "fixture_sha256": fixture_digest(),
            "max_real_calls": args.max_real_calls,
            "max_observed_tokens": args.max_observed_tokens,
            "max_wall_seconds": args.max_wall_seconds,
            "provider_token_reservation": reservation,
            "fixture_scope": "synthetic dual registration as ordinary uploaded file",
            "local_setup_approval": "one synthetic text build", "target_effects": 0}, sort_keys=True))
        return 0
    try:
        if IMPORTED_MODEL_PATH != (SAGE / "ai/langgraph/model.py").resolve():
            raise ValueError("imported model.py does not match the selected checkout")
        if (not 1 <= args.max_real_calls <= 4 or not reservation <= args.max_observed_tokens
                or args.max_observed_tokens > 250000 or not 1 <= args.max_wall_seconds <= 120):
            raise ValueError("invalid per-arm limits or token reservation exceeds arm cap")
        if not all((args.output_dir, args.expected_model,
                    args.expected_endpoint_sha256, args.expected_source_sha256)):
            raise ValueError("--run requires output and exact model, endpoint and source identities")
        profile = matrix.REAL.load_profile(args.config)
        if (profile["provider"] != "openai" or profile["model"] != args.expected_model
                or source != args.expected_source_sha256
                or digest_bytes(profile["API_ENDPOINT"].encode()) != args.expected_endpoint_sha256):
            raise ValueError("source or route identity mismatch")
        if args.output_dir.exists():
            raise ValueError("output directory already exists")
        shared = resource_budget.configured_ledger()
        if shared is None:
            raise ValueError("SAGE_RESOURCE_BUDGET_CONFIG and SAGE_RESOURCE_BUDGET_LEDGER required")
        status = shared.status()
        if status.get("unknown_provider_usage") or status.get("provider_overrun"):
            raise ValueError("shared ledger has unknown or overrun provider usage")
        ips = matrix.REAL.resolved_endpoint_ips(profile["API_ENDPOINT"])
        tls = matrix.REAL.sage_tls_context()
        args.output_dir.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="sage-payload-handoff-") as temp:
            from sage_chat import session
            with patch.object(session, "_CHANNEL_MARKER_DB_PATH", str(Path(temp) / "sage.db")):
                row = asyncio.run(asyncio.wait_for(run_case(profile, args.output_dir,
                    shared, tls, ips, SimpleNamespace(max_calls=args.max_real_calls,
                    max_tokens=args.max_observed_tokens, max_wall=args.max_wall_seconds),
                    config_path=args.config),
                    timeout=args.max_wall_seconds))
        print(json.dumps({"status": row["status"], "classification": row.get("classification"),
            "source_sha256": source, "fixture_sha256": fixture_digest(),
            "config_path": str(args.config.resolve())}))
        return 0 if row["status"] in {"observed", "no_pending_upload"} else 1
    except Exception as exc:
        print(json.dumps({"status": "configuration_or_run_error", "type": type(exc).__name__,
                          "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
