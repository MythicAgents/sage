"""Every MythicTools public method registered via get_tools() must carry a
@tool_safety decorator. An undecorated method defaults to GUARDED at enforcement
time, which is the safe default — but the CI test catches the omission so the
developer is forced to make a deliberate choice rather than shipping a silent
default.
"""
import inspect

import pytest

from ai.langgraph.mythic_tools import (
    GUARDED_TOOLS,
    TOOL_SAFETY_GUARDED,
    TOOL_SAFETY_READ_ONLY,
    MythicTools,
    _TOOL_SAFETY_ATTR,
    _TOOL_SAFETY_VALUES,
)


def _public_tool_methods() -> list[str]:
    """Return the names of all public methods on MythicTools that are registered
    as LangChain tools via get_tools(). Infrastructure methods (setters, getters,
    lifecycle) are excluded — they're called by the kernel, not by the model."""
    skip = {
        "active_for",
        "apply_scope_gating",
        "authentication_context",
        "begin_operator_turn",
        "bind_artifact",
        "bind_issue",
        "build_capability_execution_plan",
        "clear_approval_claim",
        "contract_progress_snapshot",
        "evaluation_authorization_audit_snapshot",
        "get_tools",
        "ingest_blocker",
        "login",
        "operator_objective_binding",
        "probe_authentication_context",
        "record_capability_result",
        "record_created",
        "require_request_contract",
        "reserve_ingest",
        "set_approval_claim",
        "set_capability_command_observer",
        "set_evaluation_authorization_context",
        "set_execution_observer",
        "set_mechanic_repair_resolver",
        "set_request_contract",
        "set_turn_authority",
        "whoami_scopes",
    }
    results = []
    for name, method in inspect.getmembers(MythicTools, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if name in skip:
            continue
        if not (inspect.iscoroutinefunction(method) or inspect.isfunction(method)):
            continue
        results.append(name)
    return sorted(results)


def test_every_public_method_has_tool_safety_decorator():
    """Fail if a public MythicTools method lacks @tool_safety."""
    missing = []
    for name in _public_tool_methods():
        method = getattr(MythicTools, name)
        if not hasattr(method, _TOOL_SAFETY_ATTR):
            missing.append(name)
    assert not missing, (
        f"These MythicTools methods lack a @tool_safety decorator (they default to "
        f"GUARDED at runtime, but the decorator must be explicit): {missing}"
    )


def test_tool_safety_values_are_valid():
    """No method carries an invalid classification string."""
    invalid = []
    for name in _public_tool_methods():
        method = getattr(MythicTools, name)
        value = getattr(method, _TOOL_SAFETY_ATTR, None)
        if value is not None and value not in _TOOL_SAFETY_VALUES:
            invalid.append((name, value))
    assert not invalid, f"Invalid @tool_safety values: {invalid}"


def test_guarded_tools_set_matches_decorators():
    """GUARDED_TOOLS must agree with @tool_safety(GUARDED) decorators.

    MCP-only entries (file_upload, collect_graph) are allowed in GUARDED_TOOLS
    without a corresponding method — they're gated by name, not by decorator.
    """
    decorated_guarded = set()
    for name in _public_tool_methods():
        method = getattr(MythicTools, name)
        if getattr(method, _TOOL_SAFETY_ATTR, None) == TOOL_SAFETY_GUARDED:
            decorated_guarded.append(name) if False else decorated_guarded.add(name)

    method_names = set(_public_tool_methods())
    guarded_methods = GUARDED_TOOLS & method_names

    assert decorated_guarded == guarded_methods, (
        f"GUARDED_TOOLS and @tool_safety(GUARDED) disagree.\n"
        f"  In GUARDED_TOOLS but not decorated: {guarded_methods - decorated_guarded}\n"
        f"  Decorated GUARDED but not in set: {decorated_guarded - guarded_methods}"
    )


def test_read_only_methods_are_not_in_guarded_tools():
    """A method decorated READ_ONLY must not also appear in GUARDED_TOOLS."""
    conflicts = []
    for name in _public_tool_methods():
        method = getattr(MythicTools, name)
        if (
            getattr(method, _TOOL_SAFETY_ATTR, None) == TOOL_SAFETY_READ_ONLY
            and name in GUARDED_TOOLS
        ):
            conflicts.append(name)
    assert not conflicts, f"Methods marked READ_ONLY but in GUARDED_TOOLS: {conflicts}"
