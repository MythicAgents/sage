"""A new supervised delegation's provider requests carry none of an earlier worker step's transcript.

The native chat service, graph, approval card, delegation filter, middleware and ChatOpenAI serializer are real; only
the provider's replies (an in-process HTTP transport) and Mythic's reads and task are scripted. Sockets are blocked.
Each assertion reads the JSON body the provider would receive. Markers and identities are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest
from langchain_core.messages import SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import model as model_module
from ai.langgraph.mythic_tools import MythicTools
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.test_hitl_steer_current_intent import _NativeDriverChat, _cards, _input_response
from tests.test_payload_handoff_case import case as wire  # OpenAI chat-completion replies, streamed or not

# --- synthetic fixture data ----------------------------------------------------------------------------------------
PROMPT = "Read callback 5's task history, then run whoami on it."
OLD, CURRENT = "OLDSTEP-7f3a", "CURSTEP-91c2"
OLD_FAILURE = json.dumps({"status": "error", "error": f"{OLD} callback 5 task history is unavailable"})
OLD_HANDBACK = f"{OLD} handback: the history read failed, so nothing was learned."
CURRENT_RESULT = json.dumps({"status": "success", "tasks": [], "note": f"{CURRENT} no earlier tasks"})
TASK_OUTPUT = f"{CURRENT} task output: lab\\operator"
READ = {"callback_display_id": 5}
WHOAMI = {"command": "whoami", "parameters": "", "callback_display_id": 5}
SCOPE = {"callback_display_id": 5, "targets": ["WORKSTATION-5"]}
STEP_ONE = {**SCOPE, "handoff_title": "Read task history", "handoff_instruction": "Read callback 5's task history.",
            "allowed_capabilities": ["control_plane"], "expected_result": "Callback 5's task history."}
STEP_TWO = {**SCOPE, "handoff_title": "Run whoami", "handoff_instruction": f"{CURRENT}-assignment: run whoami.",
            "allowed_tools": ["issue_task_and_waitfor_task_output"], "expected_result": "The callback user."}
TRANSFER = "transfer_to_Mythic_Operator"
SOURCE_TEST = "test_a_later_delegation_serializes_no_earlier_worker_transcript[accept-False]"
ENVELOPE = re.compile(r"^\s*<([a-z_]+)>.*</\1>\s*$", re.DOTALL)  # one Sage-authored block wrapped in its own tag
# Sensitivity only: a separate test process replaces the filter through this variable (see the last test).
FILTER_VARIANT = "SAGE_TEST_DELEGATION_FILTER"
VARIANTS = {"identity": lambda messages: list(messages),
            "drop_current": lambda messages: [m for m in messages if isinstance(m, SystemMessage)]}


def _operator_script(autonomous_solve: bool) -> list:
    def handback(text):  # Autonomous Solve's Operator ends a step only through an explicit handback
        return (("handback_to_supervisor", {"reason": "step ended", "summary": text, "outcome": "progress"})
                if autonomous_solve else (None, {"content": text}))
    return [("get_task_history_for_callback", READ), handback(OLD_HANDBACK), ("get_task_history_for_callback", READ),
            ("issue_task_and_waitfor_task_output", WHOAMI), handback(f"{CURRENT} step two done.")]


class _Provider(httpx.AsyncBaseTransport):
    """The provider endpoint: records each serialized request body and answers from the role's script."""

    def __init__(self, autonomous_solve: bool):
        self.scripts = {"Supervisor": [(TRANSFER, STEP_ONE), (TRANSFER, STEP_TWO)],
                        "Mythic_Operator": _operator_script(autonomous_solve)}
        self.calls: list[tuple[str, dict]] = []
        self.gaps: list[str] = []

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        offered = {tool["function"]["name"] for tool in body.get("tools", [])}
        role = ("Supervisor" if TRANSFER in offered else
                "Mythic_Operator" if "get_task_history_for_callback" in offered else "unscripted")
        self.calls.append((role, body))
        script = self.scripts.get(role)
        if script:
            tool, args = script.pop(0)
        else:
            if role != "Supervisor":
                self.gaps.append(f"{role} call {len(self.calls)}")
            tool, args = None, {"content": "Final answer."}
        return wire._seed_response(request, body["model"], len(self.calls), tool, args)


