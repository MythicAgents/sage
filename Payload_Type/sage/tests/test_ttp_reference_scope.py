"""TTP references preserve content without presenting prerequisites as observations."""

import asyncio
import json
import socket
from unittest.mock import AsyncMock

import pytest

from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph import ttp_library
from ai.mcp import MCPManager


@pytest.mark.parametrize("connection", ["disconnected", "connected", "unknown"])
def test_guidance_distinguishes_reference_from_runtime_metadata(monkeypatch, connection):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("reference probe must not open network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    def servers():
        if connection == "unknown":
            raise RuntimeError("registry unavailable")
        return ["bloodhound"] if connection == "connected" else []

    monkeypatch.setattr(MCPManager, "get_connected_servers", servers)
    sink = MythicTools(agent_task_id="reference-scope-test")
    monkeypatch.setattr(sink, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    tool = next(t for t in sink.get_tools(["get_ttp_guidance"])
                if t.name == "get_ttp_guidance")
    result = json.loads(asyncio.run(tool.ainvoke({
        "goal": "run SharpHound to collect AD data for BloodHound ingestion",
        "callback_display_id": 7,
    })))
    frontmatter, body = ttp_library.load_ttp(result["slug"])
    assert result["frontmatter"] == frontmatter
    assert result["guidance"] == ttp_library.guidance_body(body)
    assert result["payload_type"] == "apollo"
    scope = result["reference_scope"]
    assert set(scope["reference_fields"]) == {"frontmatter", "guidance", "execution_on_agent"}
    assert scope["prerequisites_evaluated"] is False
    assert set(scope["metadata_fields"]) == {"payload_type", "recommendation.connected"}
    assert "not observed target state" in tool.description
    if connection == "connected":
        assert "recommendation" not in result
    else:
        assert result["recommendation"]["connected"] is (False if connection == "disconnected" else None)


# --- a lookup whose best references tie is an honest ambiguity, not an arbitrary technique ----------------------
#
# Cairn #180 (Phoenix span 46): the worker's generic collection goal tied ROADtools, RustHound, cross-forest
# SharpHound and SharpHound; alphabetical iteration presented ROADtools with a missing-agent-file fallback.

HISTORICAL_GOAL = "collect BloodHound domain data for north.sevenkingdoms.local"
HISTORICAL_TIES = ["roadtools", "rusthound", "sharphound", "sharphound-cross-forest"]


def _guidance_tool(monkeypatch):
    monkeypatch.setattr(MCPManager, "get_connected_servers", lambda: [])
    sink = MythicTools(agent_task_id="reference-tie-test")
    monkeypatch.setattr(sink, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    return next(t for t in sink.get_tools(["get_ttp_guidance"]) if t.name == "get_ttp_guidance")


def _ask(tool, goal: str) -> dict:
    return json.loads(asyncio.run(tool.ainvoke({"goal": goal, "callback_display_id": 7})))


def _assert_selects_nothing(result: dict) -> None:
    for key in ("slug", "frontmatter", "guidance", "execution_on_agent", "sage_capability", "alternatives",
                "payload_type", "recommendation"):
        assert key not in result, key
    text = json.dumps(result)
    assert "mythic_agents" not in text and "get_all_commands_for_payloadtype" not in text


def test_the_historical_generic_collection_goal_is_ambiguous_and_selects_nothing(monkeypatch):
    result = _ask(_guidance_tool(monkeypatch), HISTORICAL_GOAL)

    assert result["status"] == "ambiguous"
    # SharpHound, the fourth tie, is not lost to the three-result display limit.
    assert [c["slug"] for c in result["candidates"]] == HISTORICAL_TIES
    assert result["candidate_count"] == 4 and result["truncated"] is False
    assert len({c["score"] for c in result["candidates"]}) == 1
    assert {c["name"] for c in result["candidates"]} >= {"SharpHound"}
    _assert_selects_nothing(result)


def test_reversing_candidate_iteration_order_does_not_invent_a_winner(monkeypatch):
    tool = _guidance_tool(monkeypatch)
    original = ttp_library.iter_ttps
    monkeypatch.setattr(ttp_library, "iter_ttps", lambda: iter(reversed(list(original()))))
    result = _ask(tool, HISTORICAL_GOAL)
    assert result["status"] == "ambiguous"
    assert [c["slug"] for c in result["candidates"]] == HISTORICAL_TIES


@pytest.mark.parametrize("goal, slug, procedure", [
    ("run SharpHound to collect BloodHound data for a domain", "sharphound", "collect-graph"),
    ("run ROADtools", "roadtools", ""),
    ("dump LSASS memory with nanodump", "nanodump", ""),
])
def test_an_explicitly_named_technique_keeps_its_unique_reference(monkeypatch, goal, slug, procedure):
    result = _ask(_guidance_tool(monkeypatch), goal)
    assert "status" not in result and result["slug"] == slug
    assert result["frontmatter"] == ttp_library.load_ttp(slug)[0]
    if procedure:
        assert result["sage_capability"] == {"name": procedure, "executor": "execute_capability"}
        assert f"hand back naming `{procedure}`" in result["execution_on_agent"]
    else:
        assert "sage_capability" not in result
        assert "get_all_commands_for_payloadtype" in result["execution_on_agent"]


def test_a_goal_matching_nothing_still_lists_the_catalog(monkeypatch):
    result = _ask(_guidance_tool(monkeypatch), "zzqx wvvy")
    assert result["status"] == "no_match" and result["available_categories"]


def _write_reference(directory, slug: str, *, hidden: bool = False) -> None:
    visibility = "production_visible: false\n" if hidden else ""
    (directory / f"{slug}.md").write_text(
        f"---\nname: {slug.title()}\ncategory: generic\n{visibility}tradecraft_tags: [tie-needle]\n---\nbody\n",
        encoding="utf-8",
    )


def test_hidden_references_never_appear_among_tied_candidates(monkeypatch, tmp_path):
    for slug in ("alpha-tool", "bravo-tool"):
        _write_reference(tmp_path, slug)
    _write_reference(tmp_path, "answer-key", hidden=True)
    monkeypatch.setattr(ttp_library, "TTP_DIR", tmp_path)

    result = _ask(_guidance_tool(monkeypatch), "tie needle")
    assert result["status"] == "ambiguous"
    assert [c["slug"] for c in result["candidates"]] == ["alpha-tool", "bravo-tool"]
    assert result["candidate_count"] == 2 and "answer-key" not in json.dumps(result)


def test_many_tied_candidates_are_truncated_explicitly_with_their_total(monkeypatch, tmp_path):
    slugs = [f"tool-{index:02d}" for index in range(11)]
    for slug in slugs:
        _write_reference(tmp_path, slug)
    monkeypatch.setattr(ttp_library, "TTP_DIR", tmp_path)

    result = _ask(_guidance_tool(monkeypatch), "tie needle")
    assert result["status"] == "ambiguous"
    assert [c["slug"] for c in result["candidates"]] == slugs[:8]
    assert result["candidate_count"] == 11 and result["truncated"] is True


def test_an_ambiguous_lookup_does_not_count_toward_the_guidance_cycle(monkeypatch):
    """The clarifying lookup that follows an ambiguity is a new read, not a third repetition of the same goal."""
    tool = _guidance_tool(monkeypatch)
    named = "run SharpHound to collect BloodHound data for a domain"
    assert _ask(tool, HISTORICAL_GOAL)["status"] == "ambiguous"
    assert "cycle_warning" not in _ask(tool, named)
    assert "cycle_warning" not in _ask(tool, named)
    # Positive control: a genuine third near-identical lookup still warns.
    assert "cycle_warning" in _ask(tool, named)
