"""Recorded input shapes at the real native consumer, with synthetic state.

Reuse the catalog fixture's real producer, graph, brief admission and card driver.
The model output, current ledger/callback state and external transports are inert;
this checks code given a recorded shape, not model choice or a full incident replay.
No approval is submitted. The inherited setup seeds a proved key using inert task
output; zero-task assertions below concern the proposal turn after that setup.
A catalog step's targets are recorded annotations of its candidate: any form,
including a host the candidate does not name, cards that candidate's action once.

The run 2/3 corpus (`catalog_brief_targets.json`, plus one run 4 row and two rows each of runs 5 and 6)
replays each recorded target shape of a non-collection catalog brief against a real
candidate of the same procedure. A brief that selected a candidate but listed two
procedures cards that candidate's procedure alone. The run 5 and run 6 rows' unused references
are replayed in test_supervised_catalog_step_binding.py.
Its refs, callback and references are mapped to the fixture's (references are not
replayed); collection and raw rows are inventoried and their target checks are
tested in test_supervised_catalog_step_binding.py.
"""

import asyncio
from collections import Counter
import json
from pathlib import Path
import socket

import pytest

from tests.esl_fixtures import seed_ledger
from tests.test_supervised_catalog_step_binding import NOW, _brief_targets, _client, _run, _select, _state_for


FIXTURES = Path(__file__).parent / "fixtures/model_input_boundaries"
CORPUS = json.loads((FIXTURES / "handoff_targets.json").read_text())
ROWS = json.loads((FIXTURES / "catalog_brief_targets.json").read_text())["rows"]
CATALOG_ROWS = [row for row in ROWS if row["candidate_ref"] and "collect-graph" not in row["allowed_capabilities"]]
# One replay per distinct recorded (procedures, targets) shape, named by its first row.
SHAPES = list({json.dumps([row["allowed_capabilities"], row["targets"]]): row for row in reversed(CATALOG_ROWS)}
              .values())[::-1]


@pytest.fixture
def no_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("connection attempted")
        raise AssertionError("model-input boundary test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    return attempts


@pytest.mark.parametrize("case", CORPUS["cases"], ids=[case["id"] for case in CORPUS["cases"]])
def test_handoff_targets_reach_native_consumer(monkeypatch, tmp_path, no_network, case):
    shape = CORPUS["recorded_shape"]
    assert isinstance(shape["callback_display_id"], str)
    assert isinstance(case["targets"], list) and len(case["targets"]) == 2
    assert all(isinstance(target, str) for target in case["targets"])
    assert case["provenance"] and CORPUS["provenance"]["kind"]
    if case["id"] == "equivalent_fqdn":
        assert case["targets"] == shape["targets"]
        assert shape["targets"][0].endswith("." + shape["targets"][1])

    mt, split = _client(monkeypatch, tmp_path)
    if case.get("synthetic_graph_facts"):
        seed_ledger(mt, graph_facts=[{"predicate": predicate, "source": "fixture", "timestamp": NOW,
                                      "ttl_seconds": 86400} for predicate in case["synthetic_graph_facts"]])
    run = asyncio.run(_run(mt, split, [_select(
        shape["allowed_capabilities"][0], "alpha-policy",
        allowed_capabilities=shape["allowed_capabilities"],
        callback_display_id=shape["callback_display_id"], targets=case["targets"],
    )], decision=None))

    assert no_network == []
    assert run.issued == [] and run.before.issued == []
    assert run.before.worker_calls == 0
    assert run.outcomes == []
    assert len(run.cards) == case["expected_cards"] == 1, (case["id"], run.briefs)
    assert run.briefs == [("accepted", "Mythic_Operator")] and _brief_targets(run) == case["targets"]
    arguments = run.cards[0]["data"]["arguments"]
    assert arguments["action"]["name"] == shape["allowed_capabilities"][0]
    assert arguments["action"]["intent"]["gpo"] == "alpha-policy"
    assert arguments["inputs"]["callback_id"] == shape["callback_display_id"]


def test_recorded_catalog_briefs_are_inventoried():
    assert len(ROWS) == 47 and sum(row["candidate_ref"] for row in ROWS) == 35
    assert Counter(row["recorded_refusal"] for row in ROWS) == {
        "none": 26, "brief-target": 6, "binder-target": 5, "reference": 3, "candidate-on-collection": 1, "repeat": 1,
        "binder-procedure": 1, "unused-reference": 2, "pinned-reference": 2}
    kinds = Counter("collection" if "collect-graph" in row["allowed_capabilities"] else
                    "catalog" if row["allowed_capabilities"] else "raw" for row in ROWS)
    assert kinds == {"catalog": 34, "collection": 5, "raw": 8} and len(CATALOG_ROWS) == 34
    # Every recorded target refusal was a catalog brief's; each is replayed below.
    assert {row["id"] for row in ROWS if row["recorded_refusal"].endswith("-target")} <= {
        row["id"] for row in CATALOG_ROWS}
    assert {json.dumps([row["allowed_capabilities"], row["targets"]]) for row in CATALOG_ROWS} == {
        json.dumps([row["allowed_capabilities"], row["targets"]]) for row in SHAPES}


@pytest.mark.parametrize("row", SHAPES, ids=[row["id"] for row in SHAPES])
def test_recorded_catalog_target_shapes_card_their_candidate(monkeypatch, tmp_path, no_network, row):
    procedure = row["allowed_capabilities"][0]
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(_state_for(mt, procedure), split, [_select(
        procedure, "alpha-policy" if procedure.startswith("gpo-") else "",
        allowed_capabilities=row["allowed_capabilities"], targets=row["targets"])], decision=None))

    assert no_network == [] and run.issued == [] and run.before.worker_calls == 0
    # A brief listing the selected candidate's procedure with the one it feeds binds that candidate alone.
    assert run.briefs == [("accepted", "Mythic_Operator")] and _brief_targets(run) == row["targets"]
    [card] = run.cards
    assert card["data"]["arguments"]["action"]["name"] == procedure
    assert run.terminals == []


