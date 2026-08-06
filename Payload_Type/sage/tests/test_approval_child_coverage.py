"""Approval coverage must match what a guarded root actually reaches — and nothing more.

A live request deadlocked here. The operator approved `issue_task_and_waitfor_task_output` to run
`execute_assembly` with `SharpHound.exe`; that tool's own registered-file preflight
(`_ensure_registered_file_available`) calls `ensure_tool_uploaded` to register the assembly, and
`_approved_workflow_child` denied it as a `name_mismatch` because the children map never listed
that edge. Registering was denied, retrying was forbidden until registered, and the worker spun on
`list_callbacks` until the graph hit its recursion limit.

The map is one-hop, so it has to carry the transitive closure of the call graph. These tests
recompute that closure from source and assert the map equals it in BOTH directions — a missing
edge deadlocks, an extra edge silently widens operator authority.
"""

import ast
import inspect
from pathlib import Path

import pytest

from ai.langgraph.mythic_tools import GUARDED_TOOLS, MythicTools


# --------------------------------------------------------------------------------------
# The map versus the real call graph.
# --------------------------------------------------------------------------------------


def _declared_children() -> dict[str, set[str]]:
    """The `children` literal as written in `_approved_workflow_child`."""

    src = inspect.cleandoc(inspect.getsource(MythicTools._approved_workflow_child))
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "children" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            return {
                ast.literal_eval(k): set(ast.literal_eval(v))
                for k, v in zip(node.value.keys, node.value.values)
            }
    raise AssertionError("children map not found in _approved_workflow_child")


def _reachable_guarded() -> dict[str, set[str]]:
    """For each guarded tool, the other guarded tools reachable via self.<method> calls."""

    source = Path(inspect.getfile(MythicTools)).read_text()
    cls = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ClassDef) and n.name == "MythicTools"
    )
    methods = {
        n.name: n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    edges = {}
    for name, node in methods.items():
        callees = set()
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "self"
            ):
                callees.add(sub.func.attr)
        edges[name] = callees

    out = {}
    for root in sorted(GUARDED_TOOLS & set(methods)):
        seen, stack, hits = set(), [root], set()
        while stack:
            for callee in edges.get(stack.pop(), ()):
                if callee in GUARDED_TOOLS and callee != root:
                    hits.add(callee)
                if callee in methods and callee not in seen:
                    seen.add(callee)
                    stack.append(callee)
        out[root] = hits
    return out


def test_no_reachable_guarded_effect_is_undeclared():
    """A missing edge is the deadlock. This is the guard that would have caught the live one."""

    reachable = _reachable_guarded()

    # Floor assertion: an audit that walked an empty class would report success on nothing.
    assert len(reachable) >= 8, f"call-graph audit only saw {len(reachable)} guarded tools"
    assert reachable.get("issue_task_and_waitfor_task_output") == {"ensure_tool_uploaded"}, (
        "the audit no longer sees the registered-file preflight; this test proves nothing"
    )

    declared = _declared_children()
    gaps = {
        root: sorted(hits - declared.get(root, set()))
        for root, hits in reachable.items()
        if hits - declared.get(root, set())
    }
    assert gaps == {}, f"reachable guarded effects not covered by an approval: {gaps}"


def test_no_declared_child_is_unreachable():
    """An extra edge widens operator authority with no call path to justify it.

    `collect_graph` is excluded: it is not a method on MythicTools and is admitted through the
    private-collection transaction, not this map.
    """

    reachable = _reachable_guarded()
    extra = {
        root: sorted(kids - reachable.get(root, set()))
        for root, kids in _declared_children().items()
        if root in reachable and kids - reachable.get(root, set())
    }
    assert extra == {}, f"declared approval coverage with no call path: {extra}"


# --------------------------------------------------------------------------------------
# The binding rule that admits the child.
# --------------------------------------------------------------------------------------

_ROOT = {
    "callback_display_id": 1,
    "command": "execute_assembly",
    "parameters": {"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All"},
    "timeout": 300,
}


def test_child_admitted_when_root_names_the_exact_file():
    assert MythicTools._registered_file_named_by_root(
        _ROOT, {"binary_filename": "SharpHound.exe"}
    )


def test_binding_is_case_insensitive_like_the_preflight_key():
    assert MythicTools._registered_file_named_by_root(
        _ROOT, {"binary_filename": "sharphound.exe"}
    )


def test_child_denied_when_root_names_a_different_file():
    """The authority boundary: approving SharpHound must not license uploading Rubeus."""

    assert not MythicTools._registered_file_named_by_root(
        _ROOT, {"binary_filename": "Rubeus.exe"}
    )


def test_child_denied_when_root_carries_no_parameters():
    assert not MythicTools._registered_file_named_by_root(
        {"callback_display_id": 1, "command": "shell"}, {"binary_filename": "SharpHound.exe"}
    )


def test_child_denied_when_filename_missing_or_blank():
    assert not MythicTools._registered_file_named_by_root(_ROOT, {})
    assert not MythicTools._registered_file_named_by_root(_ROOT, {"binary_filename": "   "})


def test_json_string_parameters_are_parsed_not_substring_matched():
    """Parameters arrive as a JSON string on some paths; a value match must still be exact."""

    root = dict(_ROOT, parameters='{"assembly_name": "SharpHound.exe"}')
    assert MythicTools._registered_file_named_by_root(root, {"binary_filename": "SharpHound.exe"})
    # 'Hound.exe' is a substring of the JSON blob but not a parameter value.
    assert not MythicTools._registered_file_named_by_root(root, {"binary_filename": "Hound.exe"})


@pytest.mark.parametrize("unrelated", ["add_credential", "create_payload", "delete_payload"])
def test_unrelated_guarded_tools_are_not_children_of_a_task_approval(unrelated):
    """Approving one task must never license an unrelated guarded effect."""

    assert unrelated not in _declared_children().get(
        "issue_task_and_waitfor_task_output", set()
    )
