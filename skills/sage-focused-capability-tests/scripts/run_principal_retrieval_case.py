"""One inert principal-retrieval native chat case. Inspect builds the graph offline."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ConfigDict, create_model


SCRIPT = Path(__file__).with_name("run_operator_prompt_matrix.py")
spec = importlib.util.spec_from_file_location("principal_matrix", SCRIPT)
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)
from ai.mcp import MCPManager  # noqa: E402
from ai.langgraph import mcp_tool_policy  # noqa: E402
from ai.langgraph.turn_authority import TurnAuthority  # noqa: E402
from ai.langgraph.request_contract import build_request_contract  # noqa: E402


def discard_current_stdout_handlers():
    from mythic_container.logging import logger
    for handler in list(logger.handlers):
        if handler.stream is sys.stdout or getattr(handler.stream, "closed", False):
            logger.removeHandler(handler)


@contextmanager
def isolated_case():
    """Keep native channel/session state in disposable storage for the whole case."""
    from sage_chat import session
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="sage-principal-case-") as scratch:
        with patch.object(session, "_CHANNEL_MARKER_DB_PATH", str(Path(scratch) / "sage.db")):
            try:
                os.chdir(scratch)
                yield Path(scratch)
            finally:
                os.chdir(previous)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             default=str).encode()).hexdigest()


def load_fixture(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if (fixture.get("schema") != "sage-principal-fixture-v1"
            or not isinstance(fixture.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", fixture["id"])
            or not isinstance(fixture.get("prompt"), str) or not fixture["prompt"].strip()
            or not isinstance(fixture.get("callbacks"), list)
            or not isinstance(fixture.get("mythic_responses"), list)
            or not isinstance(fixture.get("mcp_tools"), list)
            or not isinstance(fixture.get("mcp_responses"), list)
            or not isinstance(fixture.get("expected"), dict)):
        raise ValueError("invalid principal fixture")
    local = fixture.get("local_reference_tools", [])
    if not isinstance(local, list) or any(name not in {
            "get_ttp_guidance", "get_ttp_full_reference", "list_ttp_categories"} for name in local):
        raise ValueError("invalid local reference allowlist")
    for category in ("mythic_responses", "mcp_responses"):
        for row in fixture[category]:
            if not isinstance(row, dict) or not isinstance(row.get("tool"), str) or not isinstance(row.get("arguments"), dict) or "result" not in row:
                raise ValueError("invalid fixture response")
    if "search_nodes" in fixture:
        if (not isinstance(fixture["search_nodes"], list)
                or any(not isinstance(node, dict)
                       or any(not isinstance(node.get(key), str) or not node[key]
                              for key in ("name", "objectID", "label"))
                       for node in fixture["search_nodes"])):
            raise ValueError("invalid synthetic search inventory")
    names = set()
    for row in fixture["mcp_tools"]:
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or not isinstance(row.get("description"), str)
                or not isinstance(row.get("parameters"), dict)
                or row["parameters"].get("type") != "object"
                or not isinstance(row["parameters"].get("properties", {}), dict)
                or row["name"] in names
                or row["name"] not in matrix.model_module._BLOODHOUND_AGENT_TOOL_ALLOWLIST
                or mcp_tool_policy.is_mcp_tool_guarded("BloodHound", row["name"])):
            raise ValueError("invalid MCP tool schema")
        names.add(row["name"])
    fixture["fixture_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fixture


def normalized(arguments: dict, parameters: dict | None = None) -> dict:
    if not isinstance(arguments, dict):
        raise matrix.FixtureGap("non-object tool arguments")
    properties = (parameters or {}).get("properties", {})
    allowed = set(properties)
    if parameters is not None and set(arguments) - allowed:
        raise matrix.FixtureGap("undeclared tool arguments")
    return {key: value for key, value in arguments.items()
            if value is not None and not (key in properties and
                "default" in properties[key] and value == properties[key]["default"])}


class Fixture:
    def __init__(self, source: dict):
        self.source = source
        self.schemas = {row["name"]: row["parameters"] for row in source["mcp_tools"]}
        self.mythic_schemas: dict[str, dict] = {}
        self.events: list[dict] = []

    def bind_mythic_schemas(self, sink):
        names = sorted({row["tool"] for row in self.source["mythic_responses"]})
        self.mythic_schemas = {tool.name: tool.args_schema.model_json_schema()
            for tool in matrix.MythicTools.get_tools(sink, names)}

    def answer(self, kind: str, name: str, arguments: dict):
        schema = self.schemas.get(name) if kind == "mcp" else self.mythic_schemas.get(name)
        actual = normalized(arguments, schema)
        if (kind == "mcp" and name in self.schemas and name in {"graph_analysis", "domain_info"}
                and "search_nodes" in self.source and actual.get("info_type") == "search"):
            result = self.search(name, actual)
            self.events.append({"source": kind, "tool": name, "arguments": arguments,
                                "result": copy.deepcopy(result), "synthetic_search": True})
            return json.dumps(result, sort_keys=True)
        for row in self.source[kind + "_responses"]:
            if row["tool"] == name and normalized(row["arguments"], schema) == actual:
                result = copy.deepcopy(row["result"])
                self.events.append({"source": kind, "tool": name, "arguments": arguments,
                                    "result": result})
                return result if isinstance(result, str) else json.dumps(result, sort_keys=True)
        raise matrix.FixtureGap(f"out-of-fixture {kind} request: {name} {digest(actual)}")

    def search(self, name: str, arguments: dict) -> dict:
        if name not in {"graph_analysis", "domain_info"} or arguments.get("info_type") != "search":
            raise matrix.FixtureGap("unsupported synthetic search type")
        allowed = ({"info_type", "query", "search_type", "skip", "limit"} if name == "graph_analysis"
                   else {"info_type", "query", "object_type", "skip", "limit"})
        if set(arguments) - allowed:
            raise matrix.FixtureGap("unsupported synthetic search arguments")
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise matrix.FixtureGap("synthetic search requires a nonempty query")
        mode = arguments.get("search_type", "fuzzy")
        if mode not in {"fuzzy", "exact"}:
            raise matrix.FixtureGap("unsupported synthetic search mode")
        skip, limit = arguments.get("skip", 0), arguments.get("limit", 100)
        if type(skip) is not int or skip < 0 or type(limit) is not int or limit < 0:
            raise matrix.FixtureGap("invalid synthetic search pagination")
        label = arguments.get("object_type")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise matrix.FixtureGap("invalid synthetic search object type")
        needle = query.strip().casefold()
        matching = []
        for node in self.source["search_nodes"]:
            name_match = (node["name"].casefold() == needle if mode == "exact"
                          else needle in node["name"].casefold())
            if ((name_match or node["objectID"].casefold() == needle)
                    and (label is None or node["label"].casefold() == label.casefold())):
                matching.append(copy.deepcopy(node))
        return {"info_type": "search", "data": {"count": len(matching), "limit": limit,
            "skip": skip, "data": matching[skip:skip + limit]}}

    def mcp_tools(self) -> list[StructuredTool]:
        result = []
        for row in self.source["mcp_tools"]:
            params = row["parameters"]
            required = set(params.get("required", []))
            types = {"string": str, "integer": int, "number": float, "boolean": bool,
                     "array": list, "object": dict}
            fields = {}
            for key, value in params.get("properties", {}).items():
                if value.get("type") not in types:
                    raise ValueError("unsupported MCP property type")
                field_type = types[value["type"]]
                default = ... if key in required else value.get("default", None)
                if default is None and key not in required:
                    field_type = field_type | None
                fields[key] = (field_type, default)
            schema = create_model("Fixture_" + re.sub(r"\W", "_", row["name"]),
                                  __config__=ConfigDict(extra="forbid"), **fields)
            async def call(__name=row["name"], **kwargs):
                return self.answer("mcp", __name, kwargs)
            result.append(StructuredTool.from_function(coroutine=call, name=row["name"],
                description=row["description"], args_schema=schema))
        return result


def mcp_patches(stack: ExitStack, tools: list[StructuredTool]):
    stack.enter_context(patch.object(MCPManager, "get_connected_servers", return_value=["BloodHound"]))
    stack.enter_context(patch.object(MCPManager, "is_bloodhound_server", side_effect=lambda name: name == "BloodHound"))
    stack.enter_context(patch.object(MCPManager, "get_tools_by_server", side_effect=lambda name: tools if name == "BloodHound" else []))
    stack.enter_context(patch.object(MCPManager, "get_tools_summary", return_value={
        "total_tools": len(tools), "connected_servers": 1,
        "server_summaries": {"BloodHound": {"tool_count": len(tools),
            "tool_names": [tool.name for tool in tools]}}}))


def source_identity() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=matrix.ROOT,
                          text=True, capture_output=True, check=True).stdout.strip()
    diff = subprocess.run(["git", "diff", "HEAD", "--", "Payload_Type/sage/ai/langgraph",
                           "Payload_Type/sage/prompts", "Payload_Type/sage/sage_chat"],
                          cwd=matrix.ROOT, capture_output=True, check=True).stdout
    return {"head": head, "relevant_diff_sha256": hashlib.sha256(diff).hexdigest()}


def prompt_hashes() -> dict:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((matrix.SAGE / "prompts").glob("*.md"))}


def component_seed(path: Path, insertion_path: Path | None = None) -> dict:
    retained = json.loads(path.read_text(encoding="utf-8"))
    messages = copy.deepcopy(retained["model_calls"][5]["input_messages"][0])
    expected = copy.deepcopy(retained["provider_requests"][5]["request"])
    if len(messages) != 3 or [row["type"] for row in messages] != ["system", "ai", "human"]:
        raise ValueError("unexpected retained BloodHound input shape")
    if len(expected.get("tools", [])) != 9:
        raise ValueError("retained BloodHound wire schema count changed")
    original = expected["messages"][0]["content"]
    if messages[0]["content"] != original:
        raise ValueError("retained model input and provider system text differ")
    marker = "\n\nSage runtime context. "
    if original.count(marker) != 1:
        raise ValueError("retained runtime context boundary changed")
    base, runtime = original.split(marker, 1)
    candidate = insertion_path.read_text(encoding="utf-8") if insertion_path else original
    if insertion_path:
        import difflib
        edits = [(tag, a, b, c, d) for tag, a, b, c, d in
                 difflib.SequenceMatcher(a=original, b=candidate, autojunk=False).get_opcodes()
                 if tag != "equal"]
        if len(edits) != 1 or edits[0][0] != "insert" or edits[0][1] > len(base):
            raise ValueError("candidate system text must make one insertion before runtime context")
        _, at, _, start, end = edits[0]
        if not 1 <= len([word for word in candidate[start:end].split()
                         if re.search(r"\w", word)]) <= 150:
            raise ValueError("candidate insertion must contain 1–150 words")
        base = base[:at] + candidate[start:end] + base[at:]
        expected["messages"][0]["content"] = candidate
    authority_start = runtime.find("{\"attempts_remaining\":")
    if authority_start < 0:
        raise ValueError("retained turn authority missing")
    authority_payload, authority_end = json.JSONDecoder().raw_decode(runtime[authority_start:])
    authority = TurnAuthority(mode=authority_payload["mode"],
        turn_id=authority_payload["turn_id"],
        prompt_fingerprint=authority_payload["prompt_fingerprint"],
        request_id=authority_payload["request_id"],
        request_contract_digest=authority_payload["request_contract_digest"])
    contract = build_request_contract(request_id=authority.request_id,
        channel_id=1, operation_id=1, mode="supervised", autonomous_solve=False)
    if contract.digest != authority.request_contract_digest:
        raise ValueError("retained request contract cannot be reproduced")
    rendered = authority.render_ephemeral()
    projection = runtime[authority_start + authority_end:]
    if not projection.startswith("\n[supervised-runtime]"):
        raise ValueError("retained supervised projection missing")
    if not ("Sage runtime context. The following turn-authority block is generated by Sage, "
            "not supplied by the operator.\n" + rendered + projection) == "Sage runtime context. " + runtime:
        raise ValueError("retained authority cannot be reproduced")
    messages[0]["content"] = base
    return {"messages": messages, "expected_wire": expected,
            "authority": authority, "request_contract": contract,
            "supervised_projection": projection[1:],
            "retained_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "insertion_sha256": hashlib.sha256((candidate if insertion_path else "").encode()).hexdigest()
            if insertion_path else None}


def _wire_difference(expected, actual, path="wire") -> str | None:
    if type(expected) is not type(actual):
        return path + ":type"
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            return path + ":keys"
        for key in sorted(expected):
            difference = _wire_difference(expected[key], actual[key], path + "." + str(key))
            if difference:
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return path + ":length"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _wire_difference(left, right, path + f"[{index}]")
            if difference:
                return difference
    elif expected != actual:
        return path
    return None


def setup(model, ledger, tools):
    ledger["mcp_tool_schemas"] = {tool.name: tool.args_schema.model_json_schema() for tool in tools}
    ledger["mcp_schema_sha256"] = {name: digest(schema) for name, schema in ledger["mcp_tool_schemas"].items()}
    ledger["graph_nodes_expected"] = ["Supervisor", "BloodHound"]


def built_graph(model, ledger):
    ledger["graph_nodes"] = sorted(model.graph.nodes)
    ledger["rendered_prompt_sha256"] = {
        channel: [hashlib.sha256(str(message.content).encode()).hexdigest()
                  for message in model.state.get(channel, []) if message.type == "system"]
        for channel in ("supervisor_messages", "bloodhound_messages", "mythic_operator_messages")}


async def inspect(fixture: Fixture) -> dict:
    discard_current_stdout_handlers()
    tools = fixture.mcp_tools()
    fake = matrix.CASE.ScriptedModel(messages=iter([]))
    with isolated_case(), ExitStack() as stack:
        mcp_patches(stack, tools)
        stack.enter_context(patch.object(matrix.model_module, "init_chat_model", return_value=fake))
        model = matrix.Model(provider="openai", model="scripted-inspect", system_prompt="",
            config={"configurable": {"api_key": "offline", "base_url": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="principal-inspect", mode="supervised",
            autonomous_solve=False, policy_mode="hybrid", operation_id=1, channel_id=1)
        sink = matrix.MythicTools(agent_task_id="principal-inspect", operation_id=1, channel_id=1)
        def inert_tools(names):
            result = []
            for original in matrix.MythicTools.get_tools(sink, names):
                async def unavailable(**_kwargs):
                    raise matrix.FixtureGap("inspect is graph-build only")
                result.append(StructuredTool.from_function(coroutine=unavailable,
                    name=original.name, description=original.description,
                    args_schema=original.args_schema))
            return result
        sink.get_tools = inert_tools
        async def unavailable_callbacks():
            raise matrix.FixtureGap("inspect is graph-build only")
        sink.list_callbacks = unavailable_callbacks
        model.mythic_client = sink
        model._payload_names = ["apollo"]
        model.memory = InMemorySaver()
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        ledger = {}
        setup(model, ledger, tools)
        model._rebuild_graph()
        built_graph(model, ledger)
        nodes = sorted(model.graph.nodes)
    discard_current_stdout_handlers()
    return {"status": "inspect_ready", "mode": "scripted_graph_build_only", "fixture_id": fixture.source["id"],
            "fixture_sha256": fixture.source["fixture_sha256"], "prompt_sha256": prompt_hashes(),
            "source": source_identity(), "graph_nodes": nodes,
            "mcp_schema_sha256": ledger["mcp_schema_sha256"],
            "declared_mcp_schema_sha256": {row["name"]: digest(row["parameters"])
                for row in fixture.source["mcp_tools"]},
            "rendered_prompt_sha256": ledger["rendered_prompt_sha256"],
            "mcp_tool_names": [tool.name for tool in tools],
            "mythic_fixture_tools": sorted({row["tool"] for row in fixture.source["mythic_responses"]}),
            "callback_ids": [row.get("id", row.get("display_id")) for row in fixture.source["callbacks"]]}


async def execute(fixture: Fixture, profile: dict, output: Path, limits, ips, tls,
                  *, component: dict | None = None, wire_check_only: bool = False):
    discard_current_stdout_handlers()
    tools = fixture.mcp_tools()
    async def dispatch(name, args, sink, ledger):
        if name == "issue_task_and_waitfor_task_output":
            ledger["effects"].append({"tool": name, "arguments": args, "blocked": True})
            raise matrix.FixtureGap("target task forbidden")
        if name in fixture.source.get("local_reference_tools", []):
            if name == "get_ttp_guidance":
                ids = [row.get("id", row.get("display_id")) for row in fixture.source["callbacks"]]
                if args.get("callback_display_id") not in ids:
                    raise matrix.FixtureGap("out-of-fixture callback for local reference")
                result = await matrix.MythicTools.get_ttp_guidance(sink, **args)
            elif name == "get_ttp_full_reference":
                result = await matrix.MythicTools.get_ttp_full_reference(sink, **args)
            else:
                result = await matrix.MythicTools.list_ttp_categories(sink)
            return result
        result = fixture.answer("mythic", name, args)
        if name == "get_all_task_output_by_task_id":
            payload = json.loads(result)
            if not isinstance(payload, dict) or not isinstance(payload.get("task_output"), list):
                raise matrix.FixtureGap("invalid task output fixture payload")
            payload["provenance"] = sink._note_task_output_read(args.get("task_id"))
            return json.dumps(payload, sort_keys=True)
        if name == "get_task_history_for_callback":
            payload = json.loads(result)
            if not isinstance(payload, list):
                raise matrix.FixtureGap("invalid task history fixture payload")
            for row in payload:
                if not isinstance(row, dict):
                    raise matrix.FixtureGap("invalid task history row")
                row["request_provenance"] = sink._task_request_provenance(row.get("display_id"))
            if any(row["request_provenance"]["origin"] in
                   {"prior_sage_request", "historical_or_external"} for row in payload):
                sink._historical_task_history_reads.add(sink._current_request_id())
            return json.dumps(payload, sort_keys=True)
        return result
    metadata = {"synthetic_fixture": fixture.source["schema"], "fixture_id": fixture.source["id"],
                "fixture_sha256": fixture.source["fixture_sha256"], "prompt_sha256": prompt_hashes(),
                "source": source_identity(), "mcp_events": fixture.events,
                "declared_mcp_schema_sha256": {row["name"]: digest(row["parameters"])
                    for row in fixture.source["mcp_tools"]},
                "execution_mode": "real_model"}
    wire_status = {}
    if component is not None:
        metadata.update(execution_mode="component_diagnostic", user_visible_proposal=False,
                        component_wire=wire_status,
                        component_seed_sha256=component["retained_sha256"],
                        component_insertion_sha256=component["insertion_sha256"],
                        component_supervised_projection={"source": "retained_first_wire",
                            "sha256": hashlib.sha256(component["supervised_projection"].encode()).hexdigest(),
                            "harness_replay": True})
    def before_build(model, ledger):
        setup(model, ledger, tools)
        fixture.bind_mythic_schemas(model.mythic_client)
        if component is not None:
            types = {"system": SystemMessage, "ai": AIMessage, "human": HumanMessage}
            model.state["bloodhound_messages"] = [types[row["type"]](
                content=row["content"], name=row.get("name"),
                additional_kwargs=copy.deepcopy(row["additional_kwargs"]))
                for row in component["messages"]]
            model._graph_start_node_for_turn = lambda: "BloodHound"
            model.install_request_contract(component["request_contract"])
            if model._turn_authority.render_ephemeral() != component["authority"].render_ephemeral():
                raise ValueError("component turn authority projection changed")
            model.state["supervisor_messages"] = [HumanMessage(
                content=component["messages"][-1]["content"], additional_kwargs={
                    "_operator_input": True, "_operator_input_revision": 0,
                    "_request_id": component["request_contract"].request_id})]
            model._supervised_execution_context = lambda: component["supervised_projection"]
        async def payload_type(callback_display_id):
            ids = [row.get("id", row.get("display_id")) for row in fixture.source["callbacks"]]
            return "apollo" if callback_display_id in ids else None
        model.mythic_client._resolve_payload_type = payload_type
    first_wire = True
    def wire_guard(body):
        nonlocal first_wire
        if not first_wire:
            return
        first_wire = False
        difference = _wire_difference(component["expected_wire"], body)
        wire_status.update(expected_sha256=digest(component["expected_wire"]),
            actual_sha256=digest(body), tool_count=len(body.get("tools", [])),
            difference=difference)
        if difference:
            if difference == "wire.messages[0].content":
                expected_text = component["expected_wire"]["messages"][0]["content"]
                actual_text = body["messages"][0]["content"]
                offset = next((index for index, pair in enumerate(zip(expected_text, actual_text))
                               if pair[0] != pair[1]), min(len(expected_text), len(actual_text)))
                wire_status["system_difference"] = {"offset": offset,
                    "expected_length": len(expected_text), "actual_length": len(actual_text),
                    "expected_excerpt": expected_text[max(0, offset - 50):offset + 100],
                    "actual_excerpt": actual_text[max(0, offset - 50):offset + 100]}
            raise matrix.FixtureGap("component wire mismatch at " + difference)
        if wire_check_only:
            raise matrix.WireIntercept("component first request verified offline")
    try:
        with isolated_case(), ExitStack() as stack:
            mcp_patches(stack, tools)
            result = await matrix.run_case(fixture.source["id"], fixture.source["prompt"], None,
                profile, output, limits, tls, ips, fixture_dispatch=dispatch,
                before_build=before_build, after_build=built_graph, fixture_metadata=metadata,
                component_run=component is not None,
                wire_guard=wire_guard if component is not None else None)
            if wire_check_only and wire_status.get("difference") is None and wire_status.get("actual_sha256") \
                    and not result["provider_requests"]:
                result["status"] = "component_wire_verified"
                result["error"] = None
            return result
    finally:
        discard_current_stdout_handlers()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--component-seed", type=Path)
    parser.add_argument("--component-system-insertion", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-endpoint-sha256")
    parser.add_argument("--deadline-epoch", type=float)
    parser.add_argument("--max-case-calls", type=int)
    for flag in ("max-calls", "max-tokens", "max-wall", "output-tokens", "request-seconds"):
        parser.add_argument("--" + flag, type=int)
    args = parser.parse_args(argv)
    try:
        fixture = Fixture(load_fixture(args.fixture))
        if args.component_system_insertion and not args.component_seed:
            raise ValueError("component insertion requires retained component seed")
        component = (component_seed(args.component_seed, args.component_system_insertion)
                     if args.component_seed else None)
        if not args.execute:
            if component is not None:
                profile = {"provider": "openai", "model": component["expected_wire"]["model"],
                    "API_ENDPOINT": "https://127.0.0.1:9/v1", "API_KEY": "offline"}
                limits = SimpleNamespace(max_case_calls=1, max_calls=1, max_case_tokens=131072,
                    max_tokens=131072, max_wall=30, output_tokens=component["expected_wire"]["max_completion_tokens"],
                    request_seconds=5)
                with tempfile.TemporaryDirectory(prefix="sage-principal-wire-") as scratch:
                    result = asyncio.run(execute(fixture, profile, Path(scratch), limits,
                        {"127.0.0.1"}, None, component=component, wire_check_only=True))
                print(json.dumps({"status": result["status"],
                    "component_wire": result.get("component_wire"),
                    "error": result.get("error"),
                    "provider_requests": len(result["provider_requests"])}))
                return 0 if result["status"] == "component_wire_verified" else 2
            print(json.dumps(asyncio.run(inspect(fixture)), sort_keys=True))
            return 0
        if not args.config or not args.output_dir or not args.output_dir.is_absolute():
            raise ValueError("execute requires profile and absolute shared output directory")
        values = (args.max_calls, args.max_tokens, args.max_wall, args.output_tokens, args.request_seconds)
        if any(value is None or value <= 0 for value in values):
            raise ValueError("execute requires positive explicit call/token/wall/output/request limits")
        if args.max_case_calls is None or not 0 < args.max_case_calls <= args.max_calls:
            raise ValueError("execute requires a positive per-case call limit within shared calls")
        if args.deadline_epoch is None or args.deadline_epoch <= time.time():
            raise ValueError("execute requires a future shared absolute deadline epoch")
        profile = matrix.REAL.load_profile(args.config)
        if (profile["model"] != args.expected_model or
                hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest() != args.expected_endpoint_sha256):
            raise ValueError("exact route binding mismatch")
        output = args.output_dir
        if (output / f"case_{fixture.source['id']}.json").exists():
            raise ValueError("case ID already exists; no overwrite or retry")
        calls, tokens, unknown = matrix.totals(output) if output.exists() else (0, 0, False)
        if unknown or calls >= args.max_calls or tokens >= args.max_tokens:
            raise ValueError("unknown or exhausted shared usage")
        output.mkdir(parents=True, exist_ok=True)
        args.max_case_calls = min(args.max_case_calls, args.max_calls - calls)
        args.max_case_tokens = args.max_tokens - tokens
        ips = matrix.REAL.resolved_endpoint_ips(profile["API_ENDPOINT"])
        tls = matrix.REAL.sage_tls_context()
        args.max_wall = min(args.max_wall, args.deadline_epoch - time.time())
        result = asyncio.run(asyncio.wait_for(execute(fixture, profile, output, args, ips, tls,
                                  component=component),
                                              timeout=args.max_wall))
        print(json.dumps({"status": result["status"], "case_id": fixture.source["id"],
                          "provider_requests": len(result["provider_requests"]),
                          "effect_attempts": len(result["effects"]),
                          "approval_cards": len(result.get("initial_cards", []))}))
        allowed_status = "component_observed" if component is not None else "observed"
        return 0 if result["status"] == allowed_status and not result["effects"] and not result.get("initial_cards") else 1
    except Exception as exc:
        print(json.dumps({"status": "configuration_error", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
