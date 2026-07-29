"""Tests for ROADMAP Phase 3 — externalized agent system prompts (prompt_loader).

The migration moved LangGraph agent system prompts out of model.py inline
f-strings into editable markdown files under Payload_Type/sage/prompts/, loaded
at agent-build time by ai/langgraph/prompt_loader.py.

(Byte-identity to the pre-extraction code was proven during the migration; that one-time
oracle is not kept as a committed fixture because the prompt files are now the editable
source of truth — a frozen snapshot would false-fail on any legitimate prompt edit.)

The core guarantee these tests pin:
  1. The render actually substituted placeholders (the loader's raw-body fallback
     is NOT silently masking a broken template on the render path).
  2. Frontmatter parses correctly (name + tools list) for each file.
  3. A markdown `---` rule in the BODY is not mistaken for a frontmatter delimiter.
  4. A malformed template never raises — it falls back to the raw body.
  5. filter_tools_by_frontmatter keeps/drops/orders tools per frontmatter.
  6. The MCP passthrough pattern (mcp_tools + filtered static tools) preserves
     runtime-discovered MCP tools that are not in frontmatter.
  7. Prompts are read per call (T3.3 / ISC-26-27) — editing a file and rebuilding
     the agent picks up the change with no code edit or process restart.

model.py is deliberately NOT imported (it pulls the mythic_container runtime);
prompt_loader is tested directly with tiny mock tool objects.

Run from the repository root: .venv/bin/python -m pytest Payload_Type/sage/tests/test_prompt_externalization.py -q
"""
import logging
import sys
from pathlib import Path

import pytest

# Import the loader directly (mirrors test_ttp_library / test_circuit_breaker path handling).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import prompt_loader  # noqa: E402

# EXACT interpolation values used to exercise the f-string render path (no-fallback test).
FIXTURES = {
    "generalist": {},
    "mythic_operator": {
        "commands_text": "\n### Available Commands for 'apollo' Payload:\n[\"whoami\",\"ps\"]\n\n**Note:** Use the get_all_commands_for_payloadtype tool if you need commands for other payload types or want to refresh this data.\n"
    },
    "mythic_payload": {
        "installed_payloads_text": "        - apollo\n        - merlin",
        "installed_c2_profiles_text": "        - http: HTTP C2",
    },
    "mcp_manager": {
        "servers_text": "\n**Currently Connected MCP Servers:** 1\n- bloodhound: 13 tools (graph_analysis, cypher_query)\n"
    },
    "bloodhound": {
        "servers_text": "\n**Currently Connected MCP Servers:** 1\n- bloodhound: 13 tools (graph_analysis, cypher_query)\n"
    },
    "sandbox": {},
    "supervisor": {},
}

# Expected frontmatter `name` and `tools` length per agent.
EXPECTED_META = {
    "generalist": ("Generalist", 0),
    "mythic_operator": ("Mythic_Operator", 25),
    "mythic_payload": ("Mythic_Payload", 13),
    "mcp_manager": ("MCP_Manager", 1),
    "bloodhound": ("BloodHound", 4),
    "sandbox": ("Sandbox", 2),
    "supervisor": ("Supervisor", 8),
}

# A distinctive, post-substitution substring per agent. Its presence proves the
# placeholder was actually rendered (not left as a literal `{...}` by the fallback).
# Agents with no placeholders use a stable phrase from their body.
DISTINCTIVE_RENDERED = {
    "generalist": "You are a Generalist Agent",
    "mythic_operator": "### Available Commands for 'apollo'",
    "mythic_payload": "- apollo\n        - merlin",
    "mcp_manager": "**Currently Connected MCP Servers:** 1",
    "bloodhound": "You are the **BloodHound Agent**",
    "sandbox": "You are the **Sandbox Agent**",
    "supervisor": "You are a Supervisor Agent",
}

# Placeholders that MUST be gone from the golden-path output (the f-string agents).
PLACEHOLDERS = {
    "mythic_operator": ["{commands_text}"],
    "mythic_payload": ["{installed_payloads_text}", "{installed_c2_profiles_text}"],
    "mcp_manager": ["{servers_text}"],
    "bloodhound": ["{servers_text}"],
}

AGENTS = list(FIXTURES.keys())


