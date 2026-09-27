"""Replay one recorded Mythic Operator answer without executing Sage tools.

The default dry run reports identities only. ``--run`` sends one bounded provider
request; the caller owns paid-use authority, accounting, and evidence retention.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from langchain_core.load import loads
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import replay_supervisor_completion as shared

from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.prompt_loader import get_prompt_tools

ROOT = shared.ROOT
SAGE = shared.SAGE
START = "Interpret each result at its stated scope."
END = "\n### 4. Lowest detectable footprint, always"


def _recorded_messages(evidence: dict, call_index: int) -> list:
    calls = evidence.get("llm_calls")
    if not isinstance(calls, list) or len(calls) <= call_index:
        raise ValueError("recorded specialist call missing")
    serialized = json.loads(calls[call_index]["input"]["value"])["messages"]
    if len(serialized) != 1 or not isinstance(serialized[0], list):
        raise ValueError("unexpected recorded message structure")
    return [loads(json.dumps(message), allowed_objects="messages") for message in serialized[0]]


def evidence_messages(path: Path, case_file: Path | None = None) -> tuple[list, dict, dict]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if case_file is None:
        messages = _recorded_messages(evidence, 2)
        if (len(messages) != 4 or not isinstance(messages[0], SystemMessage)
                or not isinstance(messages[1], HumanMessage)
                or not isinstance(messages[2], AIMessage)
                or not isinstance(messages[3], ToolMessage)):
            raise ValueError("recorded specialist final-call shape changed")
        tool_calls = messages[2].tool_calls
        if (len(tool_calls) != 1 or tool_calls[0].get("name") != "get_all_task_output_by_task_id"
                or messages[3].tool_call_id != tool_calls[0].get("id")
                or messages[3].name != "get_all_task_output_by_task_id"):
            raise ValueError("recorded task-output call/result mismatch")
        return messages, evidence, {"name": "recorded_task_output", "call_index": 2,
                                    "case_sha256": None}

    case_bytes = case_file.read_bytes()
    case = json.loads(case_bytes)
    if (not isinstance(case, dict) or set(case) != {"name", "operator_request"}
            or not isinstance(case["name"], str) or not case["name"].strip()
            or not isinstance(case["operator_request"], str)
            or not case["operator_request"].strip()):
        raise ValueError("case file requires nonempty name and operator_request")
    messages = _recorded_messages(evidence, 1)
    if (len(messages) != 2 or not isinstance(messages[0], SystemMessage)
            or not isinstance(messages[1], HumanMessage)):
        raise ValueError("recorded specialist first-call shape changed")
    messages[1] = messages[1].model_copy(update={"content": case["operator_request"]})
    return messages, evidence, {"name": case["name"], "call_index": 1,
                                "case_sha256": hashlib.sha256(case_bytes).hexdigest()}


def _scope_paragraph(value: str) -> str:
    if value.count(START) != 1 or value.count(END) != 1:
        raise ValueError("evidence-scope paragraph markers are not unique")
    start = value.index(START)
    end = value.index(END, start)
    if end <= start:
        raise ValueError("evidence-scope paragraph markers are out of order")
    return value[start:end]


def candidate_messages(messages: list, prompt_path: Path) -> list:
    recorded = messages[0].content
    if not isinstance(recorded, str):
        raise ValueError("nontextual specialist system prompt")
    candidate = prompt_path.read_text(encoding="utf-8")
    old = _scope_paragraph(recorded)
    new = _scope_paragraph(candidate)
    if old == new or not new.strip():
        raise ValueError("candidate evidence-scope paragraph is unchanged or empty")
    result = list(messages)
    result[0] = messages[0].model_copy(update={"content": recorded.replace(old, new, 1)})
    return result


def specialist_tools() -> list:
    """Capture and slim production tools at the Mythic Operator builder boundary."""
    model = shared.Model.__new__(shared.Model)
    model.state = {"mythic_operator_messages": [SystemMessage(content="existing channel")]}
    model.mythic_client = MythicTools()  # no login or Mythic connection
    with (
        patch.object(shared.model_module, "create_agent", side_effect=lambda **kwargs: kwargs),
        patch.object(shared.Model, "_get_base_chat_model", return_value=object()),
        patch.object(shared.Model, "_context_middleware", return_value=[]),
        patch.object(shared.Model, "_wrap_create_agent", side_effect=lambda agent, *args: agent),
        patch.object(shared.Model, "_autonomous_execution_enabled_for_turn", return_value=False),
        patch.object(shared.model_module.prompt_context, "commands_text", return_value=""),
    ):
        captured = model._mythic_operator_agent()
    tools = captured["tools"]
    if set(tool.name for tool in tools) != set(get_prompt_tools("mythic_operator")):
        raise ValueError("production specialist tool set differs from prompt frontmatter")
    return [tool.model_copy(update={"description": shared.model_module._slim_tool_description(tool.description)})
            if tool.description != shared.model_module._slim_tool_description(tool.description)
            else tool for tool in tools]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--case-file", type=Path)
    parser.add_argument("--prompt", type=Path, default=SAGE / "prompts" / "mythic_operator.md")
    parser.add_argument("--profile", type=Path, default=SAGE / ".env.local")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true", help="send one paid model request")
    args = parser.parse_args()

    messages, evidence, case_identity = evidence_messages(args.evidence, args.case_file)
    if args.arm == "candidate":
        messages = candidate_messages(messages, args.prompt)
    tools = specialist_tools()
    schemas = [convert_to_openai_tool(tool) for tool in tools]
    profile = shared.read_profile(args.profile)
    source_call = evidence["llm_calls"][case_identity["call_index"]]
    identity = {
        "arm": args.arm, "case": case_identity,
        "model_source_sha256": hashlib.sha256((SAGE / "ai/langgraph/model.py").read_bytes()).hexdigest(),
        "mythic_tools_source_sha256": hashlib.sha256((SAGE / "ai/langgraph/mythic_tools.py").read_bytes()).hexdigest(),
        "trace_id": evidence["trace_id"],
        "recorded_input_sha256": shared.digest(json.loads(source_call["input"]["value"])),
        "message_sha256": shared.digest([message.model_dump(mode="json") for message in messages]),
        "tool_schema_sha256": shared.digest(schemas), "tool_names": [tool.name for tool in tools],
        "provider": profile["provider"], "model": profile["model"],
        "endpoint_sha256": hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest(),
        "output_cap": shared.OUTPUT_CAP, "timeout_seconds": shared.TIMEOUT_SECONDS,
        "max_retries": 0, "temperature": shared.model_module._eval_temperature(),
        "message_count": len(messages),
        "prompt_source_sha256": hashlib.sha256(args.prompt.read_bytes()).hexdigest(),
    }
    if not args.run:
        print(json.dumps({"mode": "dry_run", **identity}, indent=2))
        return
    if args.output is None:
        parser.error("--run requires --output for complete response evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps({"mode": "provider_replay", **identity,
                                 "response": {"status": "started", "usage": None}},
                                indent=2, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
        try:
            outcome = asyncio.run(shared.run_once(messages, tools, profile))
        except Exception as exc:
            outcome = {"status": "error", "error_type": type(exc).__name__,
                       "provider_requests": "unknown", "usage": None}
        result = {"mode": "provider_replay", **identity, "response": outcome}
        output.seek(0)
        output.truncate()
        output.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({"mode": "provider_replay", "arm": args.arm,
                      "case": case_identity["name"], "output": str(args.output),
                      "status": outcome["status"], "usage": outcome["usage"],
                      "tool_names": [call.get("name") for call in outcome.get("tool_calls", [])]}, indent=2))


if __name__ == "__main__":
    main()
