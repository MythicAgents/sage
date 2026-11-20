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


@pytest.mark.parametrize("row", REASK["rows"], ids=[row["id"] for row in REASK["rows"]])
def test_recorded_card_decided_while_down_is_reasked_not_applied(monkeypatch, tmp_path, no_network, row):
    """A recorded decided card reaches the real on-start reconciler, checkpoint reader and validator.

    Expected outcome changed by plan r2 (operator product decision): the stored decision is never
    applied; the same exact action is re-asked with a new approval id in the same request.
    """
    from types import SimpleNamespace

    import aiosqlite
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.types import Interrupt
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

    class Mythic:
        queries: list[str] = []

        async def execute(self, query, variables):
            self.queries.append(query)
            if "sageOpenRequests" in query:
                assert variables["op"] == operation and variables["container"] == "sage"
                return {"chat_request": [request_row]}
            if "sageRestoredCard" in query:
                wanted = variables["ctx"]["input_requested"]["data"]["sage_approval_context"]["approval_id"]
                card = row["card_row"]
                echoed = card["metadata"]["input_requested"]["data"]["sage_approval_context"]["approval_id"]
                return {"chat_message": [card] if echoed == wanted else []}
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

        async def _construct_model(self, kwargs):
            raise AssertionError("reconciliation must not build a Model")

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
        return chat, response, saved

    try:
        chat, response, saved = asyncio.run(scenario())
    finally:
        service._CHANNEL_TURN_LOCKS.clear()

    assert no_network == []
    assert response.EventLogErrorMessage == ""
    on_request = chat.emissions
    assert not [e for e in on_request if e.get("complete_request")], on_request
    cards = [e["metadata"]["input_requested"] for e in on_request
             if isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested"]
    assert len(cards) == 1
    new = cards[0]["data"]["sage_approval_context"]
    old = row["card_row"]["metadata"]["input_requested"]["data"]["sage_approval_context"]
    assert new["approval_id"] != row["expected"]["old_approval_id"]
    assert new["action_digest"] == row["expected"]["action_digest"]
    assert new["proposal_digest"] == row["expected"]["proposal_digest"]
    for key in ("thread_id", "turn_id", "request_id", "request_contract_digest", "operation_id", "apitoken_id"):
        assert new[key] == old[key], key
    assert any("not applied" in str(e.get("content") or "") for e in on_request)
    assert saved["pending"]["context"]["approval_id"] == new["approval_id"]
    assert saved["claim"] is None

    # The decision turn rebuilds its expectation from the saved record and the checkpoint.
    action_requests = checkpoint["interrupt_value"]["action_requests"]
    expected, _contract, reason = restoration.restored_pending_context(
        saved["pending"], action_requests, checkpoint["thread_id"])
    assert expected is not None, reason
    old_answer = restoration.input_response_from_card_row(row["card_row"])
    assert old_answer is not None
    assert not approval_response_matches(SimpleNamespace(InputResponse=old_answer), expected)
    from tests.test_hitl_steer_current_intent import _input_response

    assert approval_response_matches(SimpleNamespace(InputResponse=_input_response("accept", cards[0])), expected)
