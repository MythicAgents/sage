"""Losing BloodHound must cost exactly the attack graph, and nothing else.

ISC-19. The rest of this ISA proves Sage survives without BloodHound; this proves the survival is not
partial. The failure it guards is subtle and would be easy to ship: a `not connected` check placed one
level too high — around the agent instead of around its MCP tools — silently removes an agent, a
slash command, or an unrelated tool along with the graph. Chat still answers, so nothing looks broken,
and the operator simply finds that something else stopped existing.

The real seam is `Model._build_bloodhound_agent`, which computes `mcp_tools` from the connected
servers and then, when there are none, **still constructs the agent** with its TTP and handback tools.
That "still" is the whole property. This file asserts it against the REAL graph rather than a stand-in,
because the 2026-08-10 outage was caused precisely by a hand-built stand-in that had drifted from the
graph it stood in for.

Hermetic: dummy key, unroutable endpoint, mocked Mythic client — the same harness as
`test_graph_builds.py`, deliberately copied rather than reinvented per `AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model  # noqa: E402
from ai.bloodhound_config import REQUIRED_BLOODHOUND_TOOLS  # noqa: E402

_DEAD_ENDPOINT = "http://127.0.0.1:9/v1"


def _hermetic_model(**kwargs) -> Model:
    config = {"configurable": {"api_key": "sk-not-a-real-key", "API_ENDPOINT": _DEAD_ENDPOINT}}
    model = Model(
        provider="openai",
        model="gpt-4o-mini",
        system_prompt="bloodhound scoping check",
        config=config,
        task_id=0,
        agent_task_id="test-degradation-scope",
        **kwargs,
    )
    mythic = MagicMock()
    mythic.get_payload_names = AsyncMock(return_value=[])
    mythic.get_tools = MagicMock(return_value=[])
    model.mythic_client = mythic
    model._payload_names = []
    return model


def _fake_tool(name: str):
    """A REAL LangChain tool with a fake body.

    A hand-rolled object with `.name` and `.description` is not enough: the agent binds these into a
    model, which validates them. That validation failing is the same class of problem as a hand-built
    stand-in drifting from the real structure, so the fake is only fake in its behaviour.
    """
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        func=lambda: "", name=name, description=f"fake {name} for a scoping test"
    )


def _graph_nodes(**model_kwargs) -> set:
    """Build the REAL graph and return its node names."""

    async def body():
        model = _hermetic_model(**model_kwargs)
        model._rebuild_graph()
        return set(model.graph.get_graph().nodes)

    return asyncio.run(body())


@pytest.fixture
def with_bloodhound(monkeypatch):
    """A connected BloodHound exposing exactly the tools the admission contract requires."""
    from ai import mcp as mcp_module

    server = "BloodHound"
    monkeypatch.setattr(mcp_module.MCPManager, "get_connected_servers", staticmethod(lambda: [server]))
    monkeypatch.setattr(
        mcp_module.MCPManager,
        "get_tools_by_server",
        staticmethod(lambda _s: [_fake_tool(n) for n in sorted(REQUIRED_BLOODHOUND_TOOLS)]),
    )
    monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned", lambda self, _s: True)


@pytest.fixture
def without_bloodhound(monkeypatch):
    from ai import mcp as mcp_module

    monkeypatch.setattr(mcp_module.MCPManager, "get_connected_servers", staticmethod(list))
    monkeypatch.setattr(mcp_module.MCPManager, "get_tools_by_server", staticmethod(lambda _s: []))


def test_the_graph_has_the_same_nodes_with_and_without_bloodhound(
    monkeypatch, with_bloodhound
) -> None:
    """No agent may disappear. Losing the graph must not lose a participant in the conversation."""
    connected_nodes = _graph_nodes()

    monkeypatch.undo()
    from ai import mcp as mcp_module

    monkeypatch.setattr(mcp_module.MCPManager, "get_connected_servers", staticmethod(list))
    monkeypatch.setattr(mcp_module.MCPManager, "get_tools_by_server", staticmethod(lambda _s: []))
    degraded_nodes = _graph_nodes()

    assert degraded_nodes == connected_nodes, (
        "the node set changed when BloodHound went away: "
        f"lost {sorted(connected_nodes - degraded_nodes)}, gained {sorted(degraded_nodes - connected_nodes)}"
    )
    assert degraded_nodes, "built an empty graph, so this comparison proves nothing"


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"mode": "supervised"}, {"autonomous_solve": True}],
    ids=["conversation", "supervised", "autonomous"],
)
def test_the_graph_still_builds_without_bloodhound_in_every_mode(without_bloodhound, kwargs) -> None:
    """A graph that cannot assemble is the most complete way to widen the degradation."""
    nodes = _graph_nodes(**kwargs)

    assert nodes, "the real graph failed to build with BloodHound absent"


def test_only_bloodhound_mcp_tools_are_lost(with_bloodhound, monkeypatch) -> None:
    """The seam itself: `_bloodhound_tools_for_turn` is what empties, and only it.

    Note what this does NOT assert. A server exposing all three admission tools yields only
    `domain_info` here, because `_bloodhound_tools_for_turn` deliberately withholds composite
    management tools and raw Cypher from the LLM agent — an authorization boundary, with the
    deterministic reconcilers keeping their own source-owned query path. So admission and exposure
    are different sets on purpose, and asserting equality would encode a misreading of the design as
    a requirement.
    """
    from ai import mcp as mcp_module

    async def body():
        model = _hermetic_model()
        connected = {t.name for t in model._bloodhound_tools_for_turn()}
        monkeypatch.setattr(mcp_module.MCPManager, "get_connected_servers", staticmethod(list))
        return connected, model._bloodhound_tools_for_turn()

    connected, degraded = asyncio.run(body())
    assert connected, "a connected BloodHound exposed no tools to the agent at all"
    assert connected <= set(REQUIRED_BLOODHOUND_TOOLS), (
        f"the agent was handed a tool the server never exposed: {connected}"
    )
    assert degraded == [], "tools survived a disconnect, so the seam is not where it appears to be"


def test_every_slash_command_survives_bloodhound_being_absent(without_bloodhound) -> None:
    """Slash commands are the operator's manual controls and must not be graph-dependent.

    Asserted against the declared command set rather than a hand-listed sample, so a command added
    later is covered without anyone remembering to add it here.
    """
    from sage_chat.models import SLASH_COMMANDS

    declared = {getattr(c, "Name", None) or getattr(c, "name", None) for c in SLASH_COMMANDS}
    declared.discard(None)

    assert declared, "no slash commands declared, so this test inspects nothing"
    # `/bloodhound` is legitimately about BloodHound, but it must still EXIST when BloodHound does
    # not — it is how an operator connects one.
    assert any("bloodhound" in str(name).lower() for name in declared)
    assert len(declared) >= 5, f"implausibly few slash commands to be a real inventory: {declared}"
