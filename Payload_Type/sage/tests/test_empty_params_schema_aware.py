"""ISC-65 — an empty parameter blob is only safe for raw-command-line commands.

Mythic task 23 (`ticket_cache_list`, 2026-07-28) was issued by Sage with `params=''` and died with
`failed to parse arguments: string index out of range` — Apollo's parameterized commands parse via
`if self.command_line[0] != "{": raise` with no length check. The operator's identical no-argument
task 24 succeeded because the UI submits a dict and the agent then applies its own declared
`default_value`s.

A first attempt rewrote EVERY empty blob to `{}` and was reverted: for a raw-command-line command
like `shell`, `{}` would be submitted AS the command line. The shipped fix is schema-aware, and
these tests exercise the branch the offline tier cannot — in the tier there is no Mythic client, so
`_fetch_command_schema` fails open and every command keeps `""`.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_circuit_breaker import _make_tools, _split_issue  # noqa: E402

TICKET_SCHEMA = [
    {"name": "luid", "type": "String", "required": False, "default_value": ""},
    {"name": "getSystemTickets", "type": "Boolean", "required": False, "default_value": False},
]


def _issue(mt, command, params):
    seen = {}
    with _split_issue("ok", on_issue=lambda p: seen.__setitem__("parameters", p)):
        asyncio.run(mt.issue_task_and_waitfor_task_output(command, params, 11))
    return seen.get("parameters")


def test_parameterized_command_gets_empty_json_object():
    """The task-23 case: a command that declares parameters must receive `{}`, never ''."""
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, cb: _async(TICKET_SCHEMA)
    for empty in ({}, "", "{}", '""', None):
        assert _issue(mt, "ticket_cache_list", empty) == "{}", f"{empty!r} must become '{{}}'"


def test_raw_command_line_command_keeps_empty_string():
    """The reverted-change hazard: `{}` must NOT be submitted as a shell command line."""
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, cb: _async([])
    assert _issue(mt, "rev2self", "") == "", "a command with no declared parameters keeps ''"


def test_unavailable_schema_fails_open_to_current_behaviour():
    """No client / query error must leave today's behaviour byte-identical."""
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, cb: _async(None)
    assert _issue(mt, "ticket_cache_list", "") == ""


def test_schema_lookup_raising_never_breaks_the_issue_path():
    mt = _make_tools()

    async def boom(command, cb):
        raise RuntimeError("mythic unreachable")

    mt._fetch_command_schema = boom
    assert _issue(mt, "ticket_cache_list", "") == ""


def test_non_empty_parameters_are_never_replaced_by_the_empty_object():
    """The ISC-65 rewrite must fire ONLY on an empty blob.

    With a schema present the resolver legitimately enriches the operator's value with declared
    defaults (`getSystemTickets` arrives from `default_value`), so the assertion is that the
    operator's own value survives to the wire and `{}` never replaces it — not that the blob is
    returned untouched.
    """
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, cb: _async(TICKET_SCHEMA)
    sent = _issue(mt, "ticket_cache_list", '{"luid":"0x5b16c"}')
    assert sent != "{}", "a populated parameter blob must never be blanked to '{}'"
    assert "0x5b16c" in str(sent), "the operator's value must survive to the wire"


async def _coro(value):
    return value


def _async(value):
    return _coro(value)