REASK = json.loads((FIXTURES / "approval_reask_cards.json").read_text())


_OFFER_ROWS = [row for row in REASK["rows"] if row["kind"] in {"legacy", "down", "queued"}]
_LIFECYCLE_ROWS = [row for row in REASK["rows"] if row["kind"] == "lifecycle"]


@pytest.mark.parametrize("row", _OFFER_ROWS, ids=[row["id"] for row in _OFFER_ROWS])
def test_recorded_decided_card_is_offered_once_never_reasked_or_discarded(monkeypatch, tmp_path, no_network, row):
    """A recorded decided card reaches the real on-start reconciler, resolver, checkpoint reader and validator.

    Expected outcome changed from plan r2's re-ask by the operator's 2026-11-20 recovery decision
    (reason in each row): startup offers Continue/Stop and keeps the original approval valid; a
    record saved without its request state is closed with nothing run.
    """
    import copy
    from types import SimpleNamespace

    import aiosqlite
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.types import Interrupt
    from mythic_container.ChatBase import ChatRequest
    from mythic_container.SharedClasses import ContainerOnStartMessage

    import sage_chat.restoration as restoration
    import sage_chat.service as service
    from ai.langgraph.sqlite_checkpointer import SageAsyncSqliteSaver
    from sage_chat import session as chat_session
    from sage_chat.headless import HeadlessSageChat
    from sage_chat.hitl import approval_response_matches

    assert REASK["provenance"]["kind"] and row["expected"]["reason"]
    db_path = str(tmp_path / "sage.db")
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", db_path)
    checkpoint = row["checkpoint"]
    record = row["channel_record"]
    operation, channel = record["operation_id"], record["channel_id"]
    request_row = row["request_row"]
    controls: list[dict] = []

    class Mythic:
        async def execute(self, query, variables):
            if "sageOpenRequests" in query:
                assert variables["op"] == operation and variables["container"] == "sage"
                return {"chat_request": [request_row]}
            if "sageRestoredCard" in query:
                ((data_key, identity),) = variables["ctx"]["input_requested"]["data"].items()
                ((id_key, wanted),) = identity.items()
                rows = [row["card_row"], *controls]
                return {"chat_message": [
                    r for r in rows
                    if (r["metadata"]["input_requested"].get("data") or {}).get(data_key, {}).get(id_key) == wanted
                ]}
            raise AssertionError(query)

    mythic = Mythic()

    async def execute_custom_query(client, query, variables=None):
        return await client.execute(query, dict(variables or {}))

    monkeypatch.setattr(restoration.mythic, "execute_custom_query", execute_custom_query)

    class Watcher:
        async def restore_operation(self, *args, **kwargs):
            return None

    class Chat(HeadlessSageChat):
        async def _onstart_client(self, message):
            return mythic

        async def _mythic_read_client(self, request, model=None):
            return mythic

        async def _construct_model(self, kwargs):
            raise AssertionError("reconciliation must not build a Model")

    action_requests = checkpoint["interrupt_value"]["action_requests"]

    async def scenario():
        async with aiosqlite.connect(db_path) as conn:
            saver = SageAsyncSqliteSaver(conn)
            config = await saver.aput(
                {"configurable": {"thread_id": checkpoint["thread_id"], "checkpoint_ns": ""}},
                empty_checkpoint(), {"source": "loop", "step": 1, "parents": {}}, {},
            )
            await saver.aput_writes(
                config,
                [("__interrupt__", [Interrupt(value=checkpoint["interrupt_value"], id=checkpoint["interrupt_id"])])],
                checkpoint["task_id"],
            )
        await chat_session.save_channel_session_record(record)
        chat = Chat()
        chat._findings_watcher = lambda: Watcher()
        response = await chat.on_container_start(ContainerOnStartMessage(
            container_name="sage", operation_id=operation, server_name="mythic", apitoken="onstart-token"))
        saved = await chat_session.load_channel_session_record(operation, channel)
        outcomes = {}
        if row["kind"] != "legacy":
            restored, _contract, reason = restoration.restored_pending_context(
                saved["pending"], action_requests, checkpoint["thread_id"])
            assert restored is not None, reason
            model = SimpleNamespace(_thread_id_override=checkpoint["thread_id"],
                                    _pending_approval_context=restored)
            control = next(e["metadata"]["input_requested"] for e in chat.emissions
                           if (e.get("metadata") or {}).get("special_type") == "input_requested")
            controls.append({"id": 95, "channel_id": channel, "operation_id": operation,
                             "metadata": {"special_type": "input_requested", "input_requested": control}})
            if row["kind"] == "queued":
                # Mythic's recorded dispatch of the decision queued while Sage booted.
                delivered = ChatRequest(container_name="sage", operation_id=operation, channel_id=channel,
                                        request_id=request_row["id"], input_response=row["dispatch"])
                outcomes["delivered"] = await chat._resolve_saved_decision(delivered, model)
                outcomes["delivered_matches"] = approval_response_matches(delivered, restored)
            from tests.test_chat_session_restoration import _control_response

            answer = ChatRequest(container_name="sage", operation_id=operation, channel_id=channel,
                                 request_id=request_row["id"])
            answer.InputResponse = _control_response(control, "continue")
            controls[0]["metadata"]["input_requested"] = copy.deepcopy(answer.InputResponse.InputRequest)
            outcomes["continue"] = await chat._resolve_saved_decision(answer, model)
            outcomes["continue_matches"] = approval_response_matches(answer, restored)
            outcomes["continued_envelope"] = answer.InputResponse
        return chat, response, saved, outcomes

    try:
        chat, response, saved, outcomes = asyncio.run(scenario())
    finally:
        service._CHANNEL_TURN_LOCKS.clear()

    assert no_network == [] and response.EventLogErrorMessage == ""
    cards = [e for e in chat.emissions
             if (e.get("metadata") or {}).get("special_type") == "input_requested"]
    closings = [e for e in chat.emissions if e.get("complete_request")]
    if row["kind"] == "legacy":
        assert not cards and len(closings) == 1
        assert "without its request state" in closings[0]["content"]
        assert saved["pending"] is None
        return
    assert not closings, closings
    assert len(cards) == 1 and "sage_continuation" in cards[0]["metadata"]["input_requested"]["data"]
    # Never plan r2's re-ask: no new approval card for the action, no "decide again" notice.
    assert "sage_approval_context" not in cards[0]["metadata"]["input_requested"]["data"]
    assert not any("Decide on the new card" in str(e.get("content") or "") for e in chat.emissions)
    source = row["expected"]["source_approval_id"]
    assert saved["pending"]["context"]["approval_id"] == source
    assert saved["continuation"]["source_approval_id"] == source and saved["continuation"]["status"] == "offered"
    assert saved["claim"] is None
    if row["kind"] == "queued":
        assert outcomes["delivered"][0] == "superseded", outcomes["delivered"]
        assert outcomes["delivered_matches"] is True
        recorded = restoration.input_response_from_card_row(row["card_row"])
        dispatched = ChatRequest(input_response=row["dispatch"]).InputResponse
        assert restoration.saved_decision_digest(recorded) == restoration.saved_decision_digest(dispatched)
    assert outcomes["continue"][0] == "continue", outcomes["continue"]
    assert outcomes["continue_matches"] is True
    assert outcomes["continued_envelope"].Action == row["expected"]["action"]


