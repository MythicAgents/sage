import itertools
import copy

import pytest

from ai.langgraph.subgoal_state import (
    DuplicateAdmissionError,
    SubgoalState,
    SubgoalStatus,
    apply_worker_outcome,
    assign_and_admit,
    cancel,
    new_subgoal,
    stop_condition_satisfied,
)


def test_subgoal_id_is_request_stable_and_paraphrase_independent():
    first = new_subgoal("request-1", "actions_complete")
    same = new_subgoal("request-1", "actions_complete")
    other = new_subgoal("request-2", "actions_complete")

    assert first.subgoal_id == same.subgoal_id
    assert first.subgoal_id != other.subgoal_id


def test_active_subgoal_has_one_owner_and_roundtrips_strictly():
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )

    assert running.owner == "Mythic_Operator"
    assert running.status == SubgoalStatus.RUNNING
    assert SubgoalState.from_dict(running.to_dict()) == running


def test_exact_execution_tuple_is_admitted_at_most_once():
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )

    with pytest.raises(DuplicateAdmissionError):
        assign_and_admit(
            running,
            owner="Mythic_Operator",
            method="transfer_to_Mythic_Operator",
        )


def test_verified_revision_or_method_change_permits_one_new_attempt():
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="Mythic_Operator",
        method="method-a",
    )
    progressed = apply_worker_outcome(
        running,
        outcome_id="outcome-1",
        outcome="progress",
        source_owner="Mythic_Operator",
        verified_revision="evidence-revision-2",
    )
    retried = assign_and_admit(
        progressed,
        owner="Mythic_Operator",
        method="method-a",
    )
    method_changed = apply_worker_outcome(
        retried,
        outcome_id="outcome-2",
        outcome="progress",
        source_owner="Mythic_Operator",
    )
    method_changed = assign_and_admit(
        method_changed,
        owner="Mythic_Operator",
        method="method-b",
    )

    assert len(method_changed.admissions) == 3
    assert method_changed.admissions[1].state_revision == "evidence-revision-2"


def test_progress_without_verified_revision_does_not_enable_same_retry():
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="BloodHound",
        method="query",
    )
    progressed = apply_worker_outcome(
        running,
        outcome_id="outcome-1",
        outcome="progress",
        source_owner="BloodHound",
    )

    with pytest.raises(DuplicateAdmissionError):
        assign_and_admit(progressed, owner="BloodHound", method="query")


def test_handoff_changes_owner_without_changing_subgoal_identity():
    running = assign_and_admit(
        new_subgoal("request-1", "objective_proved"),
        owner="Mythic_Operator",
        method="collect",
    )
    handed_off = apply_worker_outcome(
        running,
        outcome_id="handoff-1",
        outcome="handoff",
        source_owner="Mythic_Operator",
        next_owner="BloodHound",
    )

    assert handed_off.subgoal_id == running.subgoal_id
    assert handed_off.owner == "BloodHound"
    assert handed_off.status == SubgoalStatus.ASSIGNED
    assert [event.kind for event in handed_off.transitions[-2:]] == [
        "handed_off",
        "assigned",
    ]


@pytest.mark.parametrize("typed_outcome", ("blocked", "complete"))
def test_terminal_worker_outcomes_remove_owner_and_prevent_delegation(typed_outcome):
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="Generalist",
        method="answer",
    )
    terminal = apply_worker_outcome(
        running,
        outcome_id=f"{typed_outcome}-1",
        outcome=typed_outcome,
        source_owner="Generalist",
    )

    assert terminal.owner == ""
    assert terminal.is_terminal
    with pytest.raises(ValueError, match="terminal"):
        assign_and_admit(terminal, owner="Generalist", method="answer")


@pytest.mark.parametrize(
    ("outcome", "next_owner"),
    (
        ("handoff", ""),
        ("handoff", "Mythic_Operator"),
        ("blocked", "BloodHound"),
        ("complete", "BloodHound"),
        ("progress", "BloodHound"),
    ),
)
def test_malformed_or_self_owner_transitions_fail_closed(outcome, next_owner):
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="Mythic_Operator",
        method="act",
    )

    with pytest.raises(ValueError):
        apply_worker_outcome(
            running,
            outcome_id="outcome-1",
            outcome=outcome,
            source_owner="Mythic_Operator",
            next_owner=next_owner,
        )


def test_duplicate_outcome_delivery_is_idempotent():
    running = assign_and_admit(
        new_subgoal("request-1", "actions_complete"),
        owner="Mythic_Operator",
        method="act",
    )
    first = apply_worker_outcome(
        running,
        outcome_id="outcome-1",
        outcome="handoff",
        source_owner="Mythic_Operator",
        next_owner="BloodHound",
    )
    replay = apply_worker_outcome(
        first,
        outcome_id="outcome-1",
        outcome="complete",
        source_owner="BloodHound",
    )

    assert replay is first


