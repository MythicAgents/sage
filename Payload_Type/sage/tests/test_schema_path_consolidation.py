"""ISC-2.4 and ISC-2.5: live-surface reuse, and no interpolated GraphQL left in the schema path.

Both are anti-criteria — they assert the absence of something — so both are written to be able to
fail for the right reason. The interpolation check walks the AST rather than matching source text,
because an earlier text-based version of this same check tripped on a docstring that merely
described the old pattern, and the tempting fix there is to reword the comment rather than the code.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import command_parameter_schema as schema  # noqa: E402
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402

MYTHIC_TOOLS = Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "mythic_tools.py"

#: Every function that fetches or shapes command-parameter schema. Named explicitly rather than
#: discovered by pattern so that adding a new schema fetcher is a deliberate act that also has to
#: be added here.
SCHEMA_PATH_FUNCTIONS = (
    "get_all_command_names_for_payloadtype",
    "get_all_command_args_for_payloadtype",
    "get_all_commands_for_payloadtype",
    "_fetch_command_metadata",
    "_fetch_command_schema",
    "_fetch_live_command_surface",
)


def _functions(names):
    module = ast.parse(MYTHIC_TOOLS.read_text(encoding="utf-8"))
    found = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in set(names)
    }
    assert set(found) == set(names), (
        f"expected every schema-path function to exist; missing {sorted(set(names) - set(found))}"
    )
    return found


_QUERY_EXECUTORS = ("execute_custom_query", "graphql_post")


def _interpolated_names(node) -> set[str]:
    """Names bound to an f-string or to a `.replace()` result anywhere in the function."""
    tainted: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        value = child.value
        is_interpolated = isinstance(value, ast.JoinedStr) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "replace"
        )
        if not is_interpolated:
            continue
        for target in child.targets:
            if isinstance(target, ast.Name):
                tainted.add(target.id)
    return tainted


def _query_arguments(node):
    """Every expression passed as the query to a GraphQL executor inside this function."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _QUERY_EXECUTORS:
            continue
        for keyword in child.keywords:
            if keyword.arg == "query":
                yield keyword.value
        # execute_custom_query(client, query, ...) — query is the second positional.
        if len(child.args) >= 2:
            yield child.args[1]


@pytest.mark.parametrize("function_name", SCHEMA_PATH_FUNCTIONS)
def test_no_schema_path_query_is_built_by_interpolation(function_name):
    """ISC-2.5 as a class, one case per function, so a failure names the offender.

    Scoped to what is passed AS a query, not to every f-string in the function. An earlier version
    asserted no `JoinedStr` anywhere and failed on `logger.debug(f"...")` and error-return strings,
    which are perfectly legitimate. Widening a guard until it catches innocent code is how guards
    get switched off; the fix is to say precisely what is forbidden.

    The failure being removed is not injection — one of these coerced with `int()` and another
    substituted a payload name. It is that a malformed query returns nothing, which the caller
    cannot distinguish from a real empty answer, so `_validate_command_parameters` logs
    `failed_open ... reason=no_schema` and permits the task.
    """
    node = _functions(SCHEMA_PATH_FUNCTIONS)[function_name]
    tainted = _interpolated_names(node)

    for argument in _query_arguments(node):
        assert not isinstance(argument, ast.JoinedStr), (
            f"{function_name} passes an f-string directly as a GraphQL query"
        )
        if isinstance(argument, ast.Name):
            assert argument.id not in tainted, (
                f"{function_name} passes '{argument.id}' as a query, and it was built by "
                "interpolation or .replace() earlier in the function"
            )


@pytest.mark.parametrize(
    ("source", "should_flag"),
    [
        pytest.param(
            'async def f(self, p):\n'
            '    q = f"query {{ command(where: {{name: {p}}}) }}"\n'
            '    return await mythic.execute_custom_query(self.client, q)\n',
            True,
            id="fstring_assigned_then_passed",
        ),
        pytest.param(
            'async def f(self, p):\n'
            '    q = BASE.replace("PLACEHOLDER", p)\n'
            '    return await mythic.execute_custom_query(self.client, q)\n',
            True,
            id="replace_assigned_then_passed",
        ),
        pytest.param(
            'async def f(self, p):\n'
            '    return await mythic.graphql_post(query=f"query {p}")\n',
            True,
            id="fstring_passed_inline",
        ),
        pytest.param(
            'async def f(self, p):\n'
            '    logger.debug(f"fetching {p}")\n'
            '    q = "query Q($n: String!) { command(where: {name: {_eq: $n}}) { cmd } }"\n'
            '    return await mythic.execute_custom_query(self.client, q, variables={"n": p})\n',
            False,
            id="parameterized_with_logging_fstring",
        ),
    ],
)
def test_the_interpolation_detector_can_actually_fail(source, should_flag):
    """Control for the guard above. A check that has never gone red is a check nobody has tested.

    The last case matters most: it contains an f-string, and the detector must NOT flag it, because
    a guard that fires on `logger.debug(f"...")` gets deleted rather than obeyed.
    """
    node = ast.parse(source).body[0]
    tainted = _interpolated_names(node)
    flagged = any(
        isinstance(argument, ast.JoinedStr)
        or (isinstance(argument, ast.Name) and argument.id in tainted)
        for argument in _query_arguments(node)
    )
    assert flagged is should_flag