class _Tool:
    """Minimal stand-in for a LangChain tool: only the `.name` attribute matters."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):  # nicer assertion failure messages
        return f"_Tool({self.name!r})"


# ---------------------------------------------------------------------------
# 1. No silent fallback on the render path — the substitution really happened.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", AGENTS)
def test_golden_path_actually_substituted_no_fallback(name, caplog):
    # If render fell back to the raw body, the loader logs an ERROR. Capture it.
    with caplog.at_level(logging.ERROR, logger="prompt_loader"):
        rendered = prompt_loader.load_prompt(name, **FIXTURES[name])

    # No raw-body fallback occurred.
    fallback_errors = [r for r in caplog.records if "using RAW body" in r.getMessage()]
    assert not fallback_errors, f"{name}: loader fell back to raw body: {fallback_errors}"

    # Distinctive rendered content is present.
    assert DISTINCTIVE_RENDERED[name] in rendered, (
        f"{name}: expected rendered marker {DISTINCTIVE_RENDERED[name]!r} not found."
    )

    # And no un-substituted single-braced placeholder survived for the f-string agents.
    for ph in PLACEHOLDERS.get(name, []):
        assert ph not in rendered, f"{name}: placeholder {ph!r} was left unrendered."


# ---------------------------------------------------------------------------
# 3. Frontmatter parse — name + tools length per file.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", AGENTS)
def test_frontmatter_name_and_tools_length(name):
    expected_name, expected_tools_len = EXPECTED_META[name]
    meta = prompt_loader.load_prompt_meta(name)
    assert isinstance(meta, dict), f"{name}: frontmatter did not parse to a dict."
    assert meta.get("name") == expected_name, (
        f"{name}: frontmatter name {meta.get('name')!r} != expected {expected_name!r}."
    )
    tools = meta.get("tools") or []
    assert len(tools) == expected_tools_len, (
        f"{name}: frontmatter tools length {len(tools)} != expected {expected_tools_len}."
    )
    # get_prompt_tools must agree with the raw frontmatter list.
    assert len(prompt_loader.get_prompt_tools(name)) == expected_tools_len


# ---------------------------------------------------------------------------
# 4. A `---` markdown rule in the BODY must not be parsed as frontmatter.
# ---------------------------------------------------------------------------
def test_body_horizontal_rule_is_not_over_split(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", tmp_path)
    body = (
        "Intro paragraph before any rule.\n"
        "\n"
        "---\n"  # a genuine markdown horizontal rule inside the body
        "\n"
        "Section after the rule with a {value} placeholder.\n"
        "\n"
        "---\n"  # a second rule, to be extra sure we don't split on it
        "\n"
        "Trailing section."
    )
    file_text = "---\nname: RuleAgent\ntools:\n  - alpha\n---\n" + body
    (tmp_path / "rule_agent.md").write_text(file_text, encoding="utf-8")

    meta = prompt_loader.load_prompt_meta("rule_agent")
    assert meta == {"name": "RuleAgent", "tools": ["alpha"]}

    rendered = prompt_loader.load_prompt("rule_agent", value="XYZ")
    # The full body survived: both rules and both sections are present.
    assert "Intro paragraph before any rule." in rendered
    assert "Section after the rule with a XYZ placeholder." in rendered
    assert "Trailing section." in rendered
    assert rendered.count("---") == 2, "both body horizontal rules must be preserved"


def test_no_leading_frontmatter_treats_whole_file_as_body(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", tmp_path)
    (tmp_path / "no_fm.md").write_text("Just a body.\n\n---\n\nMore body.", encoding="utf-8")
    assert prompt_loader.load_prompt_meta("no_fm") == {}
    rendered = prompt_loader.load_prompt("no_fm")
    assert "Just a body." in rendered and "More body." in rendered


# ---------------------------------------------------------------------------
# 5. Malformed template → no raise, raw-body fallback.
# ---------------------------------------------------------------------------
def test_unbalanced_brace_falls_back_to_raw_body_without_raising(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", tmp_path)
    raw_body = "Hello {bad"  # unterminated brace -> str.format raises ValueError
    (tmp_path / "broken.md").write_text(
        "---\nname: Broken\n---\n" + raw_body, encoding="utf-8"
    )
    with caplog.at_level(logging.ERROR, logger="prompt_loader"):
        rendered = prompt_loader.load_prompt("broken")  # must NOT raise
    assert rendered == raw_body.strip()
    assert any("using RAW body" in r.getMessage() for r in caplog.records), (
        "an ERROR explaining the raw-body fallback should have been logged"
    )


def test_missing_key_placeholder_falls_back_to_raw_body(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", tmp_path)
    raw_body = "Needs {missing_key} that the caller did not supply."
    (tmp_path / "missing.md").write_text(
        "---\nname: Missing\n---\n" + raw_body, encoding="utf-8"
    )
    with caplog.at_level(logging.ERROR, logger="prompt_loader"):
        rendered = prompt_loader.load_prompt("missing")  # KeyError -> fallback, no raise
    # Raw body returned verbatim (placeholder still literal), stripped.
    assert rendered == raw_body.strip()
    assert "{missing_key}" in rendered


# ---------------------------------------------------------------------------
# 6. filter_tools_by_frontmatter — keep / drop / order.
# ---------------------------------------------------------------------------
def test_filter_keeps_frontmatter_set_drops_others_and_preserves_order():
    fm_tools = prompt_loader.get_prompt_tools("mythic_operator")
    assert len(fm_tools) == 25

    # Build candidates: every frontmatter tool, reordered, plus one interloper not in
    # frontmatter ("rogue_tool"). Reversing proves order follows CANDIDATES, not frontmatter.
    candidates = [_Tool(n) for n in reversed(fm_tools)]
    rogue = _Tool("rogue_tool_not_in_frontmatter")
    candidates.insert(3, rogue)  # drop it somewhere in the middle

    kept = prompt_loader.filter_tools_by_frontmatter("mythic_operator", candidates)
    kept_names = [t.name for t in kept]

    # Dropped the interloper.
    assert "rogue_tool_not_in_frontmatter" not in kept_names
    # Kept exactly the frontmatter set (as a set).
    assert set(kept_names) == set(fm_tools)
    assert len(kept_names) == 25
    # Order is the CANDIDATE order (reversed frontmatter, with rogue removed) — not
    # the frontmatter order.
    assert kept_names == list(reversed(fm_tools))


def test_filter_with_empty_frontmatter_drops_everything():
    # generalist has 0 frontmatter tools; nothing should survive the filter.
    candidates = [_Tool("anything"), _Tool("else")]
    assert prompt_loader.filter_tools_by_frontmatter("generalist", candidates) == []


# ---------------------------------------------------------------------------
# 7. MCP passthrough pattern — runtime MCP tools survive alongside static tools.
# ---------------------------------------------------------------------------
def test_bloodhound_passthrough_keeps_runtime_tools_and_static_set():
    # This mirrors model.py's BloodHound agent (the passthrough pattern moved there):
    #   tools = mcp_tools + filter_tools_by_frontmatter("bloodhound", ttp_tools + [handback_tool])
    static_fm_tools = prompt_loader.get_prompt_tools("bloodhound")
    assert len(static_fm_tools) == 4

    # Runtime-discovered MCP tools are NOT in frontmatter and must NOT be filtered.
    mcp_tools = [_Tool("graph_analysis"), _Tool("cypher_query"), _Tool("adcs_info")]
    static_candidates = [_Tool(n) for n in static_fm_tools]

    filtered_static = prompt_loader.filter_tools_by_frontmatter(
        "bloodhound", static_candidates
    )
    combined = mcp_tools + filtered_static
    combined_names = [t.name for t in combined]

    # All 3 runtime MCP tools survive (they bypass the filter entirely).
    assert combined_names[:3] == ["graph_analysis", "cypher_query", "adcs_info"]
    # And all 5 static frontmatter tools survive the filter.
    assert set(combined_names[3:]) == set(static_fm_tools)
    assert len(combined_names) == 3 + 4


# ---------------------------------------------------------------------------
# 8. Reload-at-call-time (T3.3 / ISC-26-27) — files read per call, not cached.
# ---------------------------------------------------------------------------
def test_prompts_are_read_per_call_not_cached_at_import(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", tmp_path)
    target = tmp_path / "reloadable.md"
    target.write_text(
        "---\nname: Reloadable\n---\nOriginal line.", encoding="utf-8"
    )

    first = prompt_loader.load_prompt("reloadable")
    assert first == "Original line."

    # Operator edits the file between agent builds (no process restart, no code change).
    sentinel = "SENTINEL_EDIT_42"
    target.write_text(
        "---\nname: Reloadable\n---\nOriginal line.\n" + sentinel, encoding="utf-8"
    )

    second = prompt_loader.load_prompt("reloadable")
    assert sentinel in second, "edit must be visible on the next load (no import-time cache)"
    assert first != second


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
