"""`execute_capability` for `collect-graph` runs Sage's collection transaction under the one approval.

Graph collection has no capability adapter plan; it is the controller's own transaction. A supervised worker
reaches it through `execute_capability`, so the approved root is that call, and every task, registration and
ingest the transaction makes must be covered by it and nothing else. External effects are replaced exactly as in
`test_private_collection_transaction.py`, whose fixture this module reuses.
"""

from tests.esl_fixtures import seed_ledger  # noqa: E402
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


def test_the_executor_summary_names_the_collection_tasks_and_the_recorded_graph_effect(collection, monkeypatch):
    """The operator-facing executor summary of an approved collection lists the tasks the transaction issued and the
    graph effect it recorded; it never claims "none issued" for a collection that ran (live shape: msg 926)."""
    from langchain_core.messages import ToolMessage

    case = collection
    monkeypatch.setattr(case.client, "_record_graph_built", mt.MythicTools._record_graph_built.__get__(case.client))
    from ai.langgraph import access_reconciler

    async def live_footholds(_client, _now):  # what the access reconciler returns from Mythic live
        return list(case.state.footholds)

    monkeypatch.setattr(access_reconciler, "reconcile_access", live_footholds)
    _approve(case, ACTION)
    raw = asyncio.run(case.client.execute_capability(ACTION, None))
    assert json.loads(raw)["ok"] is True, raw

    payload = model._terminal_execute_capability_payload(
        [ToolMessage(content=raw, name="execute_capability", tool_call_id="call-collect")]
    )
    summary = model._terminal_execute_capability_report(payload)

    assert "none issued" not in summary, summary
    assert "Recorded effects: none" not in summary, summary
    task_line = next(line for line in summary.splitlines() if line.startswith("Task IDs: "))
    for number, command in enumerate(APPROVED_STEPS, start=1):
        assert f"{number} `{command}`" in task_line, summary
    effects_line = next(line for line in summary.splitlines() if line.startswith("Recorded effects: "))
    assert "`graph-built:" in effects_line, summary


def test_the_executor_summary_omits_task_and_effect_lines_a_payload_does_not_report():
    summary = model._terminal_execute_capability_report(
        {"capability": "collect-graph", "verdict": "achieved", "ok": True, "status": "ingested"}
    )
    assert "Task IDs" not in summary and "Recorded effects" not in summary, summary
    explicit = model._terminal_execute_capability_report(
        {"capability": "gpo-controlled-system-exec", "verdict": "achieved", "issued": [], "recorded_effects": []}
    )
    assert "Task IDs: none issued" in explicit and "Recorded effects: none" in explicit, explicit


# --- BloodHound ingest contract, through the real collection path -------------------------------------------
#
# Live evidence (2026-11-03, job 683): the image's MCP rejected every `status` read, the poller treated the
# rejection as pending for twenty reads, no graph-built hop was recorded, the card said "Collection verified",
# and the same collection was proposed again. These regressions run the approved collection through the real
# runner, ingest, proof creation, `_record_graph_built`, persisted ledger, and unchanged scope gate; only the
# external Mythic API and the BloodHound MCP (`FakeIngestTool`, the shipped envelope) are replaced.

LEDGER_KEY = "ingest-contract-regression"


def _real_recorder(case, monkeypatch, tmp_path):
    from ai.langgraph import access_reconciler

    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(mt, "SAGE_ENGAGEMENT_ID", LEDGER_KEY)
    case.client._ledger_binding = None  # the fixture client was built before the namespace was chosen
    case.client._bind_explicit_namespace()
    monkeypatch.setattr(case.client, "_record_graph_built", mt.MythicTools._record_graph_built.__get__(case.client))

    async def live_footholds(_client, _now):  # Mythic's live callbacks, as the access reconciler reports them
        return list(case.state.footholds)

    monkeypatch.setattr(access_reconciler, "reconcile_access", live_footholds)


def _streamed(case):
    messages = []
    case.owner._queue_controller_verbose_event = (
        lambda _text, *, operator_message=None: messages.append(operator_message or "")
    )
    return messages


def _graph_hops(client):
    return [hop for hop in client._engagement_hops if hop.technique == "collect-graph"]


def _reloaded(case):
    """A fresh client that knows only the persisted ledger, plus the same live footholds."""
    fresh = mt.MythicTools()  # binds the same isolated namespace and reads the shared store
    fresh.client = object()
    fresh._engagement_footholds = list(case.state.footholds)
    state = es.EngagementState(
        objective=case.state.objective, footholds=list(case.state.footholds),
        hops=list(fresh._engagement_hops), engagement_id=LEDGER_KEY, runtime_scope=True,
    )
    return fresh, state