def test_no_function_in_mythic_tools_passes_an_interpolated_query():
    """The class, not the six instances. Fixing the named schema-path functions proves nothing
    about the next one someone adds.

    This caught `get_c2_profiles_for_payload`, which was outside the schema path and therefore
    outside ISC-2.5, but carried the identical `query.replace("PLACEHOLDER", ...)` pattern. A guard
    scoped to the functions already fixed would have been green while the defect sat two thousand
    lines away.
    """
    module = ast.parse(MYTHIC_TOOLS.read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    ]
    assert len(functions) > 100, (
        f"only {len(functions)} functions parsed from mythic_tools.py; the guard is not inspecting "
        "the module it thinks it is"
    )

    offenders = []
    for node in functions:
        tainted = _interpolated_names(node)
        for argument in _query_arguments(node):
            if isinstance(argument, ast.JoinedStr) or (
                isinstance(argument, ast.Name) and argument.id in tainted
            ):
                offenders.append(f"{node.name}:{argument.lineno}")
    assert not offenders, "GraphQL queries built by interpolation: " + ", ".join(offenders)


def test_every_schema_path_query_uses_graphql_variables():
    """The complement of the check above: absence of `.replace()` must not be satisfiable by
    deleting the query. Each function that issues one has to declare a GraphQL variable."""
    source = MYTHIC_TOOLS.read_text(encoding="utf-8")
    issuing = (
        "get_all_command_names_for_payloadtype",
        "_fetch_command_metadata",
        "_fetch_live_command_surface",
    )
    for name, node in _functions(issuing).items():
        body = ast.get_source_segment(source, node) or ""
        assert "$" in body and "variables=" in body, (
            f"{name} issues a query but declares no GraphQL variable"
        )


def test_seeded_cache_entries_match_a_direct_fetch_shape():
    """ISC-2.4 — one surface fetch seeds every loaded command's schema.

    The entries must be built through the same field policy `_fetch_command_schema` uses. Before
    this, only the fallback branch seeded and it stored raw rows, so the cached shape depended on
    which branch happened to run first.
    """
    tools = MythicTools.__new__(MythicTools)
    rows = [
        {"name": "domain", "cli_name": "Domain", "type": "String", "required": True,
         "choices": None, "parameter_group_name": "Default", "default_value": "",
         "verifier_regex": "", "description": "dropped by the internal projection",
         "id": 98, "ui_position": 1},
    ]
    tools._seed_command_schema_cache("apollo", [{"cmd": "pth", "commandparameters": rows}])

    entry = tools._cmd_schema_cache[("apollo", "pth")]
    assert entry == schema.flatten_groups(
        schema.group_flat_parameters(
            rows, fields=schema.INTERNAL_SCHEMA_FIELDS, drop_empty=False
        )
    )
    assert sorted(entry[0]) == sorted(schema.INTERNAL_SCHEMA_FIELDS), (
        "the seeded entry must carry exactly the seven keys the internal contract promises"
    )
    assert "description" not in entry[0] and "id" not in entry[0]


def test_seeding_preserves_empty_values():
    """`drop_empty=False` is load-bearing here: a caller doing `param["choices"]` rather than
    `.get()` would start raising if empties vanished from the internal shape."""
    tools = MythicTools.__new__(MythicTools)
    rows = [{"name": "x", "cli_name": "", "type": "String", "required": False,
             "choices": [], "parameter_group_name": "Default", "default_value": ""}]
    tools._seed_command_schema_cache("apollo", [{"cmd": "c", "commandparameters": rows}])

    entry = tools._cmd_schema_cache[("apollo", "c")][0]
    assert entry["choices"] == []
    assert entry["cli_name"] == ""
    assert entry["required"] is False, "an explicit False must survive, not be dropped as empty"


@pytest.mark.parametrize(
    ("payload_type", "commands"),
    [
        pytest.param("", [{"cmd": "x", "commandparameters": []}], id="no_payload_type"),
        pytest.param("apollo", None, id="commands_not_a_list"),
        pytest.param("apollo", ["not a dict"], id="non_dict_entry"),
        pytest.param("apollo", [{"commandparameters": []}], id="command_without_name"),
        pytest.param("apollo", [{"cmd": "x", "commandparameters": "not a list"}], id="bad_rows"),
    ],
)
def test_seeding_never_raises_on_unexpected_shapes(payload_type, commands):
    """Seeding is an optimisation. A failure here must not take down command enumeration, which is
    on the path to every task the agent issues."""
    tools = MythicTools.__new__(MythicTools)
    tools._seed_command_schema_cache(payload_type, commands)
