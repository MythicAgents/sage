"""Recorded input shapes at the real native consumer, with synthetic state.

Reuse the catalog fixture's real producer, graph, brief admission and card driver.
The model output, current ledger/callback state and external transports are inert;
this checks code given a recorded shape, not model choice or a full incident replay.
No approval is submitted. The inherited setup seeds a proved key using inert task
output; zero-task assertions below concern the proposal turn after that setup.
"""

import asyncio
import json
from pathlib import Path
import socket

import pytest

from tests.test_supervised_catalog_step_binding import _client, _run, _select


CORPUS = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/handoff_targets.json").read_text())


class DesiredCardAssertion(AssertionError):
    """Only the equivalent-FQDN desired-card count may be an expected failure."""


@pytest.fixture
def no_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("connection attempted")
        raise AssertionError("model-input boundary test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    return attempts


@pytest.mark.parametrize("case", [
    pytest.param(case, id=case["id"], marks=pytest.mark.xfail(
        strict=True, raises=DesiredCardAssertion,
        reason="Open defect: the selected catalog action rejects its equivalent host FQDN before a card",
    ) if case["id"] == "equivalent_fqdn" else ())
    for case in CORPUS["cases"]
])
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
    run = asyncio.run(_run(mt, split, [_select(
        shape["allowed_capabilities"][0], "alpha-policy",
        allowed_capabilities=shape["allowed_capabilities"],
        callback_display_id=shape["callback_display_id"], targets=case["targets"],
    )], decision=None))

    # These assertions are outside the sole expected-failure exception type.
    assert no_network == []
    assert run.issued == [] and run.before.issued == []
    assert run.before.worker_calls == 0
    assert run.outcomes == []
    if run.cards:
        assert run.briefs == [("accepted", "Mythic_Operator")]
        for card in run.cards:
            arguments = card["data"]["arguments"]
            assert arguments["action"]["name"] == shape["allowed_capabilities"][0]
            assert arguments["action"]["intent"]["gpo"] == "alpha-policy"
            assert arguments["inputs"]["callback_id"] == shape["callback_display_id"]
    else:
        assert run.briefs and all(phase == "rejected" for phase, _ in run.briefs)
        [terminal] = run.terminals
        if case["id"] == "equivalent_fqdn":
            assert "Blocked" in str(terminal.get("content")), terminal
            [(_, reason)] = run.briefs
            assert "catalog binding:" in reason
            assert "does not act on ws02.child.lab.local" in reason

    if case["id"] == "equivalent_fqdn":
        # Only this assertion is calibrated as xfail. A fixed consumer is strict
        # XPASS until this exception is removed by the boundary's repair line.
        try:
            assert len(run.cards) == case["expected_cards"], "equivalent FQDN must produce one approval card"
        except AssertionError as exc:
            raise DesiredCardAssertion(str(exc)) from exc
    else:
        assert len(run.cards) == case["expected_cards"], (case["id"], run.briefs)
