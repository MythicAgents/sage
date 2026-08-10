"""Sage's per-node error handler (ISA 9B, ISC-35/ISC-36).

Before this handler existed, any exception out of a specialist unwound `graph.astream` and ended the
run. `Model._handle_node_failure` turns a recoverable failure into a Supervisor handback instead.

The safety property is the one worth testing hardest: Sage's operator kill-switch is raised as a
plain `Exception` (`_OperatorStopRequested`), so LangGraph's runner WILL route it to an error
handler. A handler that absorbed it would make `exit` silently resume the run. These tests assert
that control exceptions propagate unchanged while ordinary failures are handled.

Wired against a real `StateGraph` rather than by calling the handler directly, so the registration in
`_rebuild_graph` is exercised, not just the function body. Mirrors the repo's no-pytest-asyncio
convention: the graph here is synchronous on purpose.
"""

from __future__ import annotations

import operator
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.errors import GraphRecursionError, NodeCancelledError
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import (  # noqa: E402
    _NODE_FAILURE_CONTROL_EXCEPTIONS,
    _OperatorStopRequested,
    Model,
)


class _State(TypedDict):
    """The subset of SageState the handler writes to."""

    messages: Annotated[list[AnyMessage], add_messages]
    supervisor_messages: Annotated[list[AnyMessage], operator.add]
    _message_seq: int


class _SeqStub:
    """Minimal stand-in for Model: the handler only needs a sequence counter."""

    def __init__(self) -> None:
        self.seq = 0

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq


def _build_graph(failure: BaseException | None, calls: list[str]):
    """A Supervisor + one failing specialist, wired exactly as _rebuild_graph wires them."""
    stub = _SeqStub()
    handler = Model._handle_node_failure.__get__(stub, Model)

    def specialist(state: _State) -> dict[str, Any]:
        calls.append("specialist")
        if failure is not None:
            raise failure
        return {"messages": [AIMessage(content="done")]}

    def supervisor(state: _State) -> dict[str, Any]:
        calls.append("supervisor")
        return {}

    return (
        StateGraph(_State)
        .add_node("Supervisor", supervisor)
        .add_node("Mythic_Operator", specialist, error_handler=handler)
        .add_edge(START, "Mythic_Operator")
        .add_edge("Mythic_Operator", "Supervisor")
        .compile()
    )


def _empty_state() -> _State:
    return {"messages": [], "supervisor_messages": [], "_message_seq": 0}


def test_ordinary_failure_hands_back_to_supervisor():
    """A specialist blowing up must reach Supervisor with a description, not end the run."""
    calls: list[str] = []
    graph = _build_graph(RuntimeError("mythic rpc exploded"), calls)

    result = graph.invoke(_empty_state())

    assert "supervisor" in calls, "Supervisor never ran; the failure ended the run"
    handback = result["supervisor_messages"]
    assert len(handback) == 1, f"expected exactly one handback message, got {handback}"
    content = handback[0].content
    assert "Mythic_Operator" in content, content
    assert "RuntimeError" in content and "mythic rpc exploded" in content, content


def test_handler_does_not_retry_the_failed_node():
    """9B reports a failure; retrying infrastructure errors is 9D and must not leak in here."""
    calls: list[str] = []
    graph = _build_graph(RuntimeError("boom"), calls)

    graph.invoke(_empty_state())

    assert calls.count("specialist") == 1, f"specialist ran {calls.count('specialist')} times"


def test_healthy_node_is_untouched():
    """The control: with no failure the handler must not fire at all."""
    calls: list[str] = []
    graph = _build_graph(None, calls)

    result = graph.invoke(_empty_state())

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
    graph = _build_graph(exc, calls)

    with pytest.raises(type(exc)):
        graph.invoke(_empty_state())

    assert "supervisor" not in calls, (
        f"{type(exc).__name__} was absorbed and the run continued to Supervisor"
    )


def test_operator_stop_reason_survives_the_handler():
    """The stop must arrive at invoke() carrying WHY it fired, not stripped to a bare exception."""
    calls: list[str] = []
    graph = _build_graph(_OperatorStopRequested("step_limit", "detail here"), calls)

    with pytest.raises(_OperatorStopRequested) as caught:
        graph.invoke(_empty_state())

    assert caught.value.stop_reason == "step_limit"
    assert caught.value.stop_detail == "detail here"


def test_control_exception_tuple_covers_the_kill_switch():
    """Guards the constant itself: dropping _OperatorStopRequested from it is the dangerous edit."""
    assert _OperatorStopRequested in _NODE_FAILURE_CONTROL_EXCEPTIONS
    assert GraphRecursionError in _NODE_FAILURE_CONTROL_EXCEPTIONS
    assert NodeCancelledError in _NODE_FAILURE_CONTROL_EXCEPTIONS