class _NoSyncProvider(httpx.BaseTransport):
    def handle_request(self, request):
        raise AssertionError("synchronous provider call")


def _sink(reads: list, effects: list) -> MagicMock:
    """Mythic: callback 5 observed, a task-history read scripted per call, and one inert task."""
    schemas = {tool.name: tool for tool in MythicTools(agent_task_id="isolation-schema").get_tools(
        ["get_task_history_for_callback", "issue_task_and_waitfor_task_output"])}

    async def dispatch(name, kwargs):
        if name == "get_task_history_for_callback":
            reads.append(kwargs)
            return OLD_FAILURE if len(reads) == 1 else CURRENT_RESULT
        effects.append(kwargs)
        return TASK_OUTPUT

    def tools(names):
        return [StructuredTool.from_function(coroutine=lambda __name=name, **kw: dispatch(__name, kw), name=name,
                                             description=schemas[name].description,
                                             args_schema=schemas[name].args_schema)
                for name in schemas if name in names]

    preparer = MythicTools(agent_task_id="isolation-prepare")
    preparer.client = object()
    preparer._authenticate_live_command = lambda command, callback: asyncio.sleep(0, result=(
        {"status": "available", "command": command} if (command, callback) == ("whoami", 5)
        else {"status": "missing", "payload_type": "apollo"}))
    preparer._fetch_command_schema = lambda command, callback, **kw: asyncio.sleep(
        0, result=[] if (command, callback) == ("whoami", 5) else None)
    sink = MagicMock()
    sink.get_tools.side_effect = tools
    sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action
    sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]}
    return sink


async def _session(decision: str, autonomous_solve: bool) -> dict:
    """One native supervised request through both steps to the second step's card, then the operator's answer."""
    provider, reads, effects = _Provider(autonomous_solve), [], []
    factory = model_module.init_chat_model

    def offline_factory(*args, **kwargs):
        kwargs.update(http_async_client=httpx.AsyncClient(transport=provider),
                      http_client=httpx.Client(transport=_NoSyncProvider()), max_retries=0)
        return factory(*args, **kwargs)

    with patch.object(model_module, "init_chat_model", offline_factory):
        model = model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="offline isolation",
            config={"configurable": {"api_key": "offline-only", "base_url": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="isolation", mode="supervised", autonomous_solve=autonomous_solve,
            operation_id=1, channel_id=1, apitoken_id=0)
        model.mythic_client = _sink(reads, effects)
        model._payload_names = []
        model.memory = InMemorySaver()
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = _NativeDriverChat(model)
        try:
            first = build_chat_request(PROMPT, channel_id=1, operation_id=1, request_id=1,
                                       config={"mode": "supervised"})
            bind_channel_thread_id(first, model)
            await chat.chat(first)
            cards, effects_at_card = _cards(chat), list(effects)
            if cards:
                resume = build_chat_request("", channel_id=1, operation_id=1, request_id=2,
                                            config={"mode": "supervised"})
                resume.InputResponse = _input_response(decision, cards[0])
                await chat.chat(resume)
            snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        finally:
            await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))
    return {"operator": [body["messages"] for role, body in provider.calls if role == "Mythic_Operator"],
            "gaps": provider.gaps, "cards": cards, "effects_at_card": effects_at_card, "effects": effects,
            "reads": reads, "audit": "\n".join(str(m.content) for m in snapshot.values["mythic_operator_messages"])}


def _text(message: dict) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content)


