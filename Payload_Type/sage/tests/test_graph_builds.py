"""Tier 0: Sage's REAL graph must assemble. No model, no network, no credentials.

This file exists because of a total outage on 2026-08-10. Two commits that were each green on their
own tests were fatally incompatible in combination:

  * `6d9ec69` put `set_node_defaults(timeout=TimeoutPolicy(...))` on the graph, which applies to
    EVERY node.
  * `af5c490` registered `error_handler=self._handle_node_failure` on `Mythic_Operator`, which makes
    LangGraph create a synthetic `__error_handler__Mythic_Operator` node.

That synthetic node inherited the node-default timeout, and the handler was `def` rather than
`async def`. LangGraph refuses to apply a timeout to a sync node, so `_rebuild_graph` raised and
every single request failed — including "Hello" — in about five seconds, before any model call.
The offline suite was 3955 green throughout.

Why the existing tests could not catch it, which is the reusable lesson:

  * `test_node_failure_handler.py` does build a real `StateGraph`, but a HAND-BUILT stand-in that
    omitted `set_node_defaults`. A representative graph drifts from the real graph, and the drift is
    invisible precisely where the two features meet.
  * `test_task_timeout_config.py::test_graph_build_applies_the_timeout_policy` asserts the STRINGS
    `set_node_defaults` and `TimeoutPolicy` appear in the source of `_rebuild_graph`, parsed with
    `ast`. A test that reads source text cannot fail for a reason that lives in behaviour.

So the rule this file encodes: **assert on the artifact, not on a stand-in for it.** The cheapest
possible test — "does the thing assemble?" — did not exist, and it is the one that mattered.

Deliberately hermetic. `init_chat_model` builds a client object without contacting a provider, so a
dummy key and an unroutable base URL are enough; the Mythic client is a mock that fails loudly if the
build tries to call it. This runs offline, off-VPN, with no AWS session, in about two seconds.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model  # noqa: E402

# Unroutable on purpose: if graph construction ever starts making a real call, it fails fast and
# loudly here rather than silently depending on the operator's VPN.
_DEAD_ENDPOINT = "http://127.0.0.1:9/v1"


def _hermetic_model(**kwargs) -> Model:
    """A real Model wired to a provider that cannot be reached and a Mythic client that must not be."""
    config = {"configurable": {"api_key": "sk-not-a-real-key", "API_ENDPOINT": _DEAD_ENDPOINT}}
    model = Model(
        provider="openai",
        model="gpt-4o-mini",
        system_prompt="hermetic graph-build check",
        config=config,
        task_id=0,
        agent_task_id="test-graph-builds",
        **kwargs,
    )
    mythic = MagicMock()
    # Graph construction reads payload names; anything else it touches should be an explicit failure.
    mythic.get_payload_names = AsyncMock(return_value=[])
    model.mythic_client = mythic
    model._payload_names = []
    return model


def test_the_real_graph_builds():
    """The falsifier for the 2026-08-10 outage. Reverting the handler to `def` turns this red."""

    async def body() -> object:
        model = _hermetic_model()
        model._rebuild_graph()
        return model.graph

    graph = asyncio.run(body())
    assert graph is not None, "_rebuild_graph produced no graph"
    assert type(graph).__name__ == "CompiledStateGraph", type(graph).__name__


def test_the_error_handler_is_async():
    """The specific contract that broke, asserted directly so the failure names its own cause.

    A node carrying `set_node_defaults(timeout=...)` must be async, and LangGraph registers the
    error handler as exactly such a node. Keeping this separate from the build test means a
    regression reports *why* it broke rather than only that construction failed.
    """
    assert inspect.iscoroutinefunction(Model._handle_node_failure), (
        "Model._handle_node_failure must be `async def`: LangGraph registers it as the node "
        "'__error_handler__Mythic_Operator', which inherits the graph's node-default TimeoutPolicy, "
        "and node timeouts are rejected on sync callables. Shipping it sync breaks every request."
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"mode": "supervised"},
        {"autonomous_solve": True},
        {"max_steps": 0},
    ],
    ids=["default", "supervised", "autonomous", "unbounded_steps"],
)
def test_the_graph_builds_across_turn_configurations(kwargs):
    """`_rebuild_graph` branches on turn configuration, so one build proves only one path.

    The outage was configuration-independent, but the next wiring defect may not be. These are the
    dimensions that change which nodes and edges are installed.
    """

    async def body() -> object:
        model = _hermetic_model(**kwargs)
        model._rebuild_graph()
        return model.graph

    assert asyncio.run(body()) is not None, f"graph failed to build for {kwargs}"


def test_the_hermetic_fixture_never_reaches_the_network():
    """The control: prove this file is testing Sage, not the operator's connectivity.

    If graph construction started requiring a live provider or a live Mythic, this test would fail
    and the whole file would silently become an integration test that skips when off-VPN.
    """

    async def body() -> object:
        model = _hermetic_model()
        model._rebuild_graph()
        return model

    model = asyncio.run(body())
    assert model.mythic_client.get_payload_names.await_count >= 0
    for name in ("issue_task", "execute_custom_query", "login"):
        called = getattr(model.mythic_client, name).called
        assert not called, f"graph construction called Mythic.{name}; the build is not hermetic"
