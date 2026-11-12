"""Bounded provider check for a supervised step's one final call after its bound. Default: offline readiness.

Three assembled supervised delegations each reach one step bound (tool calls, active time, consecutive errors)
through an inert probe read that returns synthetic evidence. Every model call is scripted except the worker call
that follows the bound. Offline, a local stand-in answers that call behind the same bounded transport, so the wire
request is still recorded and nothing leaves the process. With --run, that call alone goes to the bound provider
route. Sage's own code decides the bound, admits the final call and builds its menu; this script sets only the
environment override, the clock and the probe's results. It proves provider integration of the final call, not
Mythic or range behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ConfigDict

ROOT = Path(__file__).resolve().parents[3]
SAGE = ROOT / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE))
from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph import step_bounds  # noqa: E402
from ai.langgraph.model import Model  # noqa: E402
from sage_chat.headless import build_chat_request  # noqa: E402
from sage_chat.service import bind_channel_thread_id  # noqa: E402
from sage_chat.session import drop_channel_session  # noqa: E402


def _load_sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REAL = _load_sibling("run_real_model_human_led_case")
CASE = REAL.CASE

# The route and resources the plan binds for one set; --run refuses any other route.
PLAN_MODEL = "bedrock-claude-4-6-sonnet"
PLAN_ENDPOINT_SHA256 = "018d811b0b67e8f8db0c0aa17adce2ffe8fc2a543527bc5802f44be577446b9d"
MAX_REQUESTS = 3
MAX_TOKENS = 150_000
OUTPUT_TOKENS = 1024
WALL_SECONDS = 180
REQUEST_SECONDS = 30
FIXTURE = "supervised-step-budget-v1"

PROMPT = "Review the file shares on LAB-FS01 with the share probe and report what they hold. Do not run tasks."
BRIEF = {"handoff_title": "Review LAB-FS01 shares", "allowed_capabilities": ["control_plane"],
         "expected_result": "What the LAB-FS01 shares hold."}
EVIDENCE = (
    "Share listing on LAB-FS01: ADMIN$, C$, IPC$, Finance (readable by LAB\\analyst).",
    "Finance share: 14 files; the newest, q3-forecast.xlsx, was modified 2026-10-02.",
    "LAB-FS01 local administrators: LAB\\Domain Admins, LAB\\svc_backup.",
)
PROBE = "probe_share_read"
SCENARIOS = (
    {"id": "tool_calls", "channel": 11, "bound": "tool_calls", "env": {"SAGE_STEP_MAX_TOOL_CALLS": "3"}, "reads": 3,
     "advance": 0.0, "fail": False},
    {"id": "active_time", "channel": 12, "bound": "active_time", "env": {"SAGE_STEP_MAX_ACTIVE_SECONDS": "60"}, "reads": 1,
     "advance": 90.0, "fail": False},
    {"id": "consecutive_errors", "channel": 13, "bound": "consecutive_errors", "env": {}, "reads": 3, "advance": 0.0,
     "fail": True},
)
OFFLINE_ANSWER = ("Offline stand-in answer: LAB-FS01 exposes a readable Finance share; the step reached its bound "
                  "before the share contents were reviewed.")
NOTICE_PREFIX = "[Sage] Mythic_Operator reached its step bound"
PROVIDER = "<provider>"  # the one script entry a provider may answer


class HybridModel(BaseChatModel):
    """Scripted for every call but the one script entry marked PROVIDER, which the provider answers with exactly the
    messages and tool menu Sage's graph supplied."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    script: list
    provider: Any
    calls: list
    bound_tools: tuple = ()
    bind_kwargs: dict = {}

    @property
    def _llm_type(self) -> str:
        return "sage-step-budget-hybrid"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"bound_tools": tuple(tools), "bind_kwargs": kwargs})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("synchronous model call")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        entry = self.script.pop(0) if self.script else None
        menu = [model_module._offered_tool_name(item) or getattr(item, "name", "") for item in self.bound_tools]
        self.calls.append({"entry": "provider" if entry == PROVIDER else "scripted" if entry else "exhausted",
                           "menu": menu})
        if entry is None:
            raise RuntimeError("script exhausted: an unplanned model call")
        if entry != PROVIDER:
            return ChatResult(generations=[ChatGeneration(message=entry)])
        target = self.provider.bind_tools(self.bound_tools, **self.bind_kwargs) if self.bound_tools else self.provider
        return ChatResult(generations=[ChatGeneration(message=await target.ainvoke(messages, stop=stop))])