def _assert_isolated(run: dict, decision: str, *, earlier_steps: bool = True) -> None:
    """The second step's provider requests: no earlier-step transcript, and every current-step carrier present.

    `earlier_steps`: Sage records a step outcome when the worker's turn ends in its node; an explicit
    `handback_to_supervisor` (how an Autonomous Solve worker ends a step) returns straight to the Supervisor and
    records none, so that lane has no earlier-step facts to carry."""
    assert run["gaps"] == [], run["gaps"]
    first, second, *step_two = run["operator"]
    assert any(OLD in _text(m) for m in second if m["role"] == "tool")  # positive control: the marker is visible
    old_ids = {call["id"] for m in first + second for call in m.get("tool_calls") or []}
    for index, messages in enumerate(step_two):
        label = f"step two call {index + 1}"
        # Transcript is everything but Sage's tagged envelopes, read whole (content and tool-call arguments).
        transcript = [m for m in messages if not (m["role"] == "assistant" and ENVELOPE.match(_text(m)))]
        leaked = [json.dumps(m)[:160] for m in transcript if OLD in json.dumps(m)]
        leaked += sorted(old_ids & ({m.get("tool_call_id") for m in messages}
                                    | {call["id"] for m in messages for call in m.get("tool_calls") or []}))
        assert not leaked, f"old transcript reached the {label}: {leaked}"
        users = [m for m in messages if m["role"] == "user" and _text(m) == PROMPT]
        assert len(users) == 1, f"current operator input missing or repeated in the {label}: {len(users)}"
        envelopes = {ENVELOPE.match(_text(m)).group(1): _text(m) for m in messages
                     if m["role"] == "assistant" and ENVELOPE.match(_text(m))}
        assert f"{CURRENT}-assignment" in envelopes.get("untrusted_delegated_assignment", ""), \
            f"current assignment missing in the {label}"
        assert "issue_task_and_waitfor_task_output" in envelopes.get("step_scope", ""), \
            f"current scope missing in the {label}"
        if earlier_steps:
            assert "Mythic_Operator" in envelopes.get("earlier_steps", ""), \
                f"earlier step facts missing in the {label}"
        if index:
            assert any(CURRENT in _text(m) for m in messages if m["role"] == "tool"), \
                f"current step's own tool result missing in the {label}"
    assert len(step_two) == 3, f"expected three step-two provider calls, got {len(step_two)}"
    resumed = [_text(m) for m in step_two[-1] if m["role"] == "tool"]
    if decision == "accept":
        assert any(TASK_OUTPUT in text for text in resumed), "approved task output missing after resume"
    else:
        assert not any(TASK_OUTPUT in text for text in resumed)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    if os.environ.get(FILTER_VARIANT):
        monkeypatch.setattr(model_module, "_current_delegation_invocation", VARIANTS[os.environ[FILTER_VARIANT]])


@pytest.mark.parametrize("autonomous_solve", [False, True])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_a_later_delegation_serializes_no_earlier_worker_transcript(decision, autonomous_solve, monkeypatch):
    if autonomous_solve:
        monkeypatch.setenv("SAGE_CONTROLLER_HITL", "0")  # the Supervisor/worker graph, not the controller
    run = asyncio.run(_session(decision, autonomous_solve))

    _assert_isolated(run, decision, earlier_steps=not autonomous_solve)
    # One card for the second step's task: nothing before the answer, exactly that task after Accept, none after Reject.
    assert len(run["cards"]) == 1 and run["effects_at_card"] == [] and len(run["reads"]) == 2
    if decision == "accept":
        assert len(run["effects"]) == 1 and {k: run["effects"][0].get(k) for k in WHOAMI} == WHOAMI
    else:
        assert run["effects"] == []
    # The earlier step's transcript stays in the worker channel for audit.
    assert f"{OLD} callback 5 task history is unavailable" in run["audit"] and OLD_HANDBACK in run["audit"]


@pytest.mark.skipif(bool(os.environ.get(FILTER_VARIANT)), reason="runs only in the parent process")
@pytest.mark.parametrize("variant,reason", [
    ("identity", "old transcript reached the step two call 1"),
    ("drop_current", "current operator input missing or repeated in the step two call 1"),
])
def test_the_same_check_fails_in_a_separate_process_without_the_filter_or_current_step_data(variant, reason):
    """Red runs of the source test above, each in its own process with only the filter replaced. The source test
    passing in this process (the filter untouched on disk and in memory here) is the green run on either side."""
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"{Path(__file__).resolve()}::{SOURCE_TEST}"],
        cwd=root, env={**os.environ, FILTER_VARIANT: variant}, capture_output=True, text=True, timeout=300)
    assert result.returncode == 1, result.stdout[-2000:]
    assert reason in result.stdout, result.stdout[-2000:]
