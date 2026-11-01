"""`execute_capability` for `collect-graph` runs Sage's collection transaction under the one approval.

Graph collection has no capability adapter plan; it is the controller's own transaction. A supervised worker
reaches it through `execute_capability`, so the approved root is that call, and every task, registration and
ingest the transaction makes must be covered by it and nothing else. External effects are replaced exactly as in
`test_private_collection_transaction.py`, whose fixture this module reuses.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_state as es, model, mythic_tools as mt
from ai.langgraph.request_contract import action_spec_from_tool_call, build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from sage_chat.hitl import (
    approval_action_digest, approval_action_fingerprint,
    approval_proposal_digest, approval_selection_digest,
)
from tests.test_private_collection_transaction import collection  # noqa: F401  (fixture)

# The `execute_capability` call a live supervised worker made (2026-10-31, channel 8, OpenRouter Sonnet 4.6),
# recovered from the Phoenix trace: `action` and `inputs` as JSON strings, a string callback id, and inputs the
# collection procedure does not use.
LIVE_ARGS = {
    "action": '{"capability": "collect-graph", "domain": "north.sevenkingdoms.local", "callback_id": "3"}',
    "inputs": json.dumps({"output_directory": "C:\\Users\\Public", "zip_filename": "sysreport.zip",
                          "collection_methods": "All"}),
}

APPROVED_STEPS = ["whoami", "ticket_cache_list", "execute_assembly", "ls", "download"]


def _approve(case, action, inputs=None, *, mode="supervised"):
    """Replace the fixture's approval with an approved `execute_capability` call."""
    root = {"name": "execute_capability", "args": {"action": action, "inputs": inputs}}
    contract = build_request_contract(
        request_id="request-capability-collection", channel_id="channel-test", operation_id="operation-test",
        mode=mode, autonomous_solve=False, requested_actions=(action_spec_from_tool_call(root),),
    )
    digest = approval_action_digest([root])
    ids = [approval_action_fingerprint(root)]
    claim = {
        "approval_id": "approved-capability-collection", "request_id": contract.request_id,
        "request_contract_digest": contract.digest, "tool_name": "execute_capability",
        "selection_mode": "single", "actions": [root], "approved_actions": [root],
        "approved_action_ids": ids, "action_digest": digest,
        "proposal_digest": approval_proposal_digest(contract.digest, digest),
        "selection_digest": approval_selection_digest(contract.digest, digest, ids),
    }
    client = case.client
    client._private_collection_transaction = None
    client.set_request_contract(contract)
    client.set_turn_authority(authority_from_request_contract(contract))
    client.set_approval_claim(claim)
    case.owner._turn_authority = client._turn_authority

    async def current_state():
        return case.state

    case.owner._build_current_engagement_state = current_state
    client.set_collection_runner(case.owner._run_collection_capability)
    return root


def _run(case, action, inputs=None):
    return json.loads(asyncio.run(case.client.execute_capability(action, inputs)))


ACTION = {"capability": "collect-graph", "callback_id": "7", "domain": "corp.test"}


def test_approved_collection_capability_runs_the_whole_transaction_under_one_approval(collection):
    case = collection
    _approve(case, ACTION)
    result = _run(case, ACTION)

    assert result["ok"] is True and result["status"] == "ingested", (result, case.events)
    assert result["capability"] == "collect-graph"
    assert [task["command_name"] for task in case.tasks] == APPROVED_STEPS
    assert {task["callback_display_id"] for task in case.tasks} == {7}
    assert case.registered == ["SharpHound.exe"]
    assert [event for event in case.events if event[0] == "ingest"] == [("ingest", "upload_bytes"),
                                                                        ("ingest", "status")]
    tx = case.client._private_collection_transaction
    assert (tx.approved_name, tx.callback_id, tx.scope_domain) == ("execute_capability", "7", "corp.test")
    assert tx._artifact_consumed is True


def test_collection_scope_comes_from_the_approval_or_defaults_to_the_foothold_forest(collection):
    case = collection
    action = {"capability": "collect-graph", "callback_id": "7"}
    _approve(case, action)
    result = _run(case, action)
    assert result["ok"] is True, result
    assert case.client._private_collection_transaction.scope_domain == ""


def test_a_callback_without_a_collection_foothold_issues_nothing(collection):
    case = collection
    action = {"capability": "collect-graph", "callback_id": "9"}
    _approve(case, action)
    result = _run(case, action)
    assert (result["ok"], result["status"]) == (False, "no_target")
    assert case.tasks == [] and case.registered == []


def test_without_a_runner_the_capability_reports_it_and_issues_nothing(collection):
    case = collection
    _approve(case, ACTION)
    case.client.set_collection_runner(None)
    result = _run(case, ACTION)
    assert (result["ok"], result["status"]) == (False, "unavailable")
    assert case.tasks == []


