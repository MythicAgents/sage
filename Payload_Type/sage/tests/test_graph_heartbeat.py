"""Graph heartbeat during deliberate Mythic waits (ISA 9A follow-up).

`TimeoutPolicy.idle_timeout` was answering two questions with one number. "Has this node stalled?"
wants a short window; "is this node allowed to still be waiting?" wants a window as long as the wait.
Callback sleep interval only decides the second. Deriving the idle timeout from sleep therefore got
stall detection wrong: a session touching one six-hour sleeper would need a six-hour blindness window
on every node, including ones that genuinely hang.

`_graph_heartbeat` separates them. A Mythic wait announces itself for as long as it lasts, so the idle
timeout can stay a flat, sleep-independent stall detector while the derived Mythic budget bounds how
long the wait may run.

The load-bearing test here is `test_heartbeat_keeps_a_long_wait_alive`, which drives a real
`StateGraph` with a real `TimeoutPolicy` and shows the same node surviving with the heartbeat and
being cancelled without it. Mirrors the repo's no-pytest-asyncio convention: each test owns its loop.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.errors import NodeTimeoutError
from langgraph.graph import START, StateGraph
from langgraph.types import TimeoutPolicy

SAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SAGE_ROOT))

from ai.langgraph.mythic_tools import (  # noqa: E402
    SAGE_TASK_HEARTBEAT_INTERVAL,
    _graph_heartbeat,
    _resolve_graph_heartbeat,
)

IDLE = 1.0        # node idle budget under test
WAIT = 2.0        # a "task" that legitimately outlasts it
BEAT = 0.25       # heartbeat cadence, comfortably inside IDLE


class _S(TypedDict, total=False):
    done: bool


def _graph(*, heartbeat: bool):
    """One node that waits longer than its own idle budget, with or without announcing itself."""

    async def waiter(state: _S) -> dict:
        if heartbeat:
            async with _graph_heartbeat(interval=BEAT):
                await asyncio.sleep(WAIT)
        else:
            await asyncio.sleep(WAIT)
        return {"done": True}

    return (
        StateGraph(_S)
        .set_node_defaults(timeout=TimeoutPolicy(idle_timeout=IDLE, refresh_on="auto"))
        .add_node("waiter", waiter)
        .add_edge(START, "waiter")
        .compile()
    )


def test_heartbeat_keeps_a_long_wait_alive():
    """The whole point: a deliberate wait outlives the idle budget only when it heartbeats."""
    assert asyncio.run(_graph(heartbeat=True).ainvoke({})) == {"done": True}


def test_without_the_heartbeat_the_same_wait_is_cancelled():
    """The control. If this ever stops raising, the test above proves nothing."""
    with pytest.raises(NodeTimeoutError):
        asyncio.run(_graph(heartbeat=False).ainvoke({}))


def test_resolve_returns_none_outside_a_graph():
    """Headless, eval, and direct-tool paths call these tools with no runnable context.

    `get_runtime()` raises there. Returning None is correct rather than degraded: those paths have no
    node timeout to refresh either.
    """
    assert _resolve_graph_heartbeat() is None


def test_context_manager_is_inert_outside_a_graph():
    """No runtime must mean no pump and no error, not a crash inside the tool."""

    async def body() -> str:
        async with _graph_heartbeat(interval=BEAT):
            await asyncio.sleep(0.05)
        return "ok"

    assert asyncio.run(body()) == "ok"


def test_pump_does_not_outlive_the_block():
    """A leaked pump would heartbeat a node that is no longer waiting, hiding real stalls."""

    async def body() -> int:
        before = len(asyncio.all_tasks())
        async with _graph_heartbeat(interval=BEAT):
            await asyncio.sleep(BEAT * 2)
        await asyncio.sleep(0)
        return len(asyncio.all_tasks()) - before

    assert asyncio.run(body()) == 0


def test_a_failing_heartbeat_does_not_break_the_body(monkeypatch):
    """Worst case for a broken heartbeat is the idle timeout firing, never a failed task."""

    def _explode() -> None:
        raise RuntimeError("heartbeat backend is down")

    monkeypatch.setattr(
        "ai.langgraph.mythic_tools._resolve_graph_heartbeat", lambda: _explode
    )

    async def body() -> str:
        async with _graph_heartbeat(interval=0.05):
            await asyncio.sleep(0.2)
        return "ok"

    assert asyncio.run(body()) == "ok"


def test_the_real_mythic_wait_is_wrapped():
    """The tests above prove the mechanism; this proves it is actually installed on the wait.

    Without this, deleting the `async with _graph_heartbeat()` at the call site would leave every
    heartbeat test green while long waits died in production.
    """
    import ast

    source = (SAGE_ROOT / "ai" / "langgraph" / "mythic_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    wrapped = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        if "_graph_heartbeat" not in ast.dump(node.items[0]):
            continue
        if "_issue_and_wait" in ast.dump(node):
            wrapped = True
            break

    assert wrapped, (
        "the Mythic task wait is no longer inside `async with _graph_heartbeat()`; a long wait will "
        "be cancelled by the node idle timeout regardless of its Mythic budget"
    )


def test_heartbeat_interval_leaves_room_inside_the_idle_budget():
    """Guards the shipped relationship: two beats must fit inside the node's idle window."""
    from ai.langgraph import model as model_module

    assert model_module.SAGE_NODE_IDLE_TIMEOUT > SAGE_TASK_HEARTBEAT_INTERVAL * 2