def test_stop_condition_matrix_and_cancel_from_every_nonterminal_state():
    complete = apply_worker_outcome(
        assign_and_admit(
            new_subgoal("request-a", "actions_complete"),
            owner="Generalist",
            method="answer",
        ),
        outcome_id="done",
        outcome="complete",
        source_owner="Generalist",
    )
    assert stop_condition_satisfied(complete)
    assert stop_condition_satisfied(
        new_subgoal("request-r", "response_emitted"),
        {"final_response"},
    )

    states = [
        new_subgoal("request-stop", "operator_stop"),
        assign_and_admit(
            new_subgoal("request-stop-2", "operator_stop"),
            owner="Generalist",
            method="answer",
        ),
    ]
    states.append(
        apply_worker_outcome(
            states[-1],
            outcome_id="progress",
            outcome="progress",
            source_owner="Generalist",
            verified_revision="r2",
        )
    )
    for index, state in enumerate(states):
        stopped = cancel(state, f"stop-{index}")
        assert stopped.status == SubgoalStatus.CANCELLED
        assert stopped.owner == ""
        assert stop_condition_satisfied(stopped)


def test_generated_owner_method_revision_sequences_preserve_invariants():
    owners = ("Mythic_Operator", "BloodHound")
    methods = ("method-a", "method-b")
    revisions = ("r1", "r2")

    for owner, method, revision in itertools.product(owners, methods, revisions):
        state = new_subgoal(f"request-{owner}-{method}-{revision}", "actions_complete")
        state = assign_and_admit(state, owner=owner, method=method)
        progressed = apply_worker_outcome(
            state,
            outcome_id="progress",
            outcome="progress",
            source_owner=owner,
            verified_revision=revision,
        )
        if revision != "0":
            retry = assign_and_admit(progressed, owner=owner, method=method)
            assert len({item.key for item in retry.admissions}) == len(retry.admissions)
            assert retry.owner == owner
        assert all(
            admission.request_id == state.request_id
            and admission.subgoal_id == state.subgoal_id
            for admission in progressed.admissions
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(state_revision="forged"),
        lambda value: value.update(owner="BloodHound"),
        lambda value: value["transitions"][-1].update(state_revision="forged"),
        lambda value: value["transitions"][-1].update(event_id="0" * 64),
        lambda value: value["transitions"][-1].update(extra=True),
        lambda value: value["admissions"][-1].update(state_revision="forged"),
        lambda value: value["admissions"][-1].update(extra=True),
        lambda value: value.update(subgoal_id="0" * 64),
        lambda value: value.update(stop_condition="operator_stop"),
        lambda value: value["processed_outcomes"].append("forged-outcome"),
    ),
)
def test_serialized_authority_mutations_fail_closed(mutate):
    running = assign_and_admit(
        new_subgoal("request-strict", "actions_complete"),
        owner="Mythic_Operator",
        method="execute",
    )
    serialized = copy.deepcopy(running.to_dict())
    mutate(serialized)

    with pytest.raises(ValueError):
        SubgoalState.from_dict(serialized)


def test_coherent_transition_append_cannot_reopen_serialized_terminal_state():
    import ai.langgraph.subgoal_state as module

    terminal = apply_worker_outcome(
        assign_and_admit(
            new_subgoal("request-terminal-chain", "actions_complete"),
            owner="Generalist",
            method="answer",
        ),
        outcome_id="complete-1",
        outcome="complete",
        source_owner="Generalist",
    )
    serialized = terminal.to_dict()
    sequence = len(serialized["transitions"]) + 1
    appended = {
        "kind": "assigned",
        "prior_status": "completed",
        "status": "assigned",
        "owner": "BloodHound",
        "method": "transfer_to_BloodHound",
        "state_revision": terminal.state_revision,
        "outcome_id": "",
    }
    appended["event_id"] = module._transition_event_id(
        terminal,
        sequence=sequence,
        **appended,
    )
    serialized["transitions"].append(appended)
    serialized.update(
        status="assigned",
        owner="BloodHound",
        method="transfer_to_BloodHound",
        terminal_reason="",
    )

    with pytest.raises(ValueError, match="terminal.*cannot continue"):
        SubgoalState.from_dict(serialized)


@pytest.mark.parametrize(
    ("outcome", "forged_reason"),
    (("blocked", "complete"), ("complete", "blocked")),
)
def test_terminal_reason_must_match_status(outcome, forged_reason):
    terminal = apply_worker_outcome(
        assign_and_admit(
            new_subgoal(f"request-reason-{outcome}", "actions_complete"),
            owner="Generalist",
            method="answer",
        ),
        outcome_id=f"{outcome}-1",
        outcome=outcome,
        source_owner="Generalist",
    )
    serialized = terminal.to_dict()
    serialized["terminal_reason"] = forged_reason

    with pytest.raises(ValueError, match="terminal reason"):
        SubgoalState.from_dict(serialized)