@pytest.mark.parametrize("row", _LIFECYCLE_ROWS, ids=[row["id"] for row in _LIFECYCLE_ROWS])
def test_recorded_lifecycle_identities_reconcile_only_when_restored(row):
    """Request 22's recorded pre-card projections (Mythic rows 439/440) through the real reconciliation.

    The resume's own events reuse those recorded identities (row 440 was completed in place live).
    Without the saved request state they are unmatched: the recorded `tool started count=0`.
    """
    import hashlib

    from ai.langgraph.request_events import RequestEventLedger, exported_content_digest

    request_id = row["request_id"]

    def synthetic_record_id(*parts):
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    events = [
        ("operator_input", "operator_input:recorded", "received", exported_content_digest("prompt"), False, ""),
        ("control_transition", "control_transition:installed", "request_installed",
         "request contract installed", False, ""),
    ]
    for item in row["pre_card_projections"]:
        phase = "opened" if item["kind"] == "delegation" else "started"
        events.append((item["kind"], item["event_id"], phase, exported_content_digest(""), False, ""))
        events.append((item["kind"], item["event_id"], phase, exported_content_digest(""), True, item["response_key"]))
    export = {"version": 1, "request_id": request_id, "events": [
        {"record_id": synthetic_record_id(str(i), *map(str, event)), "kind": event[0], "event_id": event[1],
         "phase": event[2], "content": event[3], "projected": event[4], "projection_key": event[5], "metadata": {}}
        for i, event in enumerate(events)
    ]}

    def resume(ledger):
        for item in row["pre_card_projections"]:
            phase = "finished" if item["kind"] == "delegation" else "completed"
            ledger.record_once(event_id=item["event_id"], kind=item["kind"], phase=phase)
            ledger.record_projection(event_id=item["event_id"], kind=item["kind"], phase=phase,
                                     projection_key=item["response_key"])
        ledger.record_once(event_id="control_transition:terminal", kind="control_transition",
                           phase="request_terminal", content="complete")
        ledger.record_once(event_id="final_response:terminal", kind="final_response", phase="emitted")
        ledger.record_projection(event_id="final_response:terminal", kind="final_response",
                                 phase="emitted", projection_key="assistant:22:turn")
        return ledger.reconcile()

    restored = resume(RequestEventLedger.from_export(export))
    assert restored["ok"], restored
    fresh = RequestEventLedger(request_id)
    fresh.record(event_id="operator_input:recorded", kind="operator_input", phase="received")
    fresh.record(event_id="control_transition:installed", kind="control_transition",
                 phase="request_installed", content="request contract installed")
    lost = resume(fresh)
    assert not lost["ok"]
    assert any(row["expected"]["recorded_error"] in error for error in lost["errors"]), lost["errors"]


