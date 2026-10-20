"""Sage's per-node error handler.

Without this handler, any exception out of a specialist unwinds `graph.astream` and ends the
run. `Model._handle_node_failure` turns a recoverable failure into a Supervisor handback instead.

The safety property is the one worth testing hardest: Sage's operator kill-switch is raised as a
plain `Exception` (`_OperatorStopRequested`), so LangGraph's runner WILL route it to an error
handler. A handler that absorbed it would make `exit` silently resume the run. These tests assert
that control exceptions propagate unchanged while ordinary failures are handled.

Wired against a real `StateGraph` rather than by calling the handler directly, so the registration in
`_rebuild_graph` is exercised, not just the function body.

The graph below mirrors the real graph's node defaults, including
`set_node_defaults(timeout=TimeoutPolicy(...))`, and invokes asynchronously via `asyncio.run` (the
repo's no-pytest-asyncio convention). This is where two features collide: the real graph gives every
node a timeout, LangGraph registers the error handler as its own node, and a node with a timeout
must be async — so a sync handler fails every request at build time, and a stand-in graph without
the timeout default stays green through it. `tests/test_graph_builds.py` is the belt to this file's
braces: it builds the ACTUAL graph.
"""

from __future__ import annotations

import asyncio
import operator
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.errors import GraphRecursionError, NodeCancelledError
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import TimeoutPolicy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import (  # noqa: E402
    _NODE_FAILURE_CONTROL_EXCEPTIONS,
    _OperatorStopRequested,
    SAGE_NODE_IDLE_TIMEOUT,
    Model,
)


def _invoke(graph, state):
    """Run the graph the way the real one runs: async, because its nodes carry timeouts."""
    return asyncio.run(graph.ainvoke(state))


class _State(TypedDict):
    """The subset of SageState the handler writes to."""

    messages: Annotated[list[AnyMessage], add_messages]
    supervisor_messages: Annotated[list[AnyMessage], operator.add]
    _message_seq: int


class _SeqStub:
    """Minimal stand-in for Model: the handler needs a sequence counter and card closes."""

    def __init__(self) -> None:
        self.seq = 0
        self.delegation_closes: list[dict[str, Any]] = []

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    async def _close_delegation(self, agent_name: str, content: str = "",
                                status: str = "finished") -> None:
        self.delegation_closes.append(
            {"agent": agent_name, "content": content, "status": status})


def _build_graph(failure: BaseException | None, calls: list[str]) -> tuple[Any, _SeqStub]:
    """A Supervisor + one failing specialist, wired exactly as _rebuild_graph wires them.

    Returns the compiled graph and the stub the failure handler was bound to, so tests can
    assert on the delegation-card lifecycle the handler exercised.
    """
    stub = _SeqStub()
    handler = Model._handle_node_failure.__get__(stub, Model)

    async def specialist(state: _State) -> dict[str, Any]:
        calls.append("specialist")
        if failure is not None:
            raise failure
        return {"messages": [AIMessage(content="done")]}

    async def supervisor(state: _State) -> dict[str, Any]:
        calls.append("supervisor")
        return {}

    graph = (
        StateGraph(_State)
        # Mirrors _rebuild_graph. Without this the stand-in cannot see sync/async node defects.
        .set_node_defaults(
            timeout=TimeoutPolicy(idle_timeout=SAGE_NODE_IDLE_TIMEOUT, refresh_on="auto")
        )
        .add_node("Supervisor", supervisor)
        .add_node("Mythic_Operator", specialist, error_handler=handler)
        .add_edge(START, "Mythic_Operator")
        .add_edge("Mythic_Operator", "Supervisor")
        .compile()
    )
    return graph, stub


def _empty_state() -> _State:
    return {"messages": [], "supervisor_messages": [], "_message_seq": 0}


