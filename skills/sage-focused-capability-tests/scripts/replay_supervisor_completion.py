"""Replay one recorded Supervisor completion decision without executing Sage tools.

Default is an offline identity check. ``--run`` makes exactly one bounded provider
request. The caller owns paid-use authority, accounting, and evidence retention.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
import time
from unittest.mock import patch

import httpx
from langchain_core.load import loads
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain.chat_models import init_chat_model

ROOT = Path(__file__).resolve().parents[3]
SAGE = ROOT / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE))

from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from ssl_bootstrap import configure_sage_ssl_cert_file

OUTPUT_CAP = 1024
TIMEOUT_SECONDS = 60
HEADER = "            **CRITICAL: Streaming Output Context:**\n"
NEXT_BULLET = "            - If exactly one specialist has returned"
PARAGRAPH_END = "            - "
RECORDED_CONTINUATION = (
    "Internal conversation continuation. The operator's request and authorization are unchanged."
)


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def read_profile(path: Path) -> dict[str, str]:
    wanted = {"provider", "model", "API_ENDPOINT", "API_KEY"}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if key in wanted:
            if not sep or key in values:
                raise ValueError("invalid or duplicate provider setting")
            values[key] = value.strip().strip('"\'')
    if any(not values.get(k) for k in wanted) or values["provider"] != "openai":
        raise ValueError("expected complete OpenAI-compatible Sage profile")
    return values


def evidence_messages(path: Path) -> tuple[list, dict]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    calls = evidence["llm_calls"]
    if len(calls) < 1:
        raise ValueError("no recorded model calls")
    serialized = json.loads(calls[-1]["input"]["value"])["messages"]
    if len(serialized) != 1 or not isinstance(serialized[0], list):
        raise ValueError("unexpected recorded message structure")
    # Restrict deserialization to messages; the retained trace is data, not code.
    messages = [loads(json.dumps(m), allowed_objects="messages") for m in serialized[0]]
    if not messages or not isinstance(messages[0], SystemMessage):
        raise ValueError("missing recorded Supervisor system prompt")
    return messages, evidence


def candidate_messages(messages: list, prompt_path: Path) -> list:
    recorded = messages[0].content
    candidate = prompt_path.read_text(encoding="utf-8")
    if not isinstance(recorded, str):
        raise ValueError("nontextual Supervisor prompt")
    for value in (recorded, candidate):
        if value.count(HEADER) != 1:
            raise ValueError("streaming paragraph marker is not unique")
        tail = value.split(HEADER, 1)[1]
        if PARAGRAPH_END not in tail:
            raise ValueError("completion instructions after streaming paragraph missing")
    old = recorded.split(HEADER, 1)[1].split(PARAGRAPH_END, 1)[0]
    new = candidate.split(HEADER, 1)[1].split(PARAGRAPH_END, 1)[0]
    if not old.strip() or not new.strip() or old == new:
        raise ValueError("candidate streaming paragraph is unchanged or empty")
    updated = recorded.replace(HEADER + old, HEADER + new, 1)
    result = list(messages)
    result[0] = messages[0].model_copy(update={"content": updated})
    return result


def supervisor_continuation_messages(messages: list) -> list:
    """Change only a final, runtime-tagged provider continuation for Supervisor."""
    if not messages or not isinstance(messages[-1], HumanMessage):
        raise ValueError("final message is not a provider continuation")
    last = messages[-1]
    metadata = last.additional_kwargs
    if (last.content != RECORDED_CONTINUATION
            or metadata.get("_synthetic_nudge") != "provider_requires_user_turn"
            or metadata.get("_hide_from_stream") is not True
            or metadata.get("_operator_input") is True
            or metadata.get("_operator_steer") is True):
        raise ValueError("final message is not the exact runtime-tagged continuation")
    replacement = model_module._provider_continuation_text("Supervisor")
    if not isinstance(replacement, str) or not replacement.strip() or replacement == last.content:
        raise ValueError("production Supervisor continuation unavailable or unchanged")
    result = list(messages)
    result[-1] = last.model_copy(update={"content": replacement})
    return result


def supervisor_tools() -> list:
    """Capture current Supervised tools at the real builder boundary, without running a graph."""
    model = Model.__new__(Model)
    model.mode = "supervised"
    model.state = {"supervisor_messages": [SystemMessage(content="existing channel")]}
    with (
        patch.object(model_module, "create_agent", side_effect=lambda **kwargs: kwargs),
        patch.object(Model, "_get_base_chat_model", return_value=object()),
        patch.object(Model, "_context_middleware", return_value=[]),
        patch.object(Model, "_wrap_create_agent", side_effect=lambda agent, *args: agent),
        patch.object(Model, "_autonomous_execution_enabled_for_turn", return_value=False),
    ):
        captured = model._supervisor_agent()
    tools = captured["tools"]
    if "respond_to_user" not in [tool.name for tool in tools]:
        raise ValueError("production Supervisor completion tool unavailable")
    return tools


class OneRequestTransport(httpx.AsyncBaseTransport):
    def __init__(self, expected_url: str):
        self.expected_url = expected_url
        self.inner = httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(
                cafile=configure_sage_ssl_cert_file(certs_dir=SAGE / "certs") or None
            ), retries=0
        )
        self.count = 0
        self.wire_body_sha256: str | None = None
        self.wire_tool_schema_sha256: str | None = None

    async def handle_async_request(self, request):
        if request.method != "POST" or str(request.url) != self.expected_url:
            raise RuntimeError("unexpected provider endpoint")
        if self.count:
            raise RuntimeError("replay permits one provider request")
        payload = json.loads(request.content)
        token_cap = payload.get("max_completion_tokens", payload.get("max_tokens"))
        if token_cap != OUTPUT_CAP or ("max_tokens" in payload and "max_completion_tokens" in payload):
            raise RuntimeError("missing exact output cap")
        self.wire_body_sha256 = hashlib.sha256(request.content).hexdigest()
        self.wire_tool_schema_sha256 = digest(payload.get("tools", []))
        self.count += 1
        async with asyncio.timeout(TIMEOUT_SECONDS):
            return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


class NoSyncTransport(httpx.BaseTransport):
    def handle_request(self, request):
        raise RuntimeError("unexpected synchronous provider request")


async def run_once(messages: list, tools: list, profile: dict[str, str]) -> dict:
    endpoint = profile["API_ENDPOINT"].rstrip("/") + "/chat/completions"
    transport = OneRequestTransport(endpoint)
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, trust_env=False) as async_client:
        with httpx.Client(transport=NoSyncTransport(), timeout=TIMEOUT_SECONDS, trust_env=False) as sync_client:
            kwargs = dict(model_provider=profile["provider"], model=profile["model"],
                          api_key=profile["API_KEY"], base_url=profile["API_ENDPOINT"],
                          max_tokens=OUTPUT_CAP, max_retries=0, timeout=TIMEOUT_SECONDS,
                          http_client=sync_client, http_async_client=async_client)
            temperature = model_module._eval_temperature()
            if temperature is not None:
                kwargs["temperature"] = temperature
            llm = model_module._patch_model_for_bedrock(init_chat_model(**kwargs))
            bound = llm.bind_tools(tools)
            started = time.monotonic()
            try:
                async with asyncio.timeout(TIMEOUT_SECONDS):
                    reply = await bound.ainvoke(messages)
            except Exception as exc:
                return {"status": "error", "error_type": type(exc).__name__,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "provider_requests": transport.count,
                        "wire_body_sha256": transport.wire_body_sha256,
                        "wire_tool_schema_sha256": transport.wire_tool_schema_sha256,
                        "usage": None}
            if transport.count != 1 or not isinstance(reply, AIMessage):
                raise RuntimeError("unexpected replay result")
            return {"status": "completed", "elapsed_seconds": round(time.monotonic() - started, 3),
                    "provider_requests": transport.count,
                    "wire_body_sha256": transport.wire_body_sha256,
                    "wire_tool_schema_sha256": transport.wire_tool_schema_sha256,
                    "content": reply.content, "tool_calls": reply.tool_calls,
                    "invalid_tool_calls": reply.invalid_tool_calls,
                    "usage": reply.usage_metadata, "response_metadata": reply.response_metadata}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--prompt", type=Path, default=SAGE / "prompts" / "supervisor.md")
    parser.add_argument("--profile", type=Path, default=SAGE / ".env.local")
    parser.add_argument("--supervisor-continuation", action="store_true",
                        help="candidate only: use production Supervisor continuation text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true", help="send one paid model request")
    args = parser.parse_args()
    if args.supervisor_continuation and args.arm != "candidate":
        parser.error("--supervisor-continuation requires --arm candidate")
    messages, evidence = evidence_messages(args.evidence)
    if args.arm == "candidate":
        messages = candidate_messages(messages, args.prompt)
    if args.supervisor_continuation:
        messages = supervisor_continuation_messages(messages)
    tools = supervisor_tools()
    # The production middleware slims model-visible tool descriptions. These
    # eight Supervisor descriptions currently remain unchanged; fail if a later
    # source edit would make this replay diverge from the production wire schema.
    if any(tool.description != model_module._slim_tool_description(tool.description) for tool in tools):
        raise ValueError("Supervisor tool schema changed by production middleware")
    schemas = [convert_to_openai_tool(tool) for tool in tools]
    profile = read_profile(args.profile)
    identity = {"arm": args.arm, "supervisor_continuation": args.supervisor_continuation,
                "model_source_sha256": hashlib.sha256((SAGE / "ai/langgraph/model.py").read_bytes()).hexdigest(),
                "trace_id": evidence["trace_id"],
                "recorded_input_sha256": digest(json.loads(evidence["llm_calls"][-1]["input"]["value"])),
                "message_sha256": digest([m.model_dump(mode="json") for m in messages]),
                "tool_schema_sha256": digest(schemas), "tool_names": [t.name for t in tools],
                "model": profile["model"], "provider": profile["provider"],
                "endpoint_sha256": hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest(),
                "output_cap": OUTPUT_CAP, "timeout_seconds": TIMEOUT_SECONDS,
                "max_retries": 0, "temperature": model_module._eval_temperature(),
                "message_count": len(messages), "prompt_source_sha256": hashlib.sha256(args.prompt.read_bytes()).hexdigest()}
    if not args.run:
        print(json.dumps({"mode": "dry_run", **identity}, indent=2))
        return
    if args.output is None:
        parser.error("--run requires --output for complete response evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a private, unique artifact before a paid request. An existing or
    # unwritable path must fail without spending. Keep a started record if the
    # process is interrupted before the provider returns.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps({"mode": "provider_replay", **identity,
                                 "response": {"status": "started", "usage": None}},
                                indent=2, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
        try:
            outcome = asyncio.run(run_once(messages, tools, profile))
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
                      "output": str(args.output), "status": outcome["status"],
                      "usage": outcome["usage"],
                      "tool_names": [call.get("name") for call in outcome.get("tool_calls", [])]}, indent=2))


if __name__ == "__main__":
    main()
