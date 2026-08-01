"""Regression coverage for MythicTools._validate_command_parameters.

The validation layer previously referenced an undefined `payload_type` in its ARGVAL log
lines, so EVERY validation path raised NameError and was swallowed by the fail-open handler —
silently disabling all parameter validation. These tests pin the contract: validation must
resolve, not raise, on both the accept and reject paths (the reject path is what regressed).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage

from ai.langgraph.mythic_tools import MythicTools  # noqa: E402


def _tools_with_schema(monkeypatch_schema):
    tools = MythicTools("agent-task-id")
    tools.client = object()
    # Both helpers are async; stub them so validation runs without a live Mythic.
    tools._resolve_payload_type = AsyncMock(return_value="apollo")
    tools._fetch_command_schema = AsyncMock(return_value=monkeypatch_schema)
    return tools


_SCHEMA = [
    {"name": "host", "cli_name": "host", "type": "String",
     "parameter_group_name": "Default", "required": True, "choices": None},
]


def test_validate_accepts_valid_params_without_raising():
    # Exercises the "validated" log line that references payload_type — must not NameError.
    tools = _tools_with_schema(_SCHEMA)
    result = asyncio.run(tools._validate_command_parameters("ls", {"host": "WINTERFELL"}, 2))
    assert result is None  # valid -> passes validation (None), did NOT fail open via exception


def test_validate_rejects_unknown_param_instead_of_failing_open():
    # The regressed path: an unknown key hits the rejection log that references payload_type.
    # Before the fix this raised NameError and fell through to fail-open (returned None).
    tools = _tools_with_schema(_SCHEMA)
    result = asyncio.run(tools._validate_command_parameters("ls", {"bogus_key": "x"}, 2))
    assert isinstance(result, str)  # a real rejection string, not a silent fail-open None
    assert "bogus_key" in result


# --- ISC-2.2 / ISC-2.3: consolidation onto the shared resolver -------------------------------
#
# `_fetch_command_schema` delegates its fetch to the resolver but keeps the flat return contract,
# because it has nine production callers and is stubbed in roughly seventy tests. Validation takes
# the grouped view from the shared policy rather than re-deriving the partition itself.

_MULTI_GROUP_SCHEMA = [
    {"name": "domain", "cli_name": "Domain", "type": "String",
     "parameter_group_name": "NTLM", "required": True, "choices": None},
    {"name": "ntlm", "cli_name": "NTLM", "type": "String",
     "parameter_group_name": "NTLM", "required": True, "choices": None},
    {"name": "domain", "cli_name": "Domain", "type": "String",
     "parameter_group_name": "AES256", "required": True, "choices": None},
    {"name": "aes256", "cli_name": "AES256", "type": "String",
     "parameter_group_name": "AES256", "required": True, "choices": None},
]


def test_validation_accepts_a_cli_named_parameter():
    """ISC-2.3 regression guard for the field that nearly broke this path.

    Validation builds its accepted-key set from BOTH `name` and `cli_name`. The model-facing
    projection deliberately drops `cli_name` as bookkeeping, so routing validation through that
    projection would have rejected every CLI-named parameter as unknown — wrongly blocking real
    tasks. VALIDATION_FIELDS exists precisely to keep it.
    """
    tools = _tools_with_schema(_MULTI_GROUP_SCHEMA)
    result = asyncio.run(
        tools._validate_command_parameters("pth", {"Domain": "north.local", "NTLM": "a" * 32}, 2)
    )
    assert result is None, "a cli_name-keyed parameter set must validate, not reject"


def test_validation_still_rejects_a_mixed_parameter_group():
    """mode=B — the groups are mutually exclusive, which is the whole reason for grouping."""
    tools = _tools_with_schema(_MULTI_GROUP_SCHEMA)
    result = asyncio.run(
        tools._validate_command_parameters("pth", {"ntlm": "a" * 32, "aes256": "b" * 32}, 2)
    )
    assert isinstance(result, str)
    assert "exactly ONE parameter group" in result


def test_validation_still_reports_missing_required_parameters():
    """mode=required — naming what is missing is what saves a wasted Mythic round trip."""
    tools = _tools_with_schema(_MULTI_GROUP_SCHEMA)
    result = asyncio.run(tools._validate_command_parameters("pth", {"domain": "north.local"}, 2))
    assert isinstance(result, str)
    assert "requires:" in result


def test_validation_still_rejects_a_bad_chooseone_value():
    """mode=C — ChooseOne is checked against `choices`, which the projection must preserve."""
    schema = [
        {"name": "mode", "cli_name": "Mode", "type": "ChooseOne",
         "parameter_group_name": "Default", "required": True, "choices": ["read", "write"]},
    ]
    tools = _tools_with_schema(schema)
    result = asyncio.run(tools._validate_command_parameters("reg", {"mode": "delete"}, 2))
    assert isinstance(result, str)
    assert "ChooseOne" in result and "read" in result


def test_validation_fails_open_when_schema_is_unavailable():
    """The contract that must never regress: unavailable schema permits the task.

    A validator that cannot answer must not block. `None` (no schema) and `[]` (a real
    zero-parameter command) both reach this, and both must return None rather than a rejection.
    """
    for schema in (None, []):
        tools = _tools_with_schema(schema)
        result = asyncio.run(tools._validate_command_parameters("ls", {"anything": "x"}, 2))
        assert result is None, f"schema={schema!r} must fail open, not reject"


def test_advisory_arm_never_rejects_on_verifier_regex():
    """ISC-4.1 — the advisory arm surfaces `verifier_regex` and rejects nothing on it.

    This is the default arm and must stay inert. Measured against live Mythic on 2026-08-01,
    `verifier_regex` is empty on all 192 parameters across every installed payload type, so the
    field never reaches the model in practice today and no arm can fire on real data. The fixture
    below is therefore synthetic on purpose: it pins the behaviour that a NON-empty regex, whenever
    an agent starts setting one, still does not block a task under the default configuration.
    """
    schema = [
        {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "Default",
         "required": True, "choices": None, "verifier_regex": r"^[A-Z]+$"},
    ]
    tools = _tools_with_schema(schema)

    result = asyncio.run(tools._validate_command_parameters("ls", {"host": "lowercase"}, 2))

    assert result is None, (
        "the advisory arm must not reject a regex mismatch; a gate that has never run against real "
        "data must not be the one deciding whether a live task is issued"
    )


# --- ISC-4.2 / 4.3 / 4.5: the verifier_regex rejection arm ------------------------------------

_REGEX_SCHEMA = [
    {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "Default",
     "required": True, "choices": None, "verifier_regex": r"^[A-Z]+$"},
]


def _validate(schema, params, mode=None, monkeypatch=None):
    if monkeypatch is not None:
        if mode is None:
            monkeypatch.delenv(MythicTools.VERIFIER_REGEX_MODE_ENV, raising=False)
        else:
            monkeypatch.setenv(MythicTools.VERIFIER_REGEX_MODE_ENV, mode)
    return asyncio.run(_tools_with_schema(schema)._validate_command_parameters("ls", params, 2))


def test_rejection_arm_rejects_a_mismatch(monkeypatch):
    """ISC-4.2 — armed, a value failing the declared pattern gets an actionable correction."""
    result = _validate(_REGEX_SCHEMA, {"host": "lowercase"}, mode="reject", monkeypatch=monkeypatch)
    assert isinstance(result, str)
    assert "must match the pattern" in result
    assert r"^[A-Z]+$" in result


def test_rejection_arm_permits_a_match(monkeypatch):
    result = _validate(_REGEX_SCHEMA, {"host": "WINTERFELL"}, mode="reject", monkeypatch=monkeypatch)
    assert result is None


@pytest.mark.parametrize(
    "mode",
    [None, "", "advisory", "ADVISORY", "true", "1", "yes", "on", "enforce", "strict", "garbage"],
)
def test_anything_that_is_not_the_word_reject_leaves_the_gate_inert(monkeypatch, mode):
    """ISC-4.3 — default is advisory, and any other word falls back to it.

    Covered as a class rather than one case. `true`, `1`, `on`, `enforce` and `strict` are the
    plausible guesses an operator makes when they have not read the docs, and every one of them
    must leave a task-blocking gate switched off rather than silently armed.
    """
    result = _validate(_REGEX_SCHEMA, {"host": "lowercase"}, mode=mode, monkeypatch=monkeypatch)
    assert result is None, f"{mode!r} must not arm the rejection gate"


@pytest.mark.parametrize("mode", ["reject", "Reject", "REJECT", "  reject  ", "Reject "])
def test_the_word_reject_arms_the_gate_regardless_of_case_or_whitespace(monkeypatch, mode):
    """The complement, and the direction that actually carries risk.

    Case and surrounding whitespace are incidental formatting, not a different instruction — a
    value pasted from a config file routinely carries a trailing space. An operator who wrote
    `Reject` and got silence would believe they had enforcement they do not, which is worse than
    the reverse.
    """
    result = _validate(_REGEX_SCHEMA, {"host": "lowercase"}, mode=mode, monkeypatch=monkeypatch)
    assert isinstance(result, str), f"{mode!r} is the word reject and must arm the gate"


def test_arm_uses_unanchored_search_like_mythics_own_ui(monkeypatch):
    """ISC-4.5 — matching semantics must mirror the ONE implementation that exists.

    Mythic's React dialog does `RegExp(verifier_regex).test(value)`, which is an unanchored search.
    Using `re.fullmatch` here would reject values Mythic's own UI accepts, which is precisely the
    class ISC-4.5 forbids: being stricter than the system we are mirroring.
    """
    schema = [
        {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "Default",
         "required": True, "choices": None, "verifier_regex": r"[A-Z]+"},
    ]
    result = _validate(schema, {"host": "prefix-WINTERFELL-suffix"}, mode="reject",
                       monkeypatch=monkeypatch)
    assert result is None, "an unanchored pattern must match a substring, as `.test()` does"


def test_arm_never_fires_on_an_empty_regex(monkeypatch):
    """ISC-4.5 — the field is empty on all 192 live parameters, so this is the case that governs
    every real task today. An armed Sage must still be a no-op on an unpopulated field."""
    schema = [
        {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "Default",
         "required": True, "choices": None, "verifier_regex": ""},
    ]
    assert _validate(schema, {"host": "anything at all"}, mode="reject",
                     monkeypatch=monkeypatch) is None


def test_arm_fails_open_on_an_unusable_pattern(monkeypatch):
    """A regex an agent author typed wrong is not a reason to block an operator's task."""
    schema = [
        {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "Default",
         "required": True, "choices": None, "verifier_regex": "([unclosed"},
    ]
    assert _validate(schema, {"host": "x"}, mode="reject", monkeypatch=monkeypatch) is None


def test_fetch_command_schema_no_longer_builds_interpolated_graphql():
    """ISC-2.2 — the f-string query is what let a malformed query read as an empty answer.

    AST-based so it cannot be tripped by a comment that merely mentions the old pattern.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "mythic_tools.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    node = next(
        n for n in ast.walk(module)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "_fetch_command_schema"
    )
    joined = [c for c in ast.walk(node) if isinstance(c, ast.JoinedStr)]
    assert not joined, "_fetch_command_schema must not build a query by f-string interpolation"

    calls = {
        c.func.attr
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }
    assert "get_command_schema" in calls, "it must delegate the fetch to the shared resolver"