def test_ordinary_failure_hands_back_to_supervisor():
    """A specialist blowing up must reach Supervisor with a description, not end the run."""
    calls: list[str] = []
    graph, _stub = _build_graph(RuntimeError("mythic rpc exploded"), calls)

    result = _invoke(graph, _empty_state())

    assert "supervisor" in calls, "Supervisor never ran; the failure ended the run"
    handback = result["supervisor_messages"]
    assert len(handback) == 1, f"expected exactly one handback message, got {handback}"
    content = handback[0].content
    assert "Mythic_Operator" in content, content
    assert "RuntimeError" in content and "mythic rpc exploded" in content, content


def test_handler_does_not_retry_the_failed_node():
    """9B reports a failure; retrying infrastructure errors is 9D and must not leak in here."""
    calls: list[str] = []
    graph, _stub = _build_graph(RuntimeError("boom"), calls)

    _invoke(graph, _empty_state())

    assert calls.count("specialist") == 1, f"specialist ran {calls.count('specialist')} times"


def test_healthy_node_is_untouched():
    """The control: with no failure the handler must not fire at all."""
    calls: list[str] = []
    graph, _stub = _build_graph(None, calls)

    result = _invoke(graph, _empty_state())

    assert calls == ["specialist", "supervisor"], calls
    assert result["supervisor_messages"] == [], "handler fired on a successful node"


@pytest.mark.parametrize(
    "exc",
    [
        _OperatorStopRequested("operator_exit"),
        GraphRecursionError("recursion limit"),
        NodeCancelledError("node cancelled"),
    ],
    ids=["operator_stop", "recursion", "node_cancelled"],
)
def test_control_exceptions_are_never_absorbed(exc: BaseException):
    """The falsifier. Absorbing _OperatorStopRequested would disable the kill-switch."""
    calls: list[str] = []
    graph, _stub = _build_graph(exc, calls)

    with pytest.raises(type(exc)):
        _invoke(graph, _empty_state())

    assert "supervisor" not in calls, (
        f"{type(exc).__name__} was absorbed and the run continued to Supervisor"
    )


def test_operator_stop_reason_survives_the_handler():
    """The stop must arrive at invoke() carrying WHY it fired, not stripped to a bare exception."""
    calls: list[str] = []
    graph, _stub = _build_graph(_OperatorStopRequested("step_limit", "detail here"), calls)

    with pytest.raises(_OperatorStopRequested) as caught:
        _invoke(graph, _empty_state())

    assert caught.value.stop_reason == "step_limit"
    assert caught.value.stop_detail == "detail here"


def test_control_exception_tuple_covers_the_kill_switch():
    """Guards the constant itself: dropping _OperatorStopRequested from it is the dangerous edit."""
    assert _OperatorStopRequested in _NODE_FAILURE_CONTROL_EXCEPTIONS
    assert GraphRecursionError in _NODE_FAILURE_CONTROL_EXCEPTIONS
    assert NodeCancelledError in _NODE_FAILURE_CONTROL_EXCEPTIONS


def test_failed_node_delegation_card_closes_as_error():
    """A failed specialist's card must close as an error, not success-shaped.

    If the handler leaves the failed node's card open, the Supervisor's next invocation sweeps it
    closed with the default finished status (and its streamed text as content), so a failed
    delegation renders as a completion. The handler must close the card itself, as an error, with
    the exception summary — before handing back to Supervisor.
    """
    calls: list[str] = []
    graph, stub = _build_graph(RuntimeError("mythic rpc exploded"), calls)

    result = _invoke(graph, _empty_state())

    closes = [c for c in stub.delegation_closes if c["agent"] == "Mythic_Operator"]
    assert closes, f"the failed node's card was never closed: {stub.delegation_closes}"
    assert all(c["status"] == "error" for c in closes), closes
    assert all("RuntimeError" in c["content"] and "mythic rpc exploded" in c["content"]
               for c in closes), closes
    # The handback to Supervisor still carries the failure description.
    handback = result["supervisor_messages"]
    assert any("RuntimeError" in str(m.content) for m in handback)


def test_healthy_node_delegation_card_is_not_closed_by_the_handler():
    """The control: no failure, no card lifecycle interference from the handler."""
    calls: list[str] = []
    graph, stub = _build_graph(None, calls)

    _invoke(graph, _empty_state())

    assert stub.delegation_closes == [], stub.delegation_closes