def test_a_completed_ingest_persists_one_verified_hop_and_the_unchanged_gate_suppresses_the_same_collection(
    collection, monkeypatch, tmp_path,
):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    case.ingest_tool.statuses = [(6, "Ingesting"), (2, "Complete")]
    _approve(case, ACTION)

    result = _run(case, ACTION)

    assert result["ok"] is True and result["status"] == "ingested", (result, case.events)
    assert result["graph_verified"] is True and result["bloodhound_job_id"] == 41
    assert case.ingest_tool.uploads == 1
    # Controller readiness read, ingest readiness read, then the uploaded job until it completed.
    assert case.ingest_tool.status_reads == [0, 0, 41, 41]
    assert streamed[-1].startswith("**Collection verified**")
    hops = _graph_hops(case.client)
    assert len(hops) == 1 and hops[0].status == "achieved"
    assert hops[0].evidence["graph_verified"] is True
    assert hops[0].evidence["covered_domains"] == ["corp.test"]
    assert "graph-built:" in result["recorded_effects"][0]
    proof = hops[0].proof_envelope
    assert (proof["verifier_id"], proof["ingest_job_id"], proof["ingest_status"]) == (
        "bloodhound_ingest:completed", "41", "complete",
    )
    assert (proof["callback_id"], proof["source_artifact_id"], proof["task_id"]) == ("7", "collection-file-test", "5")
    assert proof["persistence_state"] == "admitted"
    evidence = hops[0].evidence  # the collection record names its collector and source artifact
    assert (evidence["collecting_callback_id"], evidence["source_artifact_id"], evidence["collected_at"]) == (
        "7", "collection-file-test", proof["captured_at"])

    # Persisted and reloaded: the unchanged gate now covers this callback's authority epoch and scope.
    fresh, state = _reloaded(case)
    foothold = case.state.footholds[0]
    assert len(_graph_hops(fresh)) == 1
    assert es.graph_collection_covers_scope(state, foothold, "corp.test") is True
    # The same collection is not selected again by the controller: no target task, no upload.
    owner = object.__new__(model.Model)
    owner.mythic_client = fresh
    owner._turn_authority = None
    owner._controller_collection_adapter = case.owner._controller_collection_adapter
    owner._controller_hitl_enabled = lambda: False
    assert owner._controller_collection_request(state) is None
    uploads_before, tasks_before = case.ingest_tool.uploads, len(case.tasks)
    repeat = asyncio.run(owner._controller_collect(state))
    assert repeat["ok"] is False and repeat["status"] == "no_target"
    assert (case.ingest_tool.uploads, len(case.tasks)) == (uploads_before, tasks_before)
    # The card for an identical proposal says so, and an unapproved collector run is skipped by the gate.
    assert fresh._collection_already_covered("7", "corp.test") is True
    monkeypatch.setenv("SAGE_ENGAGEMENT_GATE", "true")
    same_scope = mt.build_sharphound_arguments(zip_filename="bloodhound_repeat.zip", domain="corp.test")
    blocked = asyncio.run(mt.MythicTools._engagement_issue_hook(
        fresh, "execute_assembly", {"assembly_name": "SharpHound.exe", "assembly_arguments": same_scope}, 7,
    ))
    assert blocked and "graph already built" in blocked, blocked
    # Valid recollection stays possible: another scope, or a changed authority epoch, is not covered.
    assert es.graph_collection_covers_scope(state, foothold, "other.test") is False
    domain_admin = es.Hop(
        id="hop-da", technique="dcsync", target="corp.test", effect="da:corp.test", status="achieved",
        evidence={}, preconditions=[], satisfied_effects=["da:corp.test"], source="test", timestamp="",
    )
    new_epoch = es.EngagementState(
        objective=state.objective, footholds=state.footholds, hops=[*state.hops, domain_admin], runtime_scope=False,
    )
    assert es.collection_target_key(new_epoch, foothold, "corp.test") != es.collection_target_key(
        state, foothold, "corp.test",
    )
    assert es.graph_collection_covers_scope(new_epoch, foothold, "corp.test") is False


def test_the_pinned_base_mcp_stops_collection_before_any_target_task_or_upload(collection, monkeypatch, tmp_path):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    case.ingest_tool.patched = False
    # A compatible host checkout is not the serving MCP: the runtime reads only the connected server.
    checkout = tmp_path / "host-checkout"
    checkout.mkdir()
    (checkout / "main.py").write_text('"status": lambda: get_job_status(job_id),\n')
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", str(checkout))
    _approve(case, ACTION)

    result = _run(case, ACTION)

    assert result["ok"] is False and result["status"] == "error", result
    assert result["graph_verified"] is False
    assert result["ingest_error"]["kind"] == "unsupported_operation"
    assert "Unknown info_type 'status'" in result["reason"] and "rebuild the image" in result["reason"]
    assert case.tasks == [] and case.registered == [], "no collection target task may run"
    assert case.ingest_tool.uploads == 0 and case.ingest_tool.status_reads == [0]
    assert streamed and streamed[-1].startswith("**Collection failed**")
    assert not any("Collection verified" in message for message in streamed)
    assert _graph_hops(case.client) == []


