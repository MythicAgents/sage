"""Regression tests for the supervised plan-and-execute gate's termination Command.

Two defects are covered, both observed together on a live Mythic chat channel:

1. The gate returned ``Command(graph=Command.PARENT)`` from ``_wrap_create_agent._ainvoke``,
   which is a *top-level* node body on the "Sage" graph. A top-level node has no parent, so
   langgraph raised ``ParentCommand``, rewrote its ``graph`` to the empty parent namespace,
   and the exception escaped ``astream`` with nothing to catch it.

2. Both operator-facing error surfaces then rendered ``str(exc)``. A single-arg exception
   stringifies to its payload's repr, so the operator received the entire
   ``Command(update={'messages': [AIMessage(content="...")]})`` — the whole final report,
   twice over, as the turn's error text.
"""

import ast
import inspect
import operator
import textwrap
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.errors import ParentCommand
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ai.langgraph import model as model_module
from ai.langgraph.operator_error import SUPPRESSED_SUFFIX, operator_error_text


# --------------------------------------------------------------------------------------
# The langgraph behaviour the production fix depends on.
# These two tests are each other's control: identical graphs, one kwarg apart.
# --------------------------------------------------------------------------------------


class _State(TypedDict):
    """Minimal stand-in for SageState: one accumulating message channel."""

    messages: Annotated[list[AnyMessage], operator.add]


def _one_node_graph(command: Command):
    """Compile a single-node top-level graph whose node returns `command`."""

    def node(state):
        return command

    return StateGraph(_State).add_node("only", node).add_edge(START, "only").compile()


def test_top_level_node_may_end_with_a_plain_command():
    """The corrected idiom: goto=END with no graph= applies the update and terminates."""

    update = {"messages": [AIMessage(content="final report", name="Supervisor")]}
    graph = _one_node_graph(Command(goto=END, update=update))

    result = graph.invoke({"messages": []})

    assert [m.content for m in result["messages"]] == ["final report"]


def test_top_level_node_returning_parent_command_escapes():
    """The control. Removing this behaviour would make the regression above vacuous."""

    update = {"messages": [AIMessage(content="final report", name="Supervisor")]}
    graph = _one_node_graph(Command(goto=END, update=update, graph=Command.PARENT))

    with pytest.raises(ParentCommand) as excinfo:
        graph.invoke({"messages": []})

    # This is precisely the leak: the escaped exception's text IS the Command repr,
    # carrying the full message content out to whatever renders str(exc).
    assert str(excinfo.value).startswith("Command(")
    assert "final report" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# The production node body must not use Command.PARENT.
# --------------------------------------------------------------------------------------


def _ainvoke_ast() -> ast.AsyncFunctionDef:
    """AST of the `_ainvoke` closure inside `_wrap_create_agent`.

    Parsed rather than grepped: a text search also matches prose, so the comment explaining
    why Command.PARENT is wrong here would itself trip the guard.
    """

    wrapper_src = textwrap.dedent(inspect.getsource(model_module.Model._wrap_create_agent))
    for node in ast.walk(ast.parse(wrapper_src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ainvoke":
            return node
    raise AssertionError("_ainvoke not found inside _wrap_create_agent")


def test_top_level_node_body_never_returns_a_parent_command():
    """`_ainvoke` is registered via add_node, so Command.PARENT can only ever escape.

    Command.PARENT stays correct inside the handoff/control *tools*, which run in the inner
    react subgraph whose parent is this graph. This test is scoped to the node body alone.
    """

    body = _ainvoke_ast()
    calls = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Command"
    ]

    # Floor assertions: a guard that inspected an empty or truncated body would report
    # success while examining nothing at all.
    assert len(calls) >= 3, f"expected the three Command returns, saw {len(calls)}"
    names = {n.id for n in ast.walk(body) if isinstance(n, ast.Name)}
    assert "_SUPERVISED_DELEGATION_CAP" in names, "did not capture the gate branch"

    offenders = [
        ast.unparse(kw.value)
        for call in calls
        for kw in call.keywords
        if kw.arg == "graph"
    ]
    assert offenders == [], f"Command(graph=...) returned from a top-level node: {offenders}"


def test_supervisor_node_is_top_level_so_it_has_no_parent_graph():
    """Pins the premise the test above rests on: Supervisor is added to the root graph."""

    build_src = inspect.getsource(model_module.Model)
    assert '.add_node("Supervisor", self._supervisor_agent())' in build_src
    assert "_wrap_create_agent(agent, \"supervisor_messages\", name)" in build_src


# --------------------------------------------------------------------------------------
# Operator-facing error text never renders a non-string exception payload.
# --------------------------------------------------------------------------------------


def test_escaped_control_flow_exception_is_not_rendered_to_the_operator():
    update = {"messages": [AIMessage(content="# Strategic Analysis", name="Supervisor")]}
    exc = ParentCommand(Command(goto=END, update=update))

    text = operator_error_text(exc)

    assert text == f"ParentCommand: {SUPPRESSED_SUFFIX}"
    assert "Command(" not in text
    assert "Strategic Analysis" not in text


def test_ordinary_string_error_passes_through_verbatim():
    assert operator_error_text(RuntimeError("bedrock throttled the request")) == (
        "bedrock throttled the request"
    )


def test_valid_near_match_string_error_is_not_suppressed():
    """A real error whose *text* mentions a Command must survive untouched.

    Suppressing on the string "Command(" instead of on the payload's type would silently
    destroy genuine diagnostics. The rule is about types, not about spelling.
    """

    message = "tool builder produced invalid Command(update=...) for callback 1"
    assert operator_error_text(ValueError(message)) == message


def test_exception_with_no_args_renders_empty_so_callers_use_their_fallback():
    assert operator_error_text(RuntimeError()) == ""


def test_both_operator_surfaces_use_the_safe_formatter():
    """Neither surface may regress to str(exc); each is a separate publication path."""

    repo_root = Path(__file__).resolve().parents[3]
    model_src = (repo_root / "Payload_Type/sage/ai/langgraph/model.py").read_text()
    service_src = (repo_root / "Payload_Type/sage/sage_chat/service.py").read_text()

    assert "error_msg = operator_error_text(e)" in model_src
    assert "error_msg = str(e)" not in model_src

    assert "error_text = operator_error_text(error)" in service_src
    assert "error_text = str(error)" not in service_src
