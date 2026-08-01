"""Sage must declare the parameter group it resolved when that group is not Mythic's default.

Mythic expands `@cred:`/`@link:` task references by looking the command's parameters up **filtered
by parameter group** (`mythic-docker/src/rabbitmq/task_reference.go:167-174`,
`WHERE command_id=$1 AND parameter_group_name=$2`) and assumes `"Default"` when the task declares no
group (`rabbitmq/util_create_task.go:678-681`). The scripting API's `mythic.issue_task` cannot
express a group and omits the key from its variables entirely, so any referenced parameter living
outside `Default` is never registered as reference-bearing: the reference reaches the agent as a
literal string and binds nothing.

Observed as every autonomous GOAD solve halting at `ensure-account-kerberos-context`, with Apollo
reporting `make_token ... Supplied Arguments, [], match more than one parameter group,
['credential_store', 'Default']` — zero arguments bound, because `make_token`'s `credential`
parameter sits in `credential_store`.

The group is sent only when it is neither blank nor `"Default"`. Because the server already
substitutes `"Default"` for a task that declares none, declaring it explicitly is a no-op, so every
other task keeps the exact call it made before this fix — which is what the legacy-path tests below
pin.
"""
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mythic_tools  # noqa: E402
from test_circuit_breaker import _make_tools  # noqa: E402


def _credential_store_schema():
    """Apollo `make_token`: the referenced parameter lives outside the Default group."""
    return [
        {
            "name": "credential",
            "cli_name": "Credential",
            "type": "CredentialJson",
            "parameter_group_name": "credential_store",
            "required": True,
            "choices": [],
            "default_value": None,
        },
        {
            "name": "netOnly",
            "cli_name": "netOnly",
            "type": "Boolean",
            "parameter_group_name": "credential_store",
            "required": False,
            "choices": [],
            "default_value": True,
        },
    ]


def _default_group_schema():
    return [
        {
            "name": "username",
            "cli_name": "username",
            "type": "String",
            "parameter_group_name": "Default",
            "required": True,
            "choices": [],
            "default_value": None,
        },
    ]


@contextmanager
def _record_issue(output="ok"):
    """Record both issue seams: the legacy `issue_task` and the group-declaring createTask post.

    `graphql_post` is a shared upstream seam — callback liveness and credential lookups reach it
    too — so only the createTask mutation is intercepted and everything else is delegated.
    """
    seen = {"legacy": [], "posted": []}
    real_graphql_post = mythic_tools.mythic_utilities.graphql_post

    async def fake_issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        seen["legacy"].append({"command": command_name, "parameters": parameters})
        return {"display_id": 4242}

    async def fake_graphql_post(mythic, gql_query=None, query=None, variables=None):
        if gql_query is not mythic_tools.graphql_queries.create_task:
            return await real_graphql_post(
                mythic=mythic, gql_query=gql_query, query=query, variables=variables
            )
        seen["posted"].append({"gql_query": gql_query, "variables": variables})
        return {"createTask": {"status": "success", "display_id": 4242}}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        return output

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue_task), \
         patch.object(mythic_tools.mythic_utilities, "graphql_post", fake_graphql_post), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", fake_waitfor):
        yield seen


def _issue(mt, command, parameters, schema):
    mt._fetch_command_schema = lambda c, cb: asyncio.sleep(0, result=schema)
    with _record_issue() as seen:
        asyncio.run(mt.issue_task_and_waitfor_task_output(command, parameters, 11))
    return seen


# --- ISC-1.1 / ISC-1.5 -------------------------------------------------------------------------


def test_parameter_group_declared_for_non_default_group():
    seen = _issue(
        _make_tools(), "make_token", {"credential": "@cred:12", "netOnly": True},
        _credential_store_schema(),
    )
    assert len(seen["posted"]) == 1, "a non-Default group must be declared, not assumed"
    assert seen["posted"][0]["variables"]["parameter_group_name"] == "credential_store"
    assert seen["legacy"] == [], "the group-less legacy call must not also fire"


def test_posts_upstream_create_task_mutation():
    """No second query string: the same mutation `issue_task` posts, with the variable it omits."""
    seen = _issue(
        _make_tools(), "make_token", {"credential": "@cred:12", "netOnly": True},
        _credential_store_schema(),
    )
    posted = seen["posted"][0]
    assert posted["gql_query"] is mythic_tools.graphql_queries.create_task
    variables = posted["variables"]
    assert variables["command"] == "make_token"
    assert variables["callback_display_id"] == 11
    assert variables["tasking_location"] == "scripting"
    assert json.loads(variables["params"])["Credential"] == "@cred:12"