def test_a_conversation_request_cannot_run_the_collection_capability(collection):
    case = collection
    # A conversation request cannot hold the approval at all, and without one the capability is denied.
    with pytest.raises(ValueError, match="conversational lane denies external actions"):
        _approve(case, ACTION, mode="conversation")
    case.client.clear_approval_claim()
    with pytest.raises(PermissionError):
        _run(case, ACTION)
    assert case.tasks == []


@pytest.mark.parametrize("field, value", [
    ("callback_id", "8"),        # the derived collection names another callback than the approval
    ("scope_domain", "other.test"),  # or another domain
    ("host", "elsewhere"),       # or a foothold record that is not the approved callback's
])
def test_the_transaction_starts_only_for_the_exact_approved_callback_and_scope(collection, field, value):
    case = collection
    _approve(case, ACTION)
    request = model._ControllerCollectionRequest(
        foothold=case.state.footholds[0], scope_domain="corp.test", reason="operator-approved",
        collection_key="corp.test|operator", support="approved",
    )
    root_args = {
        "collection_key": request.collection_key, "scope_domain": request.scope_domain,
        "reason": request.reason, "support": request.support, "callback_id": "7",
        "host": "workstation", "agent": "apollo", "identity": "corp\\operator",
    }
    assert case.client._begin_private_collection_transaction(root_args, request=request, adapter={}) == ""
    case.client._private_collection_transaction = None
    changed = {**root_args, field: value}
    blocker = case.client._begin_private_collection_transaction(changed, request=request, adapter={})
    assert "does not match the exact active approval" in blocker


@pytest.mark.parametrize("callback_id, scope_domain", [("8", "corp.test"), ("7", "other.test"), ("7", "")])
def test_a_self_consistent_collection_for_another_callback_or_scope_is_refused(
    collection, callback_id, scope_domain,
):
    """The request and root agree with each other; only the approval says otherwise."""
    case = collection
    _approve(case, ACTION)
    foothold = es.Foothold(
        callback_id=callback_id, host="workstation", agent="apollo", identity="corp\\operator",
        forest="corp.test", alive=True, integrity="medium", source="offline-fixture", timestamp="",
    )
    request = model._ControllerCollectionRequest(
        foothold=foothold, scope_domain=scope_domain, reason="operator-approved",
        collection_key="corp.test|operator", support="approved",
    )
    root_args = {
        "collection_key": request.collection_key, "scope_domain": scope_domain, "reason": request.reason,
        "support": request.support, "callback_id": callback_id, "host": "workstation", "agent": "apollo",
        "identity": "corp\\operator",
    }
    blocker = case.client._begin_private_collection_transaction(root_args, request=request, adapter={})
    assert "does not match the exact active approval" in blocker
    assert case.client._private_collection_transaction is None


def test_a_collection_root_admits_children_only_through_its_transaction(collection):
    case = collection
    root = _approve(case, ACTION)
    child = {"command": "whoami", "parameters": "", "callback_display_id": 7, "timeout": 300}
    provenance = {"capability": "collect-graph"}
    # No transaction is open, and a collection root never falls back to the generic capability child map.
    assert not case.client._approved_workflow_child(
        "execute_capability", root["args"], "issue_task_and_waitfor_task_output", child, provenance,
    )
    assert not case.client._approved_workflow_child(
        "execute_capability", root["args"], "ingest_collection", {"file_uuid": "x"}, provenance,
    )


def test_another_capability_root_never_admits_an_ingest(collection):
    case = collection
    other = {"capability": "dcsync-krbtgt", "callback_id": "7", "domain": "corp.test"}
    root = _approve(case, other)
    assert mt.collection_capability_binding(root) is None
    assert not case.client._approved_workflow_child(
        "execute_capability", root["args"], "ingest_collection", {"file_uuid": "x"}, {},
    )


def test_a_completed_collection_cannot_ingest_again_under_the_same_approval(collection):
    case = collection
    _approve(case, ACTION)
    assert _run(case, ACTION)["ok"] is True
    tx = case.client._private_collection_transaction
    assert tx.reserve_ingest({"file_uuid": "collection-file-test"}, {"capability": "collect-graph"})


