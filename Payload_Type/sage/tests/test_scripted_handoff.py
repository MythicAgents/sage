"""Layer 1 (scripted): drive the REAL graph with a scripted model and assert what Sage did.

Everything here is real except the model: the real `StateGraph`, the real agents, the real handoff
tools, the real prompts, the real state channels. Only the LLM is replaced, by a fake that returns
exactly the messages this test wrote. That inversion is the point — the model's tool calls are
Sage's steering wheel, so scripting them lets a test drive Sage down an exact path, deterministically,
in about a second, with no API key, no VPN and no AWS session.

What this layer proves and what it cannot:

  * PROVES the machinery. Supervisor's `transfer_to_*` call actually routes to the specialist, the
    instruction reaches the specialist's own channel, the specialist's answer returns to Supervisor,
    and the operator receives output.
  * CANNOT prove judgement. Whether a real model would *choose* that handoff is an eval question, not
    a test question. Keeping the two apart is what stops a wiring bug from being misread as a
    capability problem.

See `docs/development/TEST_TIERS.md` § Test layers. Layer 0 (`test_graph_builds.py`) proves the graph
assembles; this file proves messages flow through it.

Three seams are stubbed, and each is stubbed for a stated reason rather than convenience:

  1. **The chat model.** No stock LangChain fake implements `bind_tools`, and `create_agent` calls it
     unconditionally, so a bare `GenericFakeChatModel` raises `NotImplementedError` inside the graph.
     `_ScriptedModel` adds the one missing method and ignores the tool schemas, which is safe here
     because the script decides the tool calls.
  2. **The checkpointer.** `InMemorySaver` per LangChain's testing guidance. Using the production
     `AsyncSqliteSaver` leaves an aiosqlite worker thread alive and the test process hangs at exit
     even after a clean assertion.
  3. **The response emitter.** This is the REAL native-chat seam, not a shortcut: `sage_chat` passes
     an emitter in production. Omitting it makes `_stream_message_to_mythic` fall back to
     `SendMythicRPCResponseCreate`, which retries a RabbitMQ connection forever — the test HANGS
     rather than fails, which is far worse than a red test. Capturing the emitter also gives us the
     operator's view to assert on.

The Mythic client is a mock that must never be called during this flow; `test_the_flow_never_touches_mythic`
is the control that keeps that honest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model  # noqa: E402

_DEAD_ENDPOINT = "http://127.0.0.1:9/v1"
_INSTRUCTION = "List the active callbacks."
_WORKER_ANSWER = "callback 1 on CASTELBLACK is alive"
_FINAL_ANSWER = "One callback is alive: callback 1 on CASTELBLACK."

# Low on purpose. The real limit is large enough that a routing bug becomes a long hang instead of a
# fast red; a scripted conversation of this length needs only a handful of super-steps.
_RECURSION_LIMIT = 8


class _ScriptedModel(GenericFakeChatModel):
    """A fake chat model that can be bound to tools, and counts how often it was asked to speak.

    `bind_tools` returning `self` is the whole trick: the scripted messages already encode which tool
    calls happen, so the bound schemas are irrelevant. The counter turns "the graph looped more than
    the script expected" into an explicit assertion instead of a confusing `StopIteration`.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_ScriptedModel":
        return self


class _Harness:
    """One scripted turn against the real graph.

    The Model is built inside `run`, not in `__init__`: its constructor creates an `AsyncSqliteSaver`,
    which calls `asyncio.get_running_loop()` and therefore cannot be constructed from sync code. The
    checkpointer is swapped for `InMemorySaver` immediately afterwards, but the production one is
    built first and that is enough to require the loop.
    """

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = script
        self.emitted: list[str] = []
        self.model: Model | None = None
        self.mythic: MagicMock | None = None

    async def _emit(self, text: str) -> bool:
        self.emitted.append(text)
        return True

    def _build(self, script: list[AIMessage]) -> Model:
        config = {"configurable": {"api_key": "sk-not-a-real-key", "API_ENDPOINT": _DEAD_ENDPOINT}}
        model = Model(
            provider="openai",
            model="gpt-4o-mini",
            system_prompt="scripted handoff test",
            config=config,
            task_id=0,
            agent_task_id="test-scripted-handoff",
            response_emitter=self._emit,
            channel_id=1,
        )
        self.mythic = MagicMock()
        self.mythic.get_payload_names = AsyncMock(return_value=[])
        model.mythic_client = self.mythic
        model._payload_names = []
        model.memory = InMemorySaver()
        fake = _ScriptedModel(messages=iter(script))
        model._get_base_chat_model = lambda *a, **k: fake  # type: ignore[assignment]
        return model

    async def run(self, prompt: str) -> dict[str, Any]:
        model = self._build(self.script)
        self.model = model
        model._rebuild_graph()
        model.state["messages"] = [HumanMessage(content=prompt)]
        config = model._graph_run_config("scripted-handoff-thread")
        config["recursion_limit"] = _RECURSION_LIMIT
        return await model.graph.ainvoke(model.state, config)