APPROVED_ERRORS = json.loads((FIXTURES / "approved_call_errors.json").read_text())


@pytest.mark.parametrize("row", APPROVED_ERRORS["rows"], ids=[row["id"] for row in APPROVED_ERRORS["rows"]])
def test_recorded_approved_call_error_closes_its_own_card(no_network, row):
    """The recorded accepted call that failed its schema after the resume, through the assembled native chat.

    The recorded run (Mythic rows 58/61/62) projected a second, terminal-only `unknown_tool` card for the same call
    id and delegation, and reconciliation failed the request. Replayed, the call's own card closes with the error.
    """
    from tests.test_resumed_tool_card_identity import _assembled_run, _assert_own_card_closed

    recorded = row["recorded"]
    started, terminal_only = recorded["started_card"], recorded["terminal_only_card"]
    assert started["tool_call_id"] == terminal_only["tool_call_id"] == row["call"]["id"]
    assert started["delegation_id"] == terminal_only["delegation_id"] == recorded["approval_card"]["delegation_id"]
    assert (started["tool_name"], terminal_only["tool_name"]) == (row["call"]["name"], "unknown_tool")
    assert started["response_key"] != terminal_only["response_key"]
    assert terminal_only["result_preview_head"] == recorded["phoenix_span"]["status_message_head"]
    assert row["call"]["args"]["c2_profiles"][0]["c2_profile_parameters"]["callback_host"].startswith("http://192.0.2.")
    assert APPROVED_ERRORS["provenance"]["substitutions"]

    run = asyncio.run(_assembled_run(row, restore_ledger=False))
    _assert_own_card_closed(run, row)