class OfflineProvider(httpx.AsyncBaseTransport):
    """Answers the provider endpoint locally with a fixed completion; no socket is opened."""

    def __init__(self, model: str):
        self.model = model

    async def handle_async_request(self, request):
        body = {"id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": self.model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": OFFLINE_ANSWER}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        return httpx.Response(200, json=body, request=request)

    async def aclose(self):
        return None


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def _script(scenario: dict) -> list:
    reads = [_call(PROBE, {"step": step}, f"probe_{step}") for step in range(scenario["reads"])]
    return [_call("transfer_to_Mythic_Operator", {"handoff_instruction": PROMPT, **BRIEF}, "handoff_1"), *reads,
            PROVIDER, AIMessage(content="Reported the share review to the operator.")]


def _text(content: Any) -> str:
    return model_module._message_content_as_text(content)


def _forwarded(messages: list) -> list[str]:
    return [_text(messages[index + 1].content) for index, message in enumerate(messages[:-1])
            if message.additional_kwargs.get("_is_completion_header")]


def _sink() -> MagicMock:
    sink = MagicMock()
    sink.get_payload_names = AsyncMock(return_value=[])
    sink.get_tools.side_effect = lambda names: []  # no Mythic tool exists in this check
    sink._recon_snapshot_cache = {}
    sink._engagement_hops, sink._engagement_graph_facts = [], []
    sink._ledger_view = lambda: type("View", (), {"hops": [], "graph_facts": [], "corrected_hops": [],
                                                  "snapshot": None})()
    return sink


def verdict(scenario: dict, outcome: dict | None, forwarded: list[str], probe_calls: list[int], calls: list[dict],
            wire: list[dict], provider_text: str | None, effects: list[str]) -> dict:
    """The pass rule, written before any run: each criterion over this scenario's records."""
    provider_calls = [call for call in calls if call["entry"] == "provider"]
    wrap_up = (outcome or {}).get("wrap_up", "")
    expected = {field: int(value) if field in ("tool_calls", "consecutive_errors") else float(value)
                for field, value in ((name, scenario["env"][variable])
                                     for name, variable in step_bounds.BOUND_ENV.items()
                                     if variable in scenario["env"])}
    checks = {
        "boundary_exercised": bool(outcome) and outcome["status"] == "budget_exhausted"
                              and outcome["bound"] == scenario["bound"],
        "one_final_invocation": len(provider_calls) == 1 and len(wire) == 1
                                and not any(call["entry"] == "exhausted" for call in calls),
        "empty_wire_menu": len(wire) == 1 and not wire[0].get("request", {}).get("tools")
                           and provider_calls and provider_calls[0]["menu"] == [],
        "zero_tool_executions_after_bound": sorted(probe_calls) == list(range(scenario["reads"])) and not effects,
        "fields_accurate": bool(outcome) and outcome["tool_calls"] == scenario["reads"]
                           and all(outcome["bounds"][name] == value and outcome["bound_sources"][name] == "env"
                                   for name, value in expected.items())
                           and wrap_up in {"answered", "empty", "failed", "timeout"},
        "forwarded": len(forwarded) == 1 and (
            (wrap_up == "answered" and provider_text is not None and forwarded[0] == provider_text)
            or (wrap_up in {"empty", "failed", "timeout"} and forwarded[0].startswith(NOTICE_PREFIX))),
    }
    return {"pass": all(checks.values()), "checks": checks,
            "observations": {"wrap_up": wrap_up, "useful_answer": wrap_up == "answered",
                             "fallback": wrap_up in {"empty", "failed", "timeout"},
                             "answer_chars": len(forwarded[0]) if forwarded else 0}}


async def run_scenario(scenario: dict, profile: dict, transport: Any, usage: Any) -> dict:
    """One supervised request through native chat whose worker step reaches `scenario`'s bound."""
    state = {"calls": [], "clock": 0.0}

    def probe_share_read(step: int) -> str:
        state["calls"].append(step)
        state["clock"] += scenario["advance"]
        if scenario["fail"]:
            raise ToolException(f"access denied reading \\\\LAB-FS01\\Finance (synthetic, read {step})")
        return EVIDENCE[step % len(EVIDENCE)]

    probe = StructuredTool.from_function(probe_share_read, name=PROBE, handle_tool_error=True,
                                         description="Inert share probe. Returns synthetic share evidence.")
    original_filter = model_module.filter_tools_by_frontmatter

    def with_probe(agent: str, tools):
        selected = original_filter(agent, tools)
        return [*selected, probe] if agent == "mythic_operator" else selected

    async_client = httpx.AsyncClient(transport=transport, timeout=REQUEST_SECONDS, trust_env=False)
    sync_client = httpx.Client(transport=REAL.BoundedSyncTransport(), timeout=REQUEST_SECONDS, trust_env=False)
    factory = model_module.init_chat_model

    def bounded_factory(*args, **kwargs):
        if kwargs.get("model_provider") != "openai" or kwargs.get("model") != profile["model"]:
            raise REAL.CaseError("unexpected_model_construction")
        kwargs.update(http_client=sync_client, http_async_client=async_client, max_retries=0,
                      timeout=REQUEST_SECONDS, max_tokens=OUTPUT_TOKENS)
        llm = factory(*args, **kwargs)
        llm.callbacks = [usage]
        return llm

    overrides = {**scenario["env"], "SAGE_EVAL_MAX_OUTPUT_TOKENS": str(OUTPUT_TOKENS)}
    sent_before = len(transport.requests)
    with (patch.object(model_module, "init_chat_model", bounded_factory),
          patch.object(model_module, "filter_tools_by_frontmatter", with_probe),
          patch.dict(os.environ, overrides)):
        for variable in set(step_bounds.BOUND_ENV.values()) - set(scenario["env"]):
            os.environ.pop(variable, None)
        model = Model(provider=profile["provider"], model=profile["model"], system_prompt="",
                      config={"configurable": {"api_key": profile["API_KEY"], "base_url": profile["API_ENDPOINT"]}},
                      task_id=0, agent_task_id="step-budget-check", mode="supervised", autonomous_solve=False,
                      operation_id=1, channel_id=scenario["channel"], apitoken_id=0)
        provider = Model._get_base_chat_model(model)  # the route exactly as Sage builds it
        hybrid = HybridModel(script=_script(scenario), provider=provider, calls=[])
        calls = hybrid.calls  # the model's own list: its tool-bound copies share it
        model._get_base_chat_model = lambda *args, **kwargs: hybrid
        model.mythic_client = _sink()
        model._payload_names = []
        model.memory = InMemorySaver()
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._step_clock = lambda: state["clock"]
        model._rebuild_graph()
        chat = CASE.DriverChat(model)
        request = build_chat_request(PROMPT, channel_id=scenario["channel"], operation_id=1, request_id=1,
                                     config={"mode": "supervised"})
        bind_channel_thread_id(request, model)
        error = None
        try:
            await chat.chat(request)
        except Exception as exc:  # recorded; the verdict decides
            error = f"{type(exc).__name__}: {exc}"
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        await drop_channel_session(build_chat_request("", channel_id=scenario["channel"], operation_id=1))
    messages = list(snapshot.values.get("supervisor_messages", []))
    outcomes = [m.additional_kwargs["_step_outcome"] for m in messages if "_step_outcome" in m.additional_kwargs]
    wire = transport.requests[sent_before:]
    completed = [row for row in usage.calls if row.get("status") == "completed"]
    provider_text = _text(completed[-1]["output_messages"][0]["content"]) if wire and completed else None
    outcome = outcomes[0] if len(outcomes) == 1 else None
    effects = _effects(model.mythic_client)
    result = verdict(scenario, outcome, _forwarded(messages), state["calls"], calls, wire, provider_text, effects)
    return {"scenario": scenario["id"], "error": error, "outcomes": outcomes, "model_calls": calls,
            "forwarded": _forwarded(messages), "probe_calls": state["calls"], "wire": wire,
            "provider_text": provider_text, "mythic_effects": effects, **result}


# Mythic client methods that would change something on a callback or in Mythic; none may be reached.
_EFFECT_METHODS = ("issue_task", "execute_capability", "create_task", "upload", "add_credential", "create_payload")


def _effects(sink: MagicMock) -> list[str]:
    return [str(call) for call in sink.mock_calls if any(name in str(call).split("(")[0] for name in _EFFECT_METHODS)]


async def run_set(profile: dict, *, live: bool, tls_context: Any = None) -> dict:
    usage = REAL.UsageRecorder()
    transport = REAL.BoundedTransport(profile["API_ENDPOINT"], profile["model"], tls_context,
                                      max_requests=MAX_REQUESTS, output_tokens=OUTPUT_TOKENS, usage_recorder=usage,
                                      max_observed_tokens=MAX_TOKENS, request_seconds=REQUEST_SECONDS,
                                      require_complete_usage=live)
    if not live:
        transport.inner = OfflineProvider(profile["model"])
    results = []
    for scenario in SCENARIOS:
        if transport.rejections:
            break  # exhausted resources or a wrong destination stop the set
        results.append(await run_scenario(scenario, profile, transport, usage))
        if results[-1]["mythic_effects"] or results[-1]["error"]:
            break  # an effect or a failed run stops the set
    totals = [item.get("total_tokens") for row in usage.calls for item in row.get("usage", [])]
    return {"results": results, "transport_rejections": transport.rejections,
            "provider_requests": len(transport.requests),
            "observed_tokens": sum(value for value in totals if isinstance(value, int)),
            "pass": len(results) == len(SCENARIOS) and all(item["pass"] for item in results)
                    and not transport.rejections,
            "usefulness": {"answered": sum(item["observations"]["useful_answer"] for item in results),
                           "fallback": sum(item["observations"]["fallback"] for item in results)}}


def _route(profile: dict) -> dict:
    return {"provider": profile["provider"], "model": profile["model"],
            "endpoint_sha256": hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest()}


def _identity() -> dict:
    files = [SAGE / "ai/langgraph/model.py", SAGE / "ai/langgraph/step_bounds.py", Path(__file__)]
    files += sorted((SAGE / "prompts").glob("*.md"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def _quiet_run(coro, wall: float):
    """Run in a scratch working directory with its own channel-marker database, removed afterwards."""
    from sage_chat import session
    with tempfile.TemporaryDirectory(prefix="sage-step-budget-") as scratch, open(os.devnull, "w") as discard:
        previous = Path.cwd()
        os.chdir(scratch)
        try:
            with (contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard),
                  patch.object(session, "_CHANNEL_MARKER_DB_PATH", str(Path(scratch) / "sage.db"))):
                return asyncio.run(asyncio.wait_for(coro, timeout=wall))
        finally:
            os.chdir(previous)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="send each final call to the bound provider route")
    parser.add_argument("--config", type=Path, default=SAGE / ".env.local")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-model", default=PLAN_MODEL)
    parser.add_argument("--expected-endpoint-sha256", default=PLAN_ENDPOINT_SHA256)
    args = parser.parse_args(argv)
    try:
        profile = REAL.load_profile(args.config)
        route = _route(profile)
        if route["model"] != args.expected_model or route["endpoint_sha256"] != args.expected_endpoint_sha256:
            raise ValueError("exact route binding mismatch")
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        record_path = args.output_dir / ("set.json" if args.run else "readiness.json")
        if args.run and record_path.exists():
            raise ValueError("a set was already run in this directory; each set is one attempt")
    except Exception as exc:
        print(json.dumps({"status": "configuration_error", "reason": str(exc)}))
        return 2
    started = time.monotonic()
    record = {"fixture": FIXTURE, "mode": "run" if args.run else "offline_readiness", "route": route,
              "budgets": {"provider_requests": MAX_REQUESTS, "total_tokens": MAX_TOKENS,
                          "output_tokens_per_call": OUTPUT_TOKENS, "wall_seconds": WALL_SECONDS,
                          "transport_retries": 0},
              "source_sha256": _identity(), "started_utc": datetime.now(timezone.utc).isoformat()}
    if args.run:
        ips = REAL.resolved_endpoint_ips(profile["API_ENDPOINT"])
        guard = REAL.socket_allowlist(ips, profile["API_ENDPOINT"])
        tls_context = REAL.sage_tls_context()
    else:
        guard, tls_context = REAL.socket_allowlist(set(), profile["API_ENDPOINT"]), None  # no socket may open
    try:
        with guard:
            outcome = _quiet_run(run_set(profile, live=args.run, tls_context=tls_context), WALL_SECONDS)
        record.update(outcome)
        status = ("pass" if outcome["pass"] else "fail") if args.run else (
            "dry_run_ready" if outcome["pass"] else "not_ready")
    except Exception as exc:
        record.update(error=f"{type(exc).__name__}: {exc}")
        status = "error"
    record.update(status=status, elapsed_seconds=round(time.monotonic() - started, 3))
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")
    summary = {"status": status, "record": str(record_path), "route": route,
               "provider_requests": record.get("provider_requests"), "observed_tokens": record.get("observed_tokens"),
               "usefulness": record.get("usefulness"),
               "checks": {item["scenario"]: item["checks"] for item in record.get("results", [])},
               "error": record.get("error")}
    print(json.dumps(summary, sort_keys=True))
    return 0 if status in {"pass", "dry_run_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
