"""Paired, inert Certify feedback probe. Dry run unless --run is supplied.

The first Operator proposal is a labelled synthetic invalid tool call. Its
rejection is produced by Sage's assembled preparation middleware. Every later
model decision uses the configured provider; no Mythic task can be issued.
Run each source arm in a separate clean checkout/process with one shared budget.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import run_operator_prompt_matrix as matrix

from ai import resource_budget, resource_http
from ai.langgraph import command_parameter_schema, mythic_tools, ttp_library
from ai.langgraph.mythic_tools import MythicTools


SCRIPT = Path(__file__).resolve()
SAGE = matrix.SAGE
PROMPT = ("On synthetic Apollo callback 7, prepare a supervised Certify.exe "
          "template enumeration using the registered execute_assembly command. "
          "Stop at the approval card. Do not execute it.")
SEED_ARGS = {"command": "execute_assembly", "callback_display_id": 7,
             "parameters": {"Assembly": "Certify.exe", "Arguments": "find"}}
ARTIFACT = b"synthetic Certify contract fixture; never an executable"
CASE_ID = "certify_feedback"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_schema():
    return [
        {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
         "choices": ["Certify.exe"], "parameter_group_name": "Default", "required": True},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "choices": [], "parameter_group_name": "Default", "required": False},
        {"name": "assembly_file", "cli_name": "New Assembly", "type": "File",
         "choices": [], "parameter_group_name": "New Assembly", "required": True},
        {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
         "choices": [], "parameter_group_name": "New Assembly", "required": False},
    ]


def fixture_bindings(sink: MythicTools) -> None:
    """Only control-plane reads are synthetic; real contract methods stay bound."""
    sink.client = SimpleNamespace()  # Presence check only; no network handle.

    async def payload(callback_id):
        return "apollo" if callback_id == 7 else None

    async def live(command, callback_id):
        if command == "execute_assembly" and callback_id == 7:
            return {"status": "available", "command": command}
        return {"status": "missing", "payload_type": "apollo"}

    async def schema(command, callback_id, **_kwargs):
        return fixture_schema() if command == "execute_assembly" and callback_id == 7 else None

    async def metadata(*_args, **_kwargs):
        return {}

    async def file_by_name(name):
        return ({"id": 1, "agent_file_id": "00000000-0000-0000-0000-000000000001",
                 "filename_utf8": name, "complete": True}
                if name == "Certify.exe" else None)

    async def file_bytes(file_id):
        if file_id != "00000000-0000-0000-0000-000000000001":
            raise RuntimeError("fixture file identity mismatch")
        return ARTIFACT

    async def no_effect(*_args, **_kwargs):
        raise RuntimeError("fixture forbids every Mythic task effect")

    sink._resolve_payload_type = payload
    sink._authenticate_live_command = live
    sink._fetch_command_schema = schema
    sink._fetch_command_metadata = metadata
    sink._find_uploaded_file_by_name = file_by_name
    sink._download_registered_file_bytes_bounded = file_bytes
    sink._issue_mythic_task = no_effect
    sink.issue_task_and_waitfor_task_output = no_effect


async def fixture_dispatch(name, args, sink, _ledger):
    if name == "get_ttp_guidance":
        # Synthetic retrieval selector only: preserve the real producer's checked-in
        # Certify response, without embedding a hand-written answer in this fixture.
        if (not isinstance(args, dict) or set(args) != {"goal", "callback_display_id"}
                or not isinstance(args["goal"], str)
                or "certify" not in args["goal"].casefold()
                or args["callback_display_id"] != 7):
            raise matrix.FixtureGap("Certify guidance selector does not match")
        with patch.object(ttp_library, "match_goal", return_value=[("certify", 100)]):
            return await MythicTools.get_ttp_guidance(sink, **args)
    if name == "get_all_command_args_for_payloadtype":
        return await MythicTools.get_all_command_args_for_payloadtype(sink, **args)
    if name in {"list_callbacks", "get_all_active_callbacks"}:
        return json.dumps({"callback": [{"display_id": 7, "host": "SYNTHETIC",
            "user": "LAB\\analyst", "active": True,
            "payload": {"payloadtype": {"name": "apollo"}}}]})
    if name in {"get_all_command_names_for_payloadtype", "get_all_commands_for_payloadtype"}:
        return json.dumps({"command": [{"cmd": "execute_assembly",
            "description": "Registered assembly execution",
            "commandparameters": fixture_schema()}]})
    if name == "issue_task_and_waitfor_task_output":
        raise RuntimeError("fixture forbids every Mythic task effect")
    raise matrix.FixtureGap("tool unavailable in Certify fixture: " + name)


class SeededMeteredTransport(matrix.MatrixTransport):
    """One local model response, then the existing bounded transport and ledger."""

    instance = None
    configured_ledger = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.configured_ledger is None:
            raise RuntimeError("shared provider resource ledger is required")
        self.inner = resource_http.MeteredAsyncHTTPTransport(
            "provider", ledger=self.configured_ledger, transport=self.inner,
            operation="certify-feedback-recovery-provider")
        self.seeded = False
        self.seed_event = None
        type(self).instance = self

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        if not self.seeded and matrix.REAL._role_hint(body).startswith("Mythic_Operator"):
            if (request.method != "POST" or str(request.url) != self.endpoint
                    or body.get("model") != self.model
                    or body.get("max_completion_tokens", body.get("max_tokens")) != self.output_tokens):
                raise RuntimeError("synthetic seed wire boundary mismatch")
            self.seeded = True
            self.seed_event = {"kind": "local_fault_injection", "tool":
                "issue_task_and_waitfor_task_output", "args": copy.deepcopy(SEED_ARGS),
                "stream": body.get("stream") is True,
                "offered_tool_names": sorted({tool.get("function", {}).get("name")
                    for tool in body.get("tools", []) if isinstance(tool, dict)
                    and isinstance(tool.get("function"), dict)}),
                "wire_issue_schema": next((tool.get("function", {}).get("parameters")
                    for tool in body.get("tools", []) if isinstance(tool, dict)
                    and tool.get("function", {}).get("name") ==
                    "issue_task_and_waitfor_task_output"), None)}
            self.persist()
            tool_call = {"id": "injected-invalid-find", "type": "function",
                "function": {"name": "issue_task_and_waitfor_task_output",
                    "arguments": json.dumps(SEED_ARGS, sort_keys=True)}}
            if body.get("stream") is True:
                chunks = [
                    {"id": "synthetic-invalid-certify-proposal", "object": "chat.completion.chunk",
                     "created": 0, "model": self.model,
                     "choices": [{"index": 0, "delta": {"role": "assistant",
                         "tool_calls": [{"index": 0, **tool_call}]}, "finish_reason": None}]},
                    {"id": "synthetic-invalid-certify-proposal", "object": "chat.completion.chunk",
                     "created": 0, "model": self.model,
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                    {"id": "synthetic-invalid-certify-proposal", "object": "chat.completion.chunk",
                     "created": 0, "model": self.model, "choices": [],
                     "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
                ]
                content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                return httpx.Response(200, request=request, content=content.encode(),
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, request=request, json={
                "id": "synthetic-invalid-certify-proposal", "object": "chat.completion",
                "created": 0, "model": self.model,
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [tool_call]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        return await super().handle_async_request(request)


def classify(result: dict, seed: dict | None) -> dict:
    requests = result.get("provider_requests", [])
    input_text = json.dumps([row.get("request", {}).get("messages") for row in requests],
                            sort_keys=True)
    operator_requests = [row for row in requests
                         if str(row.get("role_hint", "")).startswith("Mythic_Operator")]
    operator_input_text = json.dumps(
        [row.get("request", {}).get("messages") for row in operator_requests], sort_keys=True)
    preparation_error = (
        "[task preparation] Approval was not requested: executable verb 'find' is not enrolled")
    errors = [item.get("result") for item in result.get("tool_events", [])
              if item.get("tool") == "get_all_command_args_for_payloadtype"
              and "not enrolled" in str(item.get("result"))]
    cards = result.get("initial_cards", [])
    actions = matrix.card_actions(cards) if cards else []
    def valid_action(action):
        if not isinstance(action, dict) or action.get("tool_name") != "issue_task_and_waitfor_task_output":
            return False
        args = action.get("arguments")
        if not isinstance(args, dict) or args.get("command") != "execute_assembly" or args.get("callback_display_id") != 7:
            return False
        parameters = args.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("Assembly") != "Certify.exe":
            return False
        invocation = parameters.get("Arguments")
        return (isinstance(invocation, str) and invocation in {
                    "enum-templates", "enum-templates --filter-vulnerable"} or
                isinstance(invocation, dict) and invocation.get("verb") == "enum-templates"
                and invocation.get("options") in ({}, {"filter-vulnerable": True}))
    valid = any(valid_action(action) for action in actions)
    def typed_schema(row):
        tools = row.get("request", {}).get("tools", [])
        for tool in tools:
            function = tool.get("function", {}) if isinstance(tool, dict) else {}
            if function.get("name") != "issue_task_and_waitfor_task_output":
                continue
            schema = function.get("parameters", {})
            try:
                verb = schema["properties"]["parameters"]["properties"]["Arguments"]["properties"]["verb"]
            except (KeyError, TypeError):
                continue
            if verb.get("const", verb.get("enum")) in ("enum-templates", ["enum-templates"]):
                return True
        return False
    return {"seeded": seed is not None,
            "actual_preparation_feedback_seen": preparation_error in input_text,
            "operator_provider_request_count": len(operator_requests),
            "operator_preparation_feedback_seen": preparation_error in operator_input_text,
            "enrolled_alternatives_seen": "enrolled verbs for this artifact:" in input_text,
            "typed_enum_templates_schema_on_wire": any(typed_schema(row) for row in requests),
            "discovery_errors": errors, "valid_enum_templates_pending_card": valid,
            "pending_card_count": len(cards), "effect_count": len(result.get("effects", [])),
            "paid_provider_request_count": len(result.get("provider_requests", []))}


async def run_case(profile, output_dir, ledger, limits, tls_context, ips, contract_root):
    source = SAGE / "tool_contracts" / "certify.json"
    descriptor = json.loads(source.read_text(encoding="utf-8"))
    descriptor["binary"]["sha256"] = hashlib.sha256(ARTIFACT).hexdigest()
    contract_root.mkdir()
    (contract_root / "certify.json").write_text(json.dumps(descriptor), encoding="utf-8")
    async def grouped(_client, payload, command):
        if payload != "apollo" or command != "execute_assembly":
            return None
        return {"Default": {"parameters": fixture_schema()[:2]},
                "New Assembly": {"parameters": fixture_schema()[2:]}}
    def setup(model, row):
        fixture_bindings(model.mythic_client)
        row["fixture_artifact_sha256"] = hashlib.sha256(ARTIFACT).hexdigest()
        row["source_contract_sha256"] = sha256(source)
    SeededMeteredTransport.configured_ledger = ledger
    SeededMeteredTransport.instance = None
    with (patch.object(matrix, "MatrixTransport", SeededMeteredTransport),
          patch.object(mythic_tools, "_EXECUTABLE_CONTRACT_ROOT", contract_root),
          patch.object(command_parameter_schema, "get_command_schema", grouped)):
        result = await matrix.run_case(CASE_ID, PROMPT, None, profile, output_dir, limits,
            tls_context, ips, fixture_dispatch=fixture_dispatch, before_build=setup,
            fixture_metadata={"fault_injection": "first Operator proposal only",
                              "no_mythic_task_effects": True})
    transport = SeededMeteredTransport.instance
    seed = transport.seed_event if transport is not None else None
    result["injected_proposal"] = seed
    result["classification"] = classify(result, seed)
    matrix.atomic_json(output_dir / f"case_{CASE_ID}.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--expected-endpoint-sha256", required=True)
    parser.add_argument("--expected-source-sha256", required=True,
                        help="SHA256 of this arm's executable_contracts.py")
    args = parser.parse_args(argv)
    try:
        profile = matrix.REAL.load_profile(args.config)
        source_hash = sha256(SAGE / "ai/langgraph/executable_contracts.py")
        route_hash = hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest()
        if (profile["provider"] != "openai" or profile["model"] != args.expected_model
                or route_hash != args.expected_endpoint_sha256
                or source_hash != args.expected_source_sha256):
            raise ValueError("source or route identity mismatch")
        if args.output_dir.exists():
            raise ValueError("output directory already exists; one attempt per arm")
        if not args.run:
            print(json.dumps({"status": "dry_run_ready", "source_sha256": source_hash,
                              "model": profile["model"], "endpoint_sha256": route_hash}))
            return 0
        ledger = resource_budget.configured_ledger()
        if ledger is None:
            raise ValueError("SAGE_RESOURCE_BUDGET_CONFIG and SAGE_RESOURCE_BUDGET_LEDGER are required")
        status = ledger.status()
        if status.get("unknown_provider_usage") or status.get("provider_overrun"):
            raise ValueError("shared ledger has unknown or overrun provider usage")
        args.output_dir.mkdir(parents=True)
        ips = matrix.REAL.resolved_endpoint_ips(profile["API_ENDPOINT"])
        tls_context = matrix.REAL.sage_tls_context()
        limits = SimpleNamespace(max_case_calls=8, max_calls=8,
            max_case_tokens=250000, max_tokens=250000, output_tokens=2048,
            request_seconds=30, max_wall=420)
        with tempfile.TemporaryDirectory(prefix="sage-certify-feedback-") as temp:
            from sage_chat import session
            scratch = Path(temp)
            with patch.object(session, "_CHANNEL_MARKER_DB_PATH", str(scratch / "sage.db")):
                result = asyncio.run(asyncio.wait_for(run_case(profile, args.output_dir,
                    ledger, limits, tls_context, ips, scratch / "contracts"), timeout=420))
        summary = {"status": result["status"], "classification": result["classification"],
                   "source_sha256": source_hash, "route": {"provider": profile["provider"],
                   "model": profile["model"], "endpoint_sha256": route_hash}}
        matrix.atomic_json(args.output_dir / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
        return 0 if result["status"] == "observed" else 1
    except Exception as exc:
        print(json.dumps({"status": "configuration_or_run_error", "type": type(exc).__name__,
                          "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
