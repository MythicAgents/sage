"""ISC-49R 49R-16: sealed kernel decision record.

These tests assert the two properties the criterion actually rests on: the record faithfully carries the
kernel's decision stream, and the seal makes post-hoc mutation detectable — including the deletion,
reordering, and truncation that a per-event digest cannot catch on its own.
"""
from __future__ import annotations

import json
import os

import pytest

from ai.langgraph.decision_record import (
    SCHEMA,
    TEST_OVERRIDE_ENV,
    record_paths,
    seal_request_decision_record,
    verify_decision_record,
)
from ai.langgraph.request_events import RequestEventLedger


REQUEST_ID = "req-49r16"


@pytest.fixture
def active(tmp_path, monkeypatch):
    """Point the emitter at a temp engagement dir and bypass the pytest no-op guard."""
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(TEST_OVERRIDE_ENV, "1")
    return tmp_path


def _ledger(request_id: str = REQUEST_ID) -> RequestEventLedger:
    """A ledger shaped like a real request: install, tool, subgoal transition, terminal."""
    ledger = RequestEventLedger(request_id)
    ledger.record(
        event_id="ct:install",
        kind="control_transition",
        phase="request_installed",
        content="request contract installed",
    )
    ledger.record(
        event_id="tool:whoami",
        kind="tool",
        phase="completed",
        content="whoami",
        metadata={"task_id": "56"},
    )
    ledger.record(
        event_id="ct:assigned",
        kind="control_transition",
        phase="assigned",
        content=json.dumps({"event_id": "ct:assigned", "kind": "assigned"}, sort_keys=True),
    )
    ledger.record(
        event_id="ct:terminal",
        kind="control_transition",
        phase="request_terminal",
        content="complete",
    )
    return ledger


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


def _rewrite(path: str, lines: list[str]) -> None:
    os.chmod(path, 0o644)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


def test_seal_round_trip_verifies(active):
    ledger = _ledger()
    assert seal_request_decision_record(ledger) is not None

    record_path, seal_path = record_paths(REQUEST_ID)
    result = verify_decision_record(record_path, seal_path)

    assert result["ok"], result["errors"]
    assert result["event_count"] == 4
    assert result["terminal_state"] == "complete"

    seal = json.loads(open(seal_path, encoding="utf-8").read())
    assert seal["schema"] == SCHEMA
    assert seal["request_id"] == REQUEST_ID


def test_record_carries_the_kernel_decision_stream(active):
    seal_request_decision_record(_ledger())
    record_path, _ = record_paths(REQUEST_ID)
    events = [json.loads(line)["event"] for line in _read_lines(record_path)]

    kinds = {event["kind"] for event in events}
    phases = {event["phase"] for event in events}
    assert "control_transition" in kinds and "tool" in kinds
    assert "request_installed" in phases  # contract install
    assert "assigned" in phases           # subgoal transition
    assert "request_terminal" in phases   # terminal transition
    assert events[1]["metadata"] == {"task_id": "56"}


def test_mutated_content_breaks_the_chain(active):
    seal_request_decision_record(_ledger())
    record_path, seal_path = record_paths(REQUEST_ID)

    lines = _read_lines(record_path)
    row = json.loads(lines[1])
    row["event"]["content"] = "tampered"
    lines[1] = json.dumps(row, sort_keys=True)
    _rewrite(record_path, lines)

    assert not verify_decision_record(record_path, seal_path)["ok"]


def test_truncated_tail_is_detected(active):
    seal_request_decision_record(_ledger())
    record_path, seal_path = record_paths(REQUEST_ID)

    _rewrite(record_path, _read_lines(record_path)[:-1])

    result = verify_decision_record(record_path, seal_path)
    assert not result["ok"]
    assert any("event_count" in error or "terminal chain" in error for error in result["errors"])


def test_reordered_events_are_detected(active):
    seal_request_decision_record(_ledger())
    record_path, seal_path = record_paths(REQUEST_ID)

    lines = _read_lines(record_path)
    lines[1], lines[2] = lines[2], lines[1]
    _rewrite(record_path, lines)

    assert not verify_decision_record(record_path, seal_path)["ok"]


def test_seal_is_idempotent(active):
    """The success path reaches finalize_visibility_turn twice (service.py:780 and :793)."""
    ledger = _ledger()
    seal_request_decision_record(ledger)
    record_path, seal_path = record_paths(REQUEST_ID)
    first = open(record_path, encoding="utf-8").read()
    first_seal = open(seal_path, encoding="utf-8").read()

    seal_request_decision_record(ledger)
    seal_request_decision_record(ledger)

    assert open(record_path, encoding="utf-8").read() == first
    assert open(seal_path, encoding="utf-8").read() == first_seal


def test_emitter_is_a_no_op_under_pytest_without_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(TEST_OVERRIDE_ENV, raising=False)

    assert seal_request_decision_record(_ledger()) is None
    record_path, _ = record_paths(REQUEST_ID)
    assert not os.path.exists(record_path)


def test_emitter_never_raises_on_a_broken_ledger(active):
    class Broken:
        request_id = "req-broken"

        @property
        def events(self):
            raise RuntimeError("ledger exploded")

    # Fail-safe is the whole behaviour-neutrality argument: evidence may be lost, the request may not fail.
    assert seal_request_decision_record(Broken()) is None


