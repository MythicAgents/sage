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