# --- ISC-1.2 / ISC-1.3 / ISC-1.6: the untouched path stays untouched ---------------------------


def test_default_group_uses_legacy_issue_path():
    """Mythic already assumes `Default`, so declaring it would change nothing — don't."""
    seen = _issue(_make_tools(), "shell", {"username": "alice"}, _default_group_schema())
    assert seen["posted"] == [], "a Default-group task must not take the new path"
    assert len(seen["legacy"]) == 1


def test_unknown_group_uses_legacy_issue_path():
    seen = _issue(_make_tools(), "shell", {"username": "alice"}, None)
    assert seen["posted"] == []
    assert len(seen["legacy"]) == 1


def test_raw_command_line_parameters_never_acquire_a_group():
    """A string blob is a raw command line; group resolution never runs for it."""
    seen = _issue(_make_tools(), "shell", "whoami /all", _credential_store_schema())
    assert seen["posted"] == []
    assert len(seen["legacy"]) == 1
    assert seen["legacy"][0]["parameters"] == "whoami /all"


# --- the group-selection rule itself ------------------------------------------------------------


def test_only_a_meaningful_group_is_declared():
    declare = mythic_tools.MythicTools._parameter_group_to_declare
    ref = {"Credential": "@cred:12", "netOnly": True}
    assert declare("credential_store", ref) == "credential_store"
    assert declare("  credential_store  ", ref) == "credential_store"
    assert declare("Default", ref) is None, "Mythic's own assumption — declaring it is a no-op"
    assert declare("", ref) is None
    assert declare("   ", ref) is None
    assert declare(None, ref) is None


def test_raw_credential_material_never_declares_a_group():
    """The Merlin case: an agent whose credentials are not in Mythic's store can only be tasked
    with raw material, and a declared group would make Mythic refuse it."""
    declare = mythic_tools.MythicTools._parameter_group_to_declare
    raw = {
        "Credential": {
            "id": "12", "account": "cersei.lannister", "realm": "sevenkingdoms.local",
            "credential": "<secret>", "type": "plaintext",
        },
        "netOnly": True,
    }
    assert declare("credential_store", raw) is None
    assert declare("credential_store", "whoami /all") is None


def test_link_references_and_nested_values_are_recognised():
    declare = mythic_tools.MythicTools._parameter_group_to_declare
    assert declare("p2p", {"connection": "@link:callback=3,c2=smb"}) == "p2p"
    assert declare("p2p", {"connection": "@link:edge=145"}) == "p2p"
    assert declare("g", {"items": ["@cred:9"]}) == "g", "a reference nested in a list still counts"
    assert declare("g", {"note": "email me @cred support"}) is None, "prose is not a reference"


def test_reference_without_a_resolvable_group_is_logged_not_silent(caplog):
    """The one path the fix cannot repair: a reference Mythic will look up under the wrong group."""
    declare = mythic_tools.MythicTools._parameter_group_to_declare
    with caplog.at_level("WARNING"):
        assert declare(None, {"Credential": "@cred:12"}, command="make_token") is None
    assert any("task-reference" in record.getMessage() for record in caplog.records), \
        "a reference that cannot be scoped must not fail silently"


# --- ISC-3: the rejection predicate ------------------------------------------------------------


def test_credential_rejection_matches_real_mythic_text():
    """The literal Mythic emits, from rabbitmq/task_reference_credential.go:55."""
    match = mythic_tools.MythicTools._is_mythic_credential_reference_rejection
    assert match(
        "Failed to process task references: credential parameters require @cred:<id> task references"
    )
    assert match(Exception("credential parameters require @cred:<id> task references"))


def test_credential_rejection_still_matches_historical_text():
    match = mythic_tools.MythicTools._is_mythic_credential_reference_rejection
    assert match("cred parameters require @cred task references")


def test_credential_rejection_ignores_unrelated_failures():
    match = mythic_tools.MythicTools._is_mythic_credential_reference_rejection
    assert not match("[-] failed to parse arguments for rev2self")
    assert not match("Supplied Arguments, [], match more than one parameter group")
    assert not match("credential store is empty")
    assert not match("")
    assert not match(None)