def test_missing_request_id_is_skipped(active):
    class Anonymous:
        request_id = ""
        events = ()

    assert seal_request_decision_record(Anonymous()) is None


def test_verify_reports_unreadable_inputs(active):
    result = verify_decision_record("/nonexistent/record.jsonl", "/nonexistent/seal.json")
    assert not result["ok"]
    assert result["errors"]


# --- The real production seam -------------------------------------------------------------------
#
# The hermetic conversation-contract harness substitutes its own finalize_visibility_turn
# (tests/conversation_contract/harness.py:79), so it never reaches the production funnel. These tests bind
# the REAL Model methods to a minimal stand-in so the actual bytes of model.py execute, rather than a
# reimplementation of them. Without this, the emitter would first run on the live path — which is exactly
# the "tests never exercised Stage A" failure this program has already paid for once.

class _Seam:
    """Carries only the attributes the real methods actually touch."""

    from ai.langgraph.model import Model as _Model

    _ensure_request_event_ledger = _Model._ensure_request_event_ledger
    finalize_visibility_turn = _Model.finalize_visibility_turn
    del _Model

    def __init__(self, ledger: RequestEventLedger):
        self._request_event_ledger = ledger
        # The real _ensure_request_event_ledger normalises the id from the contract and REPLACES any
        # ledger whose id differs, so the contract must carry the same request_id or the seam quietly
        # seals an empty "request:unbound" ledger instead.
        self._request_contract = type("_Contract", (), {"request_id": ledger.request_id})()
        self._delegation_scope = ""
        self.streamed: list[str] = []

    async def _stream_message_to_mythic(self, text: str) -> None:
        self.streamed.append(text)


def _finalize(seam: _Seam, *, require_final: bool = True) -> dict:
    import asyncio

    return asyncio.run(seam.finalize_visibility_turn(require_final=require_final))


def test_real_seam_seals_on_terminal(active):
    seam = _Seam(_ledger())
    _finalize(seam, require_final=True)

    record_path, seal_path = record_paths(REQUEST_ID)
    assert os.path.exists(record_path), "production seam did not emit a decision record"
    assert verify_decision_record(record_path, seal_path)["ok"]


def test_real_seam_does_not_seal_before_terminal(active):
    seam = _Seam(_ledger())
    _finalize(seam, require_final=False)

    record_path, _ = record_paths(REQUEST_ID)
    assert not os.path.exists(record_path)


def test_real_seam_is_idempotent_across_the_double_call(active):
    """Mirrors the success path: service.py:780 then :793, both require_final=True."""
    seam = _Seam(_ledger())
    _finalize(seam, require_final=True)
    record_path, seal_path = record_paths(REQUEST_ID)
    first = open(record_path, encoding="utf-8").read()

    _finalize(seam, require_final=True)

    assert open(record_path, encoding="utf-8").read() == first
    assert verify_decision_record(record_path, seal_path)["ok"]


def test_emitter_does_not_change_the_reconcile_summary_or_ledger(tmp_path, monkeypatch):
    """The gate's falsifier: with the emitter on vs off, the seam's observable output must be identical.

    The hermetic conversation-contract suite cannot prove this, because its harness substitutes its own
    finalize_visibility_turn and never reaches the emitter at all. This compares the real seam directly.
    """
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))

    def run(emitter_active: bool) -> tuple[dict, list[dict], list[str]]:
        if emitter_active:
            monkeypatch.setenv(TEST_OVERRIDE_ENV, "1")
        else:
            monkeypatch.delenv(TEST_OVERRIDE_ENV, raising=False)
        seam = _Seam(_ledger())
        summary = _finalize(seam, require_final=True)
        events = [event.to_dict() for event in seam._request_event_ledger.events]
        return summary, events, seam.streamed

    off_summary, off_events, off_streamed = run(False)
    on_summary, on_events, on_streamed = run(True)

    assert json.dumps(on_summary, sort_keys=True, default=str) == json.dumps(
        off_summary, sort_keys=True, default=str
    )
    assert json.dumps(on_events, sort_keys=True) == json.dumps(off_events, sort_keys=True)
    assert on_streamed == off_streamed

    # ...and the run with the emitter active is the one that actually produced evidence.
    record_path, seal_path = record_paths(REQUEST_ID)
    assert os.path.exists(record_path)
    assert verify_decision_record(record_path, seal_path)["ok"]


def test_real_seam_seals_even_when_reconciliation_fails(active):
    """49R-14: a failed request must still leave evidence, not vanish from the bundle."""
    ledger = RequestEventLedger("req-failed")
    ledger.record(
        event_id="ct:install",
        kind="control_transition",
        phase="request_installed",
        content="request contract installed",
    )
    seam = _Seam(ledger)
    summary = _finalize(seam, require_final=True)

    record_path, seal_path = record_paths("req-failed")
    assert not summary["ok"], "expected an unreconciled request for this test"
    assert os.path.exists(record_path), "a failing request must still be recorded"
    assert verify_decision_record(record_path, seal_path)["ok"]
