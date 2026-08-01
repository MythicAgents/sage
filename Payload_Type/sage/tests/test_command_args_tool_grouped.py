"""`get_all_command_args_for_payloadtype` after consolidation (ISC-2.1, ISC-3.1–3.3).

The live probe on 2026-08-01 showed the real thing working — `link` carrying LLM_Help on
`connection_info`, `unlink` on `link_info`, `pth` returning its four real groups. These tests exist
so a future refactor cannot quietly undo that: a criterion closed on a one-off probe has nothing
watching it afterwards.

Hermetic. The resolver is monkeypatched, so nothing here needs Mythic.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import command_parameter_schema  # noqa: E402
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402


def _tool(monkeypatch, *, groups, metadata=None):
    """A MythicTools with only the collaborators this path touches."""
    tool = MythicTools.__new__(MythicTools)
    tool.client = object()

    async def fake_schema(client, payload_type, command, **kwargs):
        return groups

    async def fake_metadata(self, payload, command):
        return metadata or {}

    monkeypatch.setattr(command_parameter_schema, "get_command_schema", fake_schema)
    monkeypatch.setattr(MythicTools, "_fetch_command_metadata", fake_metadata)
    return tool


def _call(tool, payload="apollo", command="pth"):
    return asyncio.run(tool.get_all_command_args_for_payloadtype(payload, command))


def test_tool_output_is_group_keyed(monkeypatch):
    """ISC-2.1 — groups are mutually exclusive tasking forms; a flat list makes the model
    re-derive the partition it was handed the answer to."""
    tool = _tool(
        monkeypatch,
        groups={
            "AES128": {"example": "pth -Domain x", "parameters": [{"name": "domain"}]},
            "NTLM": {"example": "pth -NTLM y", "parameters": [{"name": "ntlm"}]},
        },
    )

    parsed = json.loads(_call(tool))

    assert sorted(parsed["parameter_groups"]) == ["AES128", "NTLM"]
    assert parsed["command"] == "pth"
    assert parsed["payload_type"] == "apollo"


@pytest.mark.parametrize(
    ("param_type", "param_name", "help_text"),
    [
        pytest.param("CredentialJson", "credential", "expects @cred:<id>", id="isc_3_1"),
        pytest.param("AgentConnect", "connection_info", "@link:callback=3,c2=smb", id="isc_3_2"),
        pytest.param("LinkInfo", "link_info", "@link:edge=145", id="isc_3_3"),
    ],
)
def test_llm_help_reaches_model_visible_output(monkeypatch, param_type, param_name, help_text):
    """ISC-3.1–3.3 — the reference format has to be in front of the model while it chooses a value.

    Parametrised over all three reference types together because they fail as a class: a field
    allowlist that drops `LLM_Help` breaks every one of them at once, and the live census found
    exactly six such parameters across Apollo's 81 commands — too few to notice by accident.
    """
    tool = _tool(
        monkeypatch,
        groups={
            "Default": {
                "parameters": [
                    {"name": param_name, "type": param_type, "LLM_Help": help_text}
                ]
            }
        },
    )

    raw = _call(tool)

    assert help_text in raw, "LLM_Help must survive into the serialized tool result"
    parsed = json.loads(raw)
    assert parsed["parameter_groups"]["Default"]["parameters"][0]["LLM_Help"] == help_text


def test_unknown_command_does_not_return_an_empty_schema(monkeypatch):
    """A resolver `None` must surface as an error the model can act on.

    Returning an empty parameter set instead would read as "this command takes no arguments" and
    invite a task with no parameters — worse than the fail-open behaviour being replaced, because
    the model would believe it had a schema.
    """
    tool = _tool(monkeypatch, groups=None)

    raw = _call(tool, command="definitely_not_a_command")

    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert "no parameter schema found" in raw
    assert "definitely_not_a_command" in raw


def test_command_metadata_is_carried_when_present(monkeypatch):
    """`needs_admin` is not decoration — footprint.py scores Mythic-native risk from it."""
    tool = _tool(
        monkeypatch,
        groups={"Default": {"parameters": []}},
        metadata={"needs_admin": True, "help_cmd": "pth -Domain x"},
    )

    parsed = json.loads(_call(tool))

    assert parsed["needs_admin"] is True
    assert parsed["help_cmd"] == "pth -Domain x"


def test_absent_metadata_is_omitted_rather_than_falsified(monkeypatch):
    """Live Apollo has zero commands with needs_admin true, so absent must not become `False`
    in the output — an explicit False reads as a checked negative rather than an unknown."""
    tool = _tool(
        monkeypatch,
        groups={"Default": {"parameters": []}},
        metadata={"needs_admin": False, "help_cmd": ""},
    )

    parsed = json.loads(_call(tool))

    assert "needs_admin" not in parsed
    assert "help_cmd" not in parsed
    assert parsed["parameter_groups"] == {"Default": {"parameters": []}}


def test_metadata_failure_does_not_fail_the_tool(monkeypatch):
    """The parameters are the point; losing command-level extras degrades the answer, not the call."""
    tool = MythicTools.__new__(MythicTools)
    tool.client = object()

    async def fake_schema(client, payload_type, command, **kwargs):
        return {"Default": {"parameters": [{"name": "domain"}]}}

    async def exploding_metadata(self, payload, command):
        raise RuntimeError("graphql exploded")

    monkeypatch.setattr(command_parameter_schema, "get_command_schema", fake_schema)
    monkeypatch.setattr(MythicTools, "_fetch_command_metadata", exploding_metadata)

    raw = asyncio.run(tool.get_all_command_args_for_payloadtype("apollo", "pth"))

    assert "Error getting command" in raw, (
        "an exception from metadata currently surfaces as a tool error; if that is ever softened, "
        "this test should be updated deliberately rather than silently"
    )


def test_commands_tool_grouped_view_does_not_mutate_the_cached_results():
    """ISC-2.6 — the plural tool's grouped view must be a copy.

    `results` is stored in `_command_schema_cache`, which `_fetch_live_command_surface` reads and
    `mechanic_repair.compact_command_surface` walks looking for the flat `commandparameters` key.
    An earlier draft grouped in place and deleted that key, which would have silently broken repair
    on the live path while every test still passed — the coupling is across modules, not within one.
    """
    cached = [
        {
            "cmd": "pth",
            "needs_admin": False,
            "commandparameters": [
                {"name": "domain", "type": "String", "parameter_group_name": "AES128"},
                {"name": "ntlm", "type": "String", "parameter_group_name": "NTLM"},
            ],
        }
    ]

    view = MythicTools._grouped_command_view(cached)

    assert "commandparameters" in cached[0], "the cached structure must survive untouched"
    assert "commandparameters" not in view[0], "the model view drops the flat list"
    assert sorted(view[0]["parameter_groups"]) == ["AES128", "NTLM"]
    assert view[0]["needs_admin"] is False, "command-level fields carry through"


@pytest.mark.parametrize(
    "results",
    [
        pytest.param(None, id="none"),
        pytest.param("an error string", id="error_string"),
        pytest.param([{"cmd": "x"}], id="command_without_parameters"),
        pytest.param(["not a dict"], id="non_dict_entry"),
    ],
)
def test_commands_tool_grouped_view_passes_unexpected_shapes_through(results):
    """A listing tool that returns nothing is worse than one that returns the old shape, so
    anything unexpected passes through rather than raising."""
    assert MythicTools._grouped_command_view(results) is not None or results is None


def test_no_interpolated_graphql_remains_in_the_args_tool():
    """ISC-2.5, scoped to this tool: the old body built its query by substituting model-influenced
    values into query text. A malformed query returns nothing, which the caller cannot distinguish
    from a real empty answer, so validation silently fails open — the class consolidation removes.

    Inspects the AST rather than the source text. A text match trips on any comment that *mentions*
    the old pattern, which is exactly what happened when this test first ran: it matched the
    docstring below explaining the history. A guard that cannot tell code from prose fails for the
    wrong reason and gets weakened to make it pass.
    """
    module = ast.parse(
        (Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "mythic_tools.py").read_text(
            encoding="utf-8"
        )
    )
    targets = {"get_all_command_args_for_payloadtype", "_fetch_command_metadata"}
    found = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in targets
    }
    assert set(found) == targets, f"expected both functions, found {sorted(found)}"

    for name, node in found.items():
        replace_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "replace"
        ]
        assert not replace_calls, f"{name} still builds a query by string substitution"

    metadata_source = ast.get_source_segment(
        (Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "mythic_tools.py").read_text(
            encoding="utf-8"
        ),
        found["_fetch_command_metadata"],
    )
    assert "$command_name" in metadata_source, (
        "the replacement path must be parameterized, not merely free of .replace()"
    )