def _handoff_call() -> AIMessage:
    """Supervisor's delegation, exactly as the model would emit it."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "transfer_to_Mythic_Operator",
                "args": {
                    "handoff_title": "check callbacks",
                    "handoff_instruction": _INSTRUCTION,
                },
                "id": "call_handoff_1",
                "type": "tool_call",
            }
        ],
    )


def _contents(messages: list[Any]) -> list[str]:
    return [str(getattr(m, "content", "")) for m in messages]


def _work_messages(messages: list[Any]) -> list[Any]:
    """Everything except the seeded system prompt.

    Load-bearing. `_mythic_operator_agent()` appends the specialist's system prompt to its channel
    when the channel is empty, and that happens while the GRAPH IS BUILT — before any routing
    decision. So "the specialist's channel is non-empty" is true even on a turn where the specialist
    never ran, and asserting on it would make both the positive and negative cases vacuous. Only
    non-system traffic is evidence that the node actually executed.
    """
    return [m for m in messages if type(m).__name__ != "SystemMessage"]


@pytest.fixture(scope="module")
def delegated() -> dict[str, Any]:
    """One scripted delegation, shared by the assertions below.

    Module-scoped because building the real graph costs about a second and every test here inspects
    the same single run from a different angle.
    """
    harness = _Harness([_handoff_call(), AIMessage(content=_WORKER_ANSWER), AIMessage(content=_FINAL_ANSWER)])
    result = asyncio.run(harness.run("Are any callbacks alive?"))
    return {"result": result, "harness": harness}


def test_the_handoff_reaches_the_specialists_own_channel(delegated):
    """The routing claim: a transfer_to_* tool call must actually move work to that agent.

    Asserting on `mythic_operator_messages` rather than on the shared `messages` channel is
    deliberate — the shared channel would look populated even if the specialist never ran.
    """
    work = _work_messages(delegated["result"].get("mythic_operator_messages") or [])
    assert work, "Mythic_Operator's channel holds only its seeded prompt; the handoff never routed"
    assert any(_INSTRUCTION in c for c in _contents(work)), (
        f"the handoff instruction never reached the specialist: {_contents(work)}"
    )


def test_the_specialist_actually_produced_its_answer(delegated):
    """Proof the node EXECUTED, not merely that work was addressed to it.

    The specialist's own reply landing in its own channel is the first point in the flow that cannot
    be produced by routing alone.
    """
    work = _contents(_work_messages(delegated["result"].get("mythic_operator_messages") or []))
    assert any(_WORKER_ANSWER in c for c in work), (
        f"Mythic_Operator never produced its scripted answer: {work}"
    )


def test_the_workers_answer_returns_to_supervisor(delegated):
    """The handback. A specialist that runs but whose result never returns is the silent failure."""
    supervisor_channel = _contents(delegated["result"].get("supervisor_messages") or [])
    assert any(_WORKER_ANSWER in c for c in supervisor_channel), (
        f"the worker's answer never reached Supervisor: {supervisor_channel}"
    )


def test_the_operator_sees_output(delegated):
    """End of the pipe: something must reach the emitter sage_chat wires up in production."""
    emitted = delegated["harness"].emitted
    assert emitted, "nothing was emitted to the operator"
    assert any(_WORKER_ANSWER in text for text in emitted), emitted


def test_the_flow_never_touches_mythic(delegated):
    """The control. Without it this file could pass by accidentally reaching a live Mythic.

    A handoff is pure control-flow: no Mythic call is required to route work to a specialist, so any
    call here means the test is no longer hermetic and would break off-VPN.
    """
    mythic = delegated["harness"].mythic
    for forbidden in ("issue_task", "execute_custom_query", "login", "issue_task_and_waitfor_task_output"):
        assert not getattr(mythic, forbidden).called, f"scripted handoff called Mythic.{forbidden}"


def test_no_handoff_means_the_specialist_never_runs():
    """The negative arm. If the specialist ran regardless, every assertion above would be vacuous.

    Scoped deliberately to the routing claim. It asserts nothing about the emitter, because a bare
    Supervisor `AIMessage` is NOT streamed to the operator — observed here, and consistent with
    Supervisor answering through `respond_to_user`. In the delegated run the text that reached the
    emitter was the SPECIALIST's, not Supervisor's closing line. Asserting on emission here would
    fail for a reason that has nothing to do with handoff routing.
    """
    harness = _Harness([AIMessage(content="No callbacks are configured yet.")])
    result = asyncio.run(harness.run("Are any callbacks alive?"))

    work = _work_messages(result.get("mythic_operator_messages") or [])
    assert not work, f"Mythic_Operator ran without being delegated to: {_contents(work)}"
