"""Recorded input shapes at the real native consumer, with synthetic state.

Reuse the catalog fixture's real producer, graph, brief admission and card driver.
The model output, current ledger/callback state and external transports are inert;
this checks code given a recorded shape, not model choice or a full incident replay.
No approval is submitted. The inherited setup seeds a proved key using inert task
output; zero-task assertions below concern the proposal turn after that setup.
Equivalent forms of the selected candidate's host card once; a host the candidate
does not name is refused, at admission when Sage never observed it and at binding
when it is observed but not the candidate's.
"""

import asyncio
import json
from pathlib import Path
import socket

import pytest

from tests.esl_fixtures import seed_ledger
from tests.test_supervised_catalog_step_binding import NOW, _client, _run, _select


CORPUS = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/handoff_targets.json").read_text())


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
    assert len(run.cards) == case["expected_cards"], (case["id"], run.briefs)
    if run.cards:
        assert run.briefs == [("accepted", "Mythic_Operator")]
        for card in run.cards:
            arguments = card["data"]["arguments"]
            assert arguments["action"]["name"] == shape["allowed_capabilities"][0]
            assert arguments["action"]["intent"]["gpo"] == "alpha-policy"
            assert arguments["inputs"]["callback_id"] == shape["callback_display_id"]
    else:
        [(phase, reason)] = run.briefs
        [terminal] = run.terminals
        assert phase == "rejected", run.briefs
        if case["refused_at"] == "binding":
            # Admitted (Sage observed the host) but not the candidate's: the binder refuses and the request ends.
            assert "Blocked" in str(terminal.get("content")), terminal
            assert reason.startswith("catalog binding:") and f"does not act on {case['targets'][0]}" in reason
        else:
            assert reason.startswith("targets:") and case["targets"][0] in reason, reason
