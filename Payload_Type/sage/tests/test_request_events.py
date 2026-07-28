import itertools

import pytest

from ai.langgraph.request_events import (
    RequestEventLedger,
    control_transition_errors,
    stable_event_id,
)


def _complete_ledger(request_id="request-1"):
    ledger = RequestEventLedger(request_id)
    operator = stable_event_id(request_id, "operator_input", "native-1")
    control = stable_event_id(request_id, "control_transition", "contract-installed")
    tool = stable_event_id(request_id, "tool", "call-1:list_callbacks")
    delegation = stable_event_id(request_id, "delegation", "bloodhound-1")
    final = stable_event_id(request_id, "final_response", "terminal")
    ledger.record(
        event_id=operator,
        kind="operator_input",
        phase="received",
        content="List callbacks.",
    )
    ledger.record(
        event_id=control,
        kind="control_transition",
        phase="request_installed",
        content="request contract installed",
    )
    for event_id, kind, start, terminal in (
        (tool, "tool", "started", "completed"),
        (delegation, "delegation", "opened", "finished"),
    ):
        ledger.record(event_id=event_id, kind=kind, phase=start)
        ledger.record_projection(
            event_id=event_id,
            kind=kind,
            phase=start,
            projection_key=f"event:{event_id}",
        )
        ledger.record(event_id=event_id, kind=kind, phase=terminal)
        ledger.record_projection(
            event_id=event_id,
            kind=kind,
            phase=terminal,
            projection_key=f"event:{event_id}",
        )
    terminal_control = stable_event_id(
        request_id,
        "control_transition",
        "request-terminal",
    )
    ledger.record(
        event_id=terminal_control,
        kind="control_transition",
        phase="request_terminal",
        content="complete",
    )
    ledger.record(
        event_id=final,
        kind="final_response",
        phase="emitted",
        content="Done.",
    )
    ledger.record_projection(
        event_id=final,
        kind="final_response",
        phase="emitted",
        projection_key=f"event:{final}",
    )
    return ledger


def test_complete_lifecycle_reconciles_and_reconstructs_operator_and_control():
    ledger = _complete_ledger()

    report = ledger.reconcile()
    transcript = ledger.reconstruct_transcript()

    assert report["ok"] is True
    assert report["open"] == []
    assert transcript[0]["kind"] == "operator_input"
    assert any(row["kind"] == "control_transition" for row in transcript)
    assert transcript[-1]["kind"] == "final_response"


@pytest.mark.parametrize(
    ("kind", "phase"),
    (
        ("tool", "started"),
        ("tool", "completed"),
        ("delegation", "opened"),
        ("delegation", "finished"),
        ("final_response", "emitted"),
    ),
)
def test_duplicate_actual_or_projection_fails_reconciliation(kind, phase):
    for projected in (False, True):
        ledger = _complete_ledger(f"request-{kind}-{phase}-{projected}")
        event = next(
            item
            for item in ledger.events
            if item.kind == kind
            and item.phase == phase
            and item.projected is projected
        )
        if projected:
            ledger.record_projection(
                event_id=event.event_id,
                kind=kind,
                phase=phase,
                projection_key=event.projection_key,
            )
        else:
            ledger.record(event_id=event.event_id, kind=kind, phase=phase)

        assert ledger.reconcile()["ok"] is False


@pytest.mark.parametrize("terminal", ("stopped", "cancelled", "error"))
def test_operator_stop_closes_every_open_tool_and_delegation(terminal):
    ledger = RequestEventLedger(f"request-stop-{terminal}")
    for kind, identity, phase in (
        ("tool", "tool-1", "started"),
        ("delegation", "worker-1", "opened"),
    ):
        event_id = stable_event_id(ledger.request_id, kind, identity)
        ledger.record(event_id=event_id, kind=kind, phase=phase)
        ledger.record_projection(
            event_id=event_id,
            kind=kind,
            phase=phase,
            projection_key=f"event:{event_id}",
        )

    closed = ledger.close_open(terminal)

    assert {(event.kind, event.phase) for event in closed} == {
        ("tool", terminal),
        ("delegation", terminal),
    }
    assert ledger.open_lifecycles() == ()


def test_provider_call_id_reuse_is_scoped_by_logical_request():
    first = stable_event_id("logical-request-a", "tool", "call-1:list_callbacks")
    second = stable_event_id("logical-request-b", "tool", "call-1:list_callbacks")

    assert first != second


def test_append_only_records_have_stable_unique_record_ids():
    ledger = _complete_ledger()

    assert len({event.record_id for event in ledger.events}) == len(ledger.events)
    assert tuple(ledger.events) == ledger.events


@pytest.mark.parametrize(
    "events_to_omit",
    (
        ("operator_input",),
        ("control_transition",),
        ("final_response",),
    ),
)
def test_required_request_events_cannot_be_omitted(events_to_omit):
    source = _complete_ledger()
    ledger = RequestEventLedger(source.request_id)
    for event in source.events:
        if event.kind in events_to_omit:
            continue
        ledger._events.append(event)

    assert ledger.reconcile()["ok"] is False