def test_upload_acceptance_and_a_populated_graph_are_not_completion(collection, monkeypatch, tmp_path):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    case.ingest_tool.statuses = [(6, "Ingesting")]
    # BloodHound already lists the domain as collected, and the cached graph says so too.
    seed_ledger(case.client, graph_facts=[es.GraphFact(
        predicate="domain-collected:corp.test", source="bloodhound:domain_info", timestamp="", ttl_seconds=3600,
    )])
    _approve(case, ACTION)

    result = _run(case, ACTION)

    assert result["ok"] is False and result["status"] == "uploaded_pending_ingest", result
    assert result["graph_verified"] is False and result["job_status"] == "Ingesting"
    assert case.ingest_tool.uploads == 1 and case.ingest_tool.status_reads.count(41) == 20
    assert streamed[-1].startswith("**Collection pending**")
    assert _graph_hops(case.client) == []
    content = case.client._collection_already_ingested(b"")  # nothing verified was cached
    assert content[1] is None
    state = es.EngagementState(
        objective=case.state.objective, footholds=list(case.state.footholds), hops=list(case.client._engagement_hops),
        graph_facts=list(case.client._engagement_graph_facts), engagement_id=LEDGER_KEY, runtime_scope=True,
    )
    assert es.graph_collection_covers_scope(state, case.state.footholds[0], "corp.test") is False


@pytest.mark.parametrize("pair", [(3, "Canceled"), (4, "Timed Out"), (5, "Failed"),
                                  (8, "Partially Complete"), (-1, "Invalid")])
def test_a_terminal_ingest_failure_stops_at_once_records_nothing_and_a_fresh_collection_can_still_succeed(
    collection, monkeypatch, tmp_path, pair,
):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    case.ingest_tool.statuses = [pair]
    _approve(case, ACTION)

    failed = _run(case, ACTION)

    assert failed["ok"] is False and failed["status"] == "ingest_failed", failed
    assert failed["graph_verified"] is False and failed["job_status"] == pair[1]
    assert case.ingest_tool.status_reads == [0, 0, 41], "a terminal status is read once"
    assert streamed[-1].startswith("**Collection failed**") and pair[1] in streamed[-1]
    assert _graph_hops(case.client) == []
    assert case.client._collection_in_flight == {}

    # No permanent denial: the scope is still uncovered and nothing verified was cached, so a fresh approved
    # collection runs end to end. (The fake Mythic file registry is reset so it lists the collector once.)
    state = es.EngagementState(
        objective=case.state.objective, footholds=list(case.state.footholds),
        hops=list(case.client._engagement_hops), engagement_id=LEDGER_KEY, runtime_scope=True,
    )
    assert es.graph_collection_covers_scope(state, case.state.footholds[0], "corp.test") is False
    case.registered.clear()
    case.ingest_tool.statuses = [(2, "Complete")]
    _approve(case, ACTION)
    succeeded = _run(case, ACTION)
    assert succeeded["ok"] is True and succeeded["status"] == "ingested", succeeded
    assert len(_graph_hops(case.client)) == 1


@pytest.mark.parametrize("response", [
    # Another job's completion.
    [{"type": "text", "text": json.dumps({"info_type": "status", "data": {
        "job_id": 40, "status": 2, "status_message": "Complete"}})}],
    # A contradictory numeric/text pair.
    [{"type": "text", "text": json.dumps({"info_type": "status", "data": {
        "job_id": 41, "status": 5, "status_message": "Complete"}})}],
    # A top-level error beside completion-shaped data.
    [{"type": "text", "text": json.dumps({"error": "API error: (HTTP 500) boom", "info_type": "status", "data": {
        "job_id": 41, "status": 2, "status_message": "Complete"}})}],
    # Malformed JSON, and more than one content block.
    [{"type": "text", "text": "{\"info_type\": \"status\", "}],
    [{"type": "text", "text": json.dumps({"info_type": "status", "data": {
        "job_id": 41, "status": 2, "status_message": "Complete"}})}] * 2,
])
def test_completion_lookalikes_never_verify_the_collection(collection, monkeypatch, tmp_path, response):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    case.ingest_tool.statuses = [response]
    _approve(case, ACTION)

    result = _run(case, ACTION)

    assert result["ok"] is False and result["graph_verified"] is False, result
    assert result["status"] in {"error", "uploaded_pending_ingest"}
    assert _graph_hops(case.client) == []
    assert not streamed[-1].startswith("**Collection verified**")


def test_refused_collections_never_read_or_upload_to_bloodhound(collection):
    case = collection
    _approve(case, {"capability": "collect-graph", "callback_id": "9"})
    assert _run(case, {"capability": "collect-graph", "callback_id": "9"})["status"] == "no_target"
    _approve(case, ACTION)
    case.client.set_collection_runner(None)
    assert _run(case, ACTION)["status"] == "unavailable"
    with pytest.raises(ValueError):
        _approve(case, ACTION, mode="conversation")
    case.client.clear_approval_claim()
    with pytest.raises(PermissionError):
        _run(case, ACTION)
    assert case.tasks == [] and case.ingest_tool.calls == []
