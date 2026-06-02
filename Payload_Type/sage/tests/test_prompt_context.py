"""Tests for ROADMAP Phase 3 Layer 1 — prompt_context providers.

The refactor extracted four prompt-variable producers out of model.py's agent
builders into ai/langgraph/prompt_context.py. Each provider is ``(model) -> str``
and must be BYTE-IDENTICAL to the inline block it replaced. These tests pin that
contract directly against prompt_context (model.py is deliberately NOT imported —
it pulls the mythic_container runtime).

What these tests pin:
  1. commands_text: populated (dict-shaped and list-shaped commands -> json.dumps
     indent=2; non-dict/list -> str()), empty dict, and None. The ``**Note:**``
     tail appears ONLY when populated.
  2. installed_payloads_text / installed_c2_profiles_text: populated join vs the
     "(No ... available)" sentinel for empty and None.
  3. servers_text: >5 tools (preview truncates with ``...``), <=5 tools (no
     truncation), and the none-connected sentinel — via monkeypatched MCPManager,
     no live MCP subsystem required.
  4. PROVIDERS registry: keys are exactly the four provider names and each value
     is callable.
  5. resolve: known names -> dict with those keys; unknown name skipped (absent,
     no raise); a provider that raises -> "" for that key (fail-safe).

Run: cd /home/john/dev/sage && .venv/bin/python -m pytest Payload_Type/sage/tests/test_prompt_context.py -q
"""
import json
import sys
from pathlib import Path

import pytest

# prompt_context imports ``from ai.mcp import MCPManager``, so 'ai' must be importable
# (Payload_Type/sage on the path) AND prompt_context itself importable by bare name
# (ai/langgraph on the path) — mirrors test_prompt_externalization path handling.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage -> 'ai.mcp'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import prompt_context  # noqa: E402


class FakeModel:
    """Minimal stand-in carrying only the attributes the providers read."""

    def __init__(self, cached_commands=None, payload_names=None, c2_profiles=None):
        self._cached_commands = cached_commands
        self._payload_names = payload_names
        self._c2_profiles = c2_profiles


# --------------------------------------------------------------------------- #
# commands_text
# --------------------------------------------------------------------------- #

NOTE_TAIL = (
    "\n**Note:** Use the get_all_commands_for_payloadtype tool if you need "
    "commands for other payload types or want to refresh this data.\n"
)


def test_commands_text_populated_dict_and_list_shapes():
    """dict-shaped and list-shaped commands both json.dumps(indent=2); Note tail appended."""
    cached = {
        "apollo": {"shell": {"description": "run a shell command"}},  # dict shape
        "poseidon": ["ls", "cat", "whoami"],                          # list shape
    }
    model = FakeModel(cached_commands=cached)

    expected = ""
    for payload_name, commands in cached.items():
        commands_json = json.dumps(commands, indent=2)  # both are dict/list
        expected += f"\n### Available Commands for '{payload_name}' Payload:\n{commands_json}\n"
    expected += NOTE_TAIL

    result = prompt_context.commands_text(model)
    assert result == expected
    # Sanity: the JSON really was indent=2 (multi-line), not a compact dump.
    assert '\n  "shell"' in result
    assert result.endswith(NOTE_TAIL)


def test_commands_text_non_dict_or_list_uses_str_fallback():
    """A scalar command value falls back to str(commands), not json.dumps."""
    cached = {"raw": 12345}  # not a dict/list -> str()
    model = FakeModel(cached_commands=cached)

    expected = "\n### Available Commands for 'raw' Payload:\n12345\n" + NOTE_TAIL
    assert prompt_context.commands_text(model) == expected


def test_commands_text_empty_dict_returns_empty_no_note():
    """Empty dict is falsy -> empty string and NO Note tail."""
    assert prompt_context.commands_text(FakeModel(cached_commands={})) == ""


def test_commands_text_none_returns_empty_no_note():
    """None is falsy -> empty string and NO Note tail."""
    assert prompt_context.commands_text(FakeModel(cached_commands=None)) == ""


# --------------------------------------------------------------------------- #
# installed_payloads_text
# --------------------------------------------------------------------------- #

PAYLOADS_SENTINEL = "        - (No payload data available)"


def test_installed_payloads_text_populated():
    model = FakeModel(payload_names=["apollo", "poseidon", "medusa"])
    expected = "        - apollo\n        - poseidon\n        - medusa"
    assert prompt_context.installed_payloads_text(model) == expected


def test_installed_payloads_text_empty_returns_sentinel():
    assert prompt_context.installed_payloads_text(FakeModel(payload_names=[])) == PAYLOADS_SENTINEL


def test_installed_payloads_text_none_returns_sentinel():
    assert prompt_context.installed_payloads_text(FakeModel(payload_names=None)) == PAYLOADS_SENTINEL


# --------------------------------------------------------------------------- #
# installed_c2_profiles_text
# --------------------------------------------------------------------------- #

C2_SENTINEL = "        - (No C2 profile data available)"