def test_generated_phase_order_does_not_hide_missing_or_duplicate_lifecycle():
    phases = ("started", "completed")
    for sequence in itertools.product(phases, repeat=3):
        ledger = RequestEventLedger(f"generated-{'-'.join(sequence)}")
        event_id = stable_event_id(ledger.request_id, "tool", "call-1")
        for phase in sequence:
            ledger.record(event_id=event_id, kind="tool", phase=phase)
            if ledger.should_project(event_id, "tool", phase):
                ledger.record_projection(
                    event_id=event_id,
                    kind="tool",
                    phase=phase,
                    projection_key=f"event:{event_id}",
                )
        report = ledger.reconcile(require_final=False)
        assert report["ok"] is False


@pytest.mark.parametrize(
    ("kind", "start", "terminal"),
    (
        ("tool", "started", "completed"),
        ("delegation", "opened", "finished"),
    ),
)
def test_terminal_before_open_or_start_fails_reconciliation(
    kind,
    start,
    terminal,
):
    ledger = RequestEventLedger(f"request-order-{kind}")
    event_id = stable_event_id(ledger.request_id, kind, "one")
    for phase in (terminal, start):
        ledger.record(event_id=event_id, kind=kind, phase=phase)
        ledger.record_projection(
            event_id=event_id,
            kind=kind,
            phase=phase,
            projection_key=f"event:{event_id}",
        )

    report = ledger.reconcile(require_final=False)

    assert report["ok"] is False
    assert any("precedes" in error for error in report["errors"])


def test_final_response_requires_prior_typed_request_terminal_transition():
    ledger = _complete_ledger()
    terminal = next(
        event
        for event in ledger.events
        if event.kind == "control_transition"
        and event.phase == "request_terminal"
    )
    ledger._events.remove(terminal)

    report = ledger.reconcile()

    assert report["ok"] is False
    assert "request terminal transition count=0" in report["errors"]


def test_orphan_or_wrongly_keyed_projection_cannot_reconcile():
    ledger = _complete_ledger()
    orphan = stable_event_id(ledger.request_id, "tool", "orphan")
    ledger.record_projection(
        event_id=orphan,
        kind="tool",
        phase="started",
        projection_key="unrelated-key",
    )

    report = ledger.reconcile()

    assert report["ok"] is False
    assert any("evidence count=0" in error for error in report["errors"])
    assert any("does not use the evidence event id" in error for error in report["errors"])


@pytest.mark.parametrize(
    "mutate",
    (
        lambda rows: [],
        lambda rows: rows[:1],
        lambda rows: rows[1:],
        lambda rows: list(reversed(rows)),
        lambda rows: [
            rows[0],
            {
                "event_id": "control:bogus",
                "kind": "control_transition",
                "phase": "not_a_typed_phase",
                "content": "nonsense",
            },
            rows[1],
        ],
        lambda rows: [
            rows[0],
            {**rows[1], "content": "not-terminal"},
        ],
    ),
)
def test_control_transition_grammar_rejects_incomplete_or_unknown_sequences(
    mutate,
):
    rows = [
        {
            "event_id": "control:install",
            "kind": "control_transition",
            "phase": "request_installed",
            "content": "request contract installed",
        },
        {
            "event_id": "control:terminal",
            "kind": "control_transition",
            "phase": "request_terminal",
            "content": "complete",
        },
    ]

    assert control_transition_errors(mutate(rows))


@pytest.mark.parametrize(
    ("phase", "prior_status", "status"),
    (
        ("assigned", "proposed", "assigned"),
        ("execution_admitted", "assigned", "running"),
        ("handed_off", "running", "handed_off"),
        ("blocked", "running", "blocked"),
        ("completed", "running", "completed"),
        ("progressed", "running", "progressed"),
        ("cancelled", "running", "cancelled"),
    ),
)
def test_control_transition_grammar_accepts_declared_subgoal_phases(
    phase,
    prior_status,
    status,
):
    import json

    event_id = f"subgoal:{phase}"
    payload = {
        "event_id": event_id,
        "kind": phase,
        "prior_status": prior_status,
        "status": status,
        "owner": "Generalist",
        "method": "delegate",
        "state_revision": "1",
        "outcome_id": "outcome-1",
    }
    rows = [
        {
            "event_id": "control:install",
            "kind": "control_transition",
            "phase": "request_installed",
            "content": "request contract installed",
        },
        {
            "event_id": event_id,
            "kind": "control_transition",
            "phase": phase,
            "content": json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
        {
            "event_id": "control:terminal",
            "kind": "control_transition",
            "phase": "request_terminal",
            "content": "complete",
        },
    ]

    assert control_transition_errors(rows) == ()
