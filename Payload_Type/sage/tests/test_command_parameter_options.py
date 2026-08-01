"""The shared Mythic command-parameter-schema resolver (ISC-1, ISC-7.2, ISC-9.2).

Hermetic: upstream is monkeypatched, no Mythic required. Async tests drive explicit event loops
because the suite deliberately does not configure pytest-asyncio.

Fixtures mirror shapes observed against live Apollo on 2026-08-01 rather than invented ones — the
zero-parameter shape in particular, because upstream's own docstring still documents the rc6
``{"Default": []}`` form while rc8 actually returns ``{"Default": {"example": ..., "parameters":
[]}}``. A fixture copied from the docstring would have asserted the wrong contract.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import command_parameter_schema as schema  # noqa: E402

PAYLOAD = "apollo"


class _Client:
    """Stand-in for a logged-in Mythic client; the resolver only passes it through."""


def _grouped(**groups):
    return {name: {"example": f"example for {name}", "parameters": params} for name, params in groups.items()}


def _param(name, ptype="String", **extra):
    base = {
        "name": name,
        "type": ptype,
        "required": True,
        "description": f"{name} description",
        "default_value": "",
        "choices": [],
        "parameter_group_name": "Default",
        "verifier_regex": "",
        # Fields upstream returns that must never reach the model.
        "id": 98,
        "cli_name": name.title(),
        "display_name": name.title(),
        "ui_position": 1,
        "dynamic_query_function": "",
        "limit_credentials_by_type": [],
        "choices_are_all_commands": False,
        "choices_are_loaded_commands": False,
        "choice_filter_by_command_attributes": {},
        "supported_agents": [],
        "supported_agent_build_parameters": {},
    }
    base.update(extra)
    return base


@pytest.fixture
def resolver():
    return schema.CommandSchemaResolver()


@pytest.fixture
def upstream(monkeypatch):
    """Install a fake ``get_command_parameter_options`` and count its calls."""
    from mythic import mythic as upstream_module

    calls: list[tuple[str, str]] = []
    state: dict[str, object] = {"result": _grouped(Default=[_param("domain")]), "raises": None}

    async def fake(*, mythic, command_name, payload_type_name):
        calls.append((payload_type_name, command_name))
        if state["raises"] is not None:
            raise state["raises"]
        return state["result"]

    monkeypatch.setattr(upstream_module, "get_command_parameter_options", fake)
    return {"calls": calls, "state": state}


def test_groups_expose_example_and_parameters(resolver, upstream):
    """ISC-1.1 — the grouped shape is the whole point; a flat list would defeat the change."""
    upstream["state"]["result"] = _grouped(AES128=[_param("domain")], Kerberos=[_param("ticket")])

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))

    assert sorted(result) == ["AES128", "Kerberos"]
    for group in result.values():
        assert sorted(group) == ["example", "parameters"]


def test_unknown_command_returns_none_without_raising(resolver, upstream, monkeypatch):
    """ISC-1.2 — upstream raises a bare Exception naming the command; callers must see None.

    Exercises the REAL fallback rather than stubbing it out. An earlier version of this test
    monkeypatched `_fetch_parameters_only` to raise, which made it agree with the assumption it was
    supposed to check: against live Mythic the fallback returned an empty `Default` group and an
    unknown command resolved to a valid-looking zero-parameter schema. A mock that encodes the
    expected answer tests nothing.
    """
    upstream["state"]["raises"] = Exception("Failed to find command 'nope' for payload type 'apollo'")

    async def empty_rows(*, mythic, query, variables):
        return {"commandparameters": []}

    from mythic import mythic_utilities

    monkeypatch.setattr(mythic_utilities, "graphql_post", empty_rows)

    assert asyncio.run(resolver.get(_Client(), PAYLOAD, "nope")) is None


def test_zero_rows_never_becomes_an_empty_schema(resolver, upstream, monkeypatch):
    """The ambiguity that caused the defect above, pinned directly.

    This query selects parameters, not commands, so zero rows cannot distinguish a missing command
    from a parameterless one. Resolving it optimistically would hand a caller an empty schema it
    believes is real, which is worse than no schema at all: `_validate_command_parameters` fails
    open on None but would happily validate against an empty group.
    """
    upstream["state"]["raises"] = KeyError("renderer blew up")

    async def empty_rows(*, mythic, query, variables):
        return {"commandparameters": []}

    from mythic import mythic_utilities

    monkeypatch.setattr(mythic_utilities, "graphql_post", empty_rows)

    assert asyncio.run(resolver.get(_Client(), PAYLOAD, "anything")) is None


def test_fallback_rebuilds_groups_from_parameter_group_name(resolver, upstream, monkeypatch):
    """When rows DO come back, the fallback must reproduce the grouped shape, not a flat list."""
    upstream["state"]["raises"] = ValueError("renderer blew up")

    async def rows(*, mythic, query, variables):
        return {
            "commandparameters": [
                {"name": "domain", "type": "String", "parameter_group_name": "AES128"},
                {"name": "aes_key", "type": "String", "parameter_group_name": "AES128"},
                {"name": "ticket", "type": "String", "parameter_group_name": "Kerberos"},
            ]
        }

    from mythic import mythic_utilities

    monkeypatch.setattr(mythic_utilities, "graphql_post", rows)

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))

    assert sorted(result) == ["AES128", "Kerberos"]
    assert [p["name"] for p in result["AES128"]["parameters"]] == ["domain", "aes_key"]
    assert "example" not in result["AES128"], "the fallback exists because the renderer failed"


@pytest.mark.parametrize(
    ("client", "payload_type", "command"),
    [
        (None, PAYLOAD, "pth"),
        (_Client(), None, "pth"),
        (_Client(), "", "pth"),
        (_Client(), PAYLOAD, None),
        (_Client(), PAYLOAD, ""),
    ],
)
def test_missing_inputs_return_none(resolver, upstream, client, payload_type, command):
    """ISC-1.3 — covered as a class: any absent input is 'schema unavailable', never an exception."""
    assert asyncio.run(resolver.get(client, payload_type, command)) is None
    assert upstream["calls"] == [], "an unresolvable request must not reach Mythic at all"


def test_zero_parameter_command_keeps_its_empty_group(resolver, upstream):
    """ISC-1.4 — an empty parameter list is a command's single valid tasking form, not an absence.

    Shape taken from live Apollo `exit`, not from upstream's docstring, which still describes rc6.
    """
    upstream["state"]["result"] = {"Default": {"example": "just a raw string", "parameters": []}}

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "exit"))

    assert result == {"Default": {"parameters": [], "example": "just a raw string"}}


def test_results_are_cached_per_payload_type_and_command(resolver, upstream):
    """ISC-1.5 — `_fetch_live_command_surface` walks every loaded command; without a cache that is
    dozens of identical round trips."""
    client = _Client()
    asyncio.run(resolver.get(client, PAYLOAD, "pth"))
    asyncio.run(resolver.get(client, PAYLOAD, "pth"))

    assert upstream["calls"] == [(PAYLOAD, "pth")], "second call must be served from cache"

    asyncio.run(resolver.get(client, PAYLOAD, "make_token"))
    asyncio.run(resolver.get(client, "merlin", "pth"))

    assert upstream["calls"] == [(PAYLOAD, "pth"), (PAYLOAD, "make_token"), ("merlin", "pth")], (
        "the cache key must include the payload type; sharing a command name across payload types "
        "would serve Apollo's schema for a Merlin task"
    )

    asyncio.run(resolver.get(client, PAYLOAD, "pth", use_cache=False))
    assert upstream["calls"][-1] == (PAYLOAD, "pth")


def test_only_selected_fields_reach_the_model(resolver, upstream):
    """ISC-7.2 — the raw 19-field payload peaks at 11,741 chars against a 4,000-char trigger."""
    upstream["state"]["result"] = _grouped(Default=[_param("domain")])

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))
    emitted = set(result["Default"]["parameters"][0])

    assert emitted <= set(schema.MODEL_VISIBLE_FIELDS)
    for leaked in ("id", "cli_name", "ui_position", "supported_agents", "dynamic_query_function"):
        assert leaked not in emitted, f"{leaked} is upstream bookkeeping and must not reach the model"


def test_llm_help_survives_field_selection(resolver, upstream):
    """ISC-3 depends on this: LLM_Help is the reference-format text the whole change exists to
    surface, so a field allowlist that dropped it would defeat the purpose silently."""
    upstream["state"]["result"] = _grouped(
        Default=[_param("credential", "CredentialJson", LLM_Help="expects @cred:<id>")]
    )

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))

    assert result["Default"]["parameters"][0]["LLM_Help"] == "expects @cred:<id>"


def test_empty_values_are_dropped(resolver, upstream):
    """An empty default is not a default, and dropping empties is most of the size win."""
    upstream["state"]["result"] = _grouped(Default=[_param("domain", default_value="", choices=[])])

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))
    emitted = result["Default"]["parameters"][0]

    assert "default_value" not in emitted
    assert "choices" not in emitted
    assert emitted["name"] == "domain"


def test_example_can_be_suppressed(resolver, upstream):
    """ISC-9.3's lever: one Apollo command (`sc`, five groups, 1,772 chars of prose) breaches the
    ceiling only because of example text."""
    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth", include_example=False))

    assert "example" not in result["Default"]
    assert result["Default"]["parameters"]


@pytest.mark.parametrize(
    "renderer_exception",
    [
        pytest.param(KeyError("choices_filter_by_command_attributes"), id="rc5_rc7_keyerror"),
        pytest.param(ValueError("invalid literal for int() with base 10: ''"), id="rc8_valueerror"),
        pytest.param(TypeError("unhashable"), id="unseen_type"),
    ],
)
def test_renderer_exception_degrades_to_parameters_without_example(
    resolver, upstream, monkeypatch, renderer_exception
):
    """ISC-9.2 — a universal over the failure class, not an example.

    The renderer has shipped a call-killing exception in every release that contained it, a
    different type each time. An example-shaped assertion would have passed at rc8 while the code
    was still crashing, which is exactly why this is parametrised over unseen types too.
    """
    upstream["state"]["raises"] = renderer_exception

    async def fake_fallback(client, payload_type, command):
        return {"Default": {"parameters": [{"name": "domain", "type": "String"}]}}

    monkeypatch.setattr(schema, "_fetch_parameters_only", fake_fallback)

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))

    assert result["Default"]["parameters"], "parameters must survive a renderer defect"
    assert "example" not in result["Default"], "only the example is lost"


def test_rc6_style_list_groups_are_tolerated(resolver, upstream):
    """A pin change back to rc6's `Dict[str, List[dict]]` must not silently yield empty schemas,
    which would fail open on every task while looking like a successful fetch."""
    upstream["state"]["result"] = {"Default": [_param("domain")]}

    result = asyncio.run(resolver.get(_Client(), PAYLOAD, "pth"))

    assert result["Default"]["parameters"][0]["name"] == "domain"
    assert "example" not in result["Default"]