@pytest.mark.parametrize("root, expected", [
    ({"name": "execute_capability", "args": {"action": {"capability": "collect-graph", "callback_id": "7"}}},
     {"callback_id": "7", "scope_domain": ""}),
    ({"name": "execute_capability", "args": {"action": '{"name": "collect-graph", "callback_id": "3"}',
                                             "inputs": {"scope_domain": "North.Test"}}},
     {"callback_id": "3", "scope_domain": "north.test"}),
    ({"name": "execute_capability", "args": {"action": {"capability": "collect-graph"}}},
     {"callback_id": "", "scope_domain": ""}),
    ({"name": "execute_capability", "args": LIVE_ARGS},
     {"callback_id": "3", "scope_domain": "north.sevenkingdoms.local"}),
    ({"name": "execute_capability", "args": {"action": {"capability": "forge-golden-ticket"}}}, None),
    ({"name": "collect_graph", "args": {"callback_id": "7"}}, None),
    ({"name": "execute_capability", "args": "not an object"}, None),
])
def test_collection_binding_reads_only_an_execute_capability_root_for_collect_graph(root, expected):
    assert mt.collection_capability_binding(root) == expected


def _card_plan(args) -> str:
    owner = SimpleNamespace(mythic_client=mt.MythicTools(agent_task_id="test"))
    return model.Model._capability_card_plan_text(owner, args)


def test_the_approval_card_plans_the_live_collection_call_instead_of_refusing_it():
    # The HITL pre-check refuses any execute_capability card whose plan text says "could not be built". The live
    # call was refused there, before the operator saw a card, because collection has no adapter plan.
    text = _card_plan(LIVE_ARGS)

    assert "could not be built" not in text
    assert "callback 3" in text and "north.sevenkingdoms.local" in text
    assert "BloodHound" in text


def test_the_approval_card_still_refuses_a_collection_call_without_a_callback():
    text = _card_plan({"action": '{"capability": "collect-graph", "domain": "north.sevenkingdoms.local"}'})

    assert "could not be built" in text and "callback_id" in text


def test_the_approval_card_still_refuses_a_capability_with_no_plan_that_is_not_collection():
    text = _card_plan({"action": '{"capability": "collect-graphs", "callback_id": "3"}'})

    assert "could not be built" in text


def _covered(case, monkeypatch):
    """Turn the engagement gate on with a verified graph already covering every collection key."""
    monkeypatch.setenv("SAGE_ENGAGEMENT_GATE", "true")
    monkeypatch.setattr(es, "graph_collection_covers_scope", lambda *a, **k: True)

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(case.client, "_collection_in_flight_blocker", none)
    from ai.langgraph import access_reconciler

    async def footholds(*_a, **_k):
        return list(case.state.footholds)

    monkeypatch.setattr(access_reconciler, "reconcile_access", footholds)
    gate_decisions: list = []
    original = case.client._engagement_issue_hook

    async def recorded(command, parameters, callback_display_id):
        decision = await original(command, parameters, callback_display_id)
        gate_decisions.append((command, decision))
        return decision

    monkeypatch.setattr(case.client, "_engagement_issue_hook", recorded)
    return gate_decisions


def test_an_approved_collection_re_collects_a_scope_already_collected(collection, monkeypatch):
    """Rights changed outside Sage leave the key unchanged, so the operator's approval is what re-collects."""
    case = collection
    _covered(case, monkeypatch)
    _approve(case, ACTION)
    covered_key = es.collection_target_key(case.state, case.state.footholds[0], "corp.test")
    result = _run(case, ACTION)

    assert covered_key  # the gate could key this collection, so its covered branch was reached
    assert result["ok"] is True and result["status"] == "ingested", (result, case.events)
    assert "execute_assembly" in [task["command_name"] for task in case.tasks]


def test_without_that_approval_a_covered_collection_is_still_skipped(collection, monkeypatch):
    case = collection
    _covered(case, monkeypatch)
    _approve(case, ACTION)
    case.client._private_collection_transaction = None  # no approved collection is running
    from ai.langgraph.mythic_tools import MythicTools
    blocked = asyncio.run(MythicTools._engagement_issue_hook(case.client, 
        "execute_assembly", {"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All"}, 7))

    assert blocked and "graph already built" in blocked


@pytest.mark.parametrize("covered", [True, False])
def test_the_approval_card_says_when_the_collection_was_already_made(monkeypatch, covered):
    client = mt.MythicTools(agent_task_id="test")
    monkeypatch.setattr(client, "_collection_already_covered", lambda callback, scope: covered)
    text = model.Model._capability_card_plan_text(SimpleNamespace(mythic_client=client), LIVE_ARGS)

    assert ("already have a verified graph" in text) is covered and "could not be built" not in text


def test_coverage_for_the_card_comes_from_the_cached_engagement_state(collection, monkeypatch):
    case = collection
    monkeypatch.setattr(es, "graph_collection_covers_scope", lambda *a, **k: True)
    assert not case.client._collection_already_covered("7", "corp.test")  # nothing cached yet: no claim
    case.client._engagement_footholds = list(case.state.footholds)
    assert case.client._collection_already_covered("7", "corp.test")
    assert not case.client._collection_already_covered("8", "corp.test")