def test_installed_c2_profiles_text_populated():
    profiles = [
        {"name": "http", "description": "HTTP egress profile"},
        {"name": "smb", "description": "SMB named-pipe profile"},
    ]
    model = FakeModel(c2_profiles=profiles)
    expected = (
        "        - http: HTTP egress profile\n"
        "        - smb: SMB named-pipe profile"
    )
    assert prompt_context.installed_c2_profiles_text(model) == expected


def test_installed_c2_profiles_text_empty_returns_sentinel():
    assert prompt_context.installed_c2_profiles_text(FakeModel(c2_profiles=[])) == C2_SENTINEL


def test_installed_c2_profiles_text_none_returns_sentinel():
    assert prompt_context.installed_c2_profiles_text(FakeModel(c2_profiles=None)) == C2_SENTINEL


# --------------------------------------------------------------------------- #
# servers_text — monkeypatch the module-level MCPManager (no live subsystem)
# --------------------------------------------------------------------------- #

SERVERS_NONE_SENTINEL = (
    "\n**No MCP servers currently connected.** "
    "Inform the user to use `mcp-connect` command first.\n"
)


def test_servers_text_more_than_five_tools_truncates_with_ellipsis(monkeypatch):
    """A server with >5 tools previews the first 5 and appends '...'."""
    tool_names = ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    monkeypatch.setattr(prompt_context.MCPManager, "get_connected_servers",
                        staticmethod(lambda: ["alpha"]))
    monkeypatch.setattr(prompt_context.MCPManager, "get_tools_summary",
                        staticmethod(lambda: {
                            "server_summaries": {
                                "alpha": {"tool_count": 7, "tool_names": tool_names}
                            }
                        }))

    expected = (
        "\n**Currently Connected MCP Servers:** 1\n"
        "- alpha: 7 tools (t1, t2, t3, t4, t5...)\n"
    )
    assert prompt_context.servers_text(FakeModel()) == expected


def test_servers_text_five_or_fewer_tools_no_ellipsis(monkeypatch):
    """A server with <=5 tools previews all of them with no '...'."""
    tool_names = ["a", "b", "c"]
    monkeypatch.setattr(prompt_context.MCPManager, "get_connected_servers",
                        staticmethod(lambda: ["beta"]))
    monkeypatch.setattr(prompt_context.MCPManager, "get_tools_summary",
                        staticmethod(lambda: {
                            "server_summaries": {
                                "beta": {"tool_count": 3, "tool_names": tool_names}
                            }
                        }))

    expected = (
        "\n**Currently Connected MCP Servers:** 1\n"
        "- beta: 3 tools (a, b, c)\n"
    )
    result = prompt_context.servers_text(FakeModel())
    assert result == expected
    assert "..." not in result


def test_servers_text_none_connected_returns_sentinel(monkeypatch):
    monkeypatch.setattr(prompt_context.MCPManager, "get_connected_servers",
                        staticmethod(lambda: []))
    # get_tools_summary must not even be consulted when nothing is connected.
    def _boom():
        raise AssertionError("get_tools_summary should not be called when no servers connected")
    monkeypatch.setattr(prompt_context.MCPManager, "get_tools_summary", staticmethod(_boom))

    assert prompt_context.servers_text(FakeModel()) == SERVERS_NONE_SENTINEL


# --------------------------------------------------------------------------- #
# PROVIDERS registry
# --------------------------------------------------------------------------- #

def test_providers_keys_are_exactly_the_four_names():
    assert set(prompt_context.PROVIDERS.keys()) == {
        "commands_text",
        "installed_payloads_text",
        "installed_c2_profiles_text",
        "servers_text",
    }


def test_providers_values_are_callable():
    for name, fn in prompt_context.PROVIDERS.items():
        assert callable(fn), f"provider {name} is not callable"


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #

def test_resolve_known_names_returns_dict_with_those_keys():
    model = FakeModel(payload_names=["apollo"], c2_profiles=[])
    out = prompt_context.resolve(model, "installed_payloads_text", "installed_c2_profiles_text")
    assert set(out.keys()) == {"installed_payloads_text", "installed_c2_profiles_text"}
    assert out["installed_payloads_text"] == "        - apollo"
    assert out["installed_c2_profiles_text"] == C2_SENTINEL


def test_resolve_unknown_name_is_skipped_without_raising():
    model = FakeModel(payload_names=["apollo"])
    out = prompt_context.resolve(model, "installed_payloads_text", "does_not_exist")
    assert "does_not_exist" not in out
    assert out == {"installed_payloads_text": "        - apollo"}


def test_resolve_provider_that_raises_yields_empty_string(monkeypatch):
    """A provider raising must not block resolve — that key becomes ''."""
    def _raises(_model):
        raise RuntimeError("boom")

    monkeypatch.setitem(prompt_context.PROVIDERS, "commands_text", _raises)
    out = prompt_context.resolve(FakeModel(), "commands_text")
    assert out == {"commands_text": ""}
