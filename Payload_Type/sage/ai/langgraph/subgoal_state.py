"""Pure request-scoped subgoal ownership and retry state.

Prose is deliberately absent from this module. A subgoal keeps one stable identity,
one current owner, and an append-only set of admitted execution tuples. The same
owner/method/revision tuple can never be admitted twice.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1


class SubgoalStatus(str, Enum):
    PROPOSED = "proposed"
    ASSIGNED = "assigned"
    RUNNING = "running"
    PROGRESSED = "progressed"
    HANDED_OFF = "handed_off"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TypedOutcome(str, Enum):
    PROGRESS = "progress"
    HANDOFF = "handoff"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class DuplicateAdmissionError(ValueError):
    """The exact execution tuple was already admitted."""


@dataclass(frozen=True)
class Admission:
    request_id: str
    subgoal_id: str
    owner: str
    method: str
    state_revision: str

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.request_id,
            self.subgoal_id,
            self.owner,
            self.method,
            self.state_revision,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "subgoal_id": self.subgoal_id,
            "owner": self.owner,
            "method": self.method,
            "state_revision": self.state_revision,
        }


@dataclass(frozen=True)
class Transition:
    event_id: str
    kind: str
    prior_status: str
    status: str
    owner: str
    method: str
    state_revision: str
    outcome_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "prior_status": self.prior_status,
            "status": self.status,
            "owner": self.owner,
            "method": self.method,
            "state_revision": self.state_revision,
            "outcome_id": self.outcome_id,
        }


@dataclass(frozen=True)
class SubgoalState:
    request_id: str
    subgoal_id: str
    stop_condition: str
    status: SubgoalStatus = SubgoalStatus.PROPOSED
    owner: str = ""
    method: str = ""
    state_revision: str = "0"
    admissions: tuple[Admission, ...] = ()
    processed_outcomes: tuple[str, ...] = ()
    transitions: tuple[Transition, ...] = ()
    terminal_reason: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            SubgoalStatus.BLOCKED,
            SubgoalStatus.COMPLETED,
            SubgoalStatus.CANCELLED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": self.request_id,
            "subgoal_id": self.subgoal_id,
            "stop_condition": self.stop_condition,
            "status": self.status.value,
            "owner": self.owner,
            "method": self.method,
            "state_revision": self.state_revision,
            "admissions": [item.to_dict() for item in self.admissions],
            "processed_outcomes": list(self.processed_outcomes),
            "transitions": [item.to_dict() for item in self.transitions],
            "terminal_reason": self.terminal_reason,
        }

    @staticmethod
    def from_dict(value: Mapping[str, Any]) -> "SubgoalState":
        required = {
            "schema_version",
            "request_id",
            "subgoal_id",
            "stop_condition",
            "status",
            "owner",
            "method",
            "state_revision",
            "admissions",
            "processed_outcomes",
            "transitions",
            "terminal_reason",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("subgoal state has an invalid schema")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("subgoal state schema version is unsupported")
        state = SubgoalState(
            request_id=_required(value.get("request_id"), "request_id"),
            subgoal_id=_required(value.get("subgoal_id"), "subgoal_id"),
            stop_condition=_required(value.get("stop_condition"), "stop_condition"),
            status=SubgoalStatus(value.get("status")),
            owner=_optional(value.get("owner"), "owner"),
            method=_optional(value.get("method"), "method"),
            state_revision=_required(value.get("state_revision"), "state_revision"),
            admissions=tuple(
                Admission(
                    request_id=_required(item.get("request_id"), "admission.request_id"),
                    subgoal_id=_required(item.get("subgoal_id"), "admission.subgoal_id"),
                    owner=_required(item.get("owner"), "admission.owner"),
                    method=_required(item.get("method"), "admission.method"),
                    state_revision=_required(
                        item.get("state_revision"),
                        "admission.state_revision",
                    ),
                )
                for item in _exact_mapping_items(
                    value.get("admissions"),
                    "admissions",
                    {
                        "request_id",
                        "subgoal_id",
                        "owner",
                        "method",
                        "state_revision",
                    },
                )
            ),
            processed_outcomes=_unique_strings(
                value.get("processed_outcomes"),
                "processed_outcomes",
            ),
            transitions=tuple(
                Transition(
                    event_id=_required(item.get("event_id"), "transition.event_id"),
                    kind=_required(item.get("kind"), "transition.kind"),
                    prior_status=_required(
                        item.get("prior_status"),
                        "transition.prior_status",
                    ),
                    status=_required(item.get("status"), "transition.status"),
                    owner=_optional(item.get("owner"), "transition.owner"),
                    method=_optional(item.get("method"), "transition.method"),
                    state_revision=_required(
                        item.get("state_revision"),
                        "transition.state_revision",
                    ),
                    outcome_id=_optional(
                        item.get("outcome_id"),
                        "transition.outcome_id",
                    ),
                )
                for item in _exact_mapping_items(
                    value.get("transitions"),
                    "transitions",
                    {
                        "event_id",
                        "kind",
                        "prior_status",
                        "status",
                        "owner",
                        "method",
                        "state_revision",
                        "outcome_id",
                    },
                )
            ),
            terminal_reason=_optional(
                value.get("terminal_reason"),
                "terminal_reason",
            ),
        )
        _validate_state(state)
        return state


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _mapping_items(value: Any, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{name} must be a list of objects")
    return list(value)


def _exact_mapping_items(
    value: Any,
    name: str,
    keys: set[str],
) -> list[Mapping[str, Any]]:
    items = _mapping_items(value, name)
    if any(set(item) != keys for item in items):
        raise ValueError(f"{name} entries have an invalid schema")
    return items


def _unique_strings(value: Any, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{name} must contain unique non-empty strings")
    return tuple(value)


def _stable_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _transition_event_id(
    state: SubgoalState,
    *,
    sequence: int,
    kind: str,
    prior_status: str,
    status: str,
    owner: str,
    method: str,
    state_revision: str,
    outcome_id: str,
) -> str:
    return _stable_digest({
        "kind": kind,
        "method": method,
        "outcome_id": outcome_id,
        "owner": owner,
        "prior_status": prior_status,
        "request_id": state.request_id,
        "sequence": sequence,
        "state_revision": state_revision,
        "status": status,
        "subgoal_id": state.subgoal_id,
    })


def _transition(
    state: SubgoalState,
    *,
    kind: str,
    status: SubgoalStatus,
    owner: str,
    method: str,
    state_revision: str | None = None,
    terminal_reason: str = "",
    admission: Admission | None = None,
    outcome_id: str = "",
) -> SubgoalState:
    revision = state.state_revision if state_revision is None else state_revision
    sequence = len(state.transitions) + 1
    event = Transition(
        event_id=_transition_event_id(
            state,
            sequence=sequence,
            kind=kind,
            prior_status=state.status.value,
            status=status.value,
            owner=owner,
            method=method,
            state_revision=revision,
            outcome_id=outcome_id,
        ),
        kind=kind,
        prior_status=state.status.value,
        status=status.value,
        owner=owner,
        method=method,
        state_revision=revision,
        outcome_id=outcome_id,
    )
    updated = replace(
        state,
        status=status,
        owner=owner,
        method=method,
        state_revision=revision,
        admissions=(
            state.admissions + (admission,)
            if admission is not None
            else state.admissions
        ),
        processed_outcomes=(
            state.processed_outcomes + (outcome_id,)
            if outcome_id
            else state.processed_outcomes
        ),
        transitions=state.transitions + (event,),
        terminal_reason=terminal_reason,
    )
    _validate_state(updated)
    return updated


def _validate_state(state: SubgoalState) -> None:
    if state.subgoal_id != _stable_digest({
        "request_id": state.request_id,
        "stop_condition": state.stop_condition,
        "subgoal": "root",
    }):
        raise ValueError("subgoal identity does not match its request")
    if state.status in {
        SubgoalStatus.ASSIGNED,
        SubgoalStatus.RUNNING,
        SubgoalStatus.PROGRESSED,
        SubgoalStatus.HANDED_OFF,
    } and not state.owner:
        raise ValueError("active subgoal must have exactly one owner")
    if state.is_terminal and state.owner:
        raise ValueError("terminal subgoal cannot retain an owner")
    if any(
        admission.request_id != state.request_id
        or admission.subgoal_id != state.subgoal_id
        for admission in state.admissions
    ):
        raise ValueError("admission belongs to a different request or subgoal")
    keys = [admission.key for admission in state.admissions]
    if len(keys) != len(set(keys)):
        raise ValueError("subgoal state contains a duplicate admission")
    prior_status = SubgoalStatus.PROPOSED.value
    prior_owner = ""
    prior_method = ""
    prior_revision = "0"
    for sequence, transition in enumerate(state.transitions, start=1):
        if prior_status in {
            SubgoalStatus.BLOCKED.value,
            SubgoalStatus.COMPLETED.value,
            SubgoalStatus.CANCELLED.value,
        }:
            raise ValueError("terminal subgoal transition chain cannot continue")
        try:
            SubgoalStatus(transition.prior_status)
            SubgoalStatus(transition.status)
        except ValueError as exc:
            raise ValueError("transition contains an invalid status") from exc
        if transition.prior_status != prior_status:
            raise ValueError("transition chain is discontinuous")
        expected_event_id = _transition_event_id(
            state,
            sequence=sequence,
            kind=transition.kind,
            prior_status=transition.prior_status,
            status=transition.status,
            owner=transition.owner,
            method=transition.method,
            state_revision=transition.state_revision,
            outcome_id=transition.outcome_id,
        )
        if transition.event_id != expected_event_id:
            raise ValueError("transition event identity is invalid")
        if transition.kind == "assigned":
            if (
                transition.status != SubgoalStatus.ASSIGNED.value
                or not transition.owner
                or (prior_owner and transition.owner != prior_owner)
                or transition.state_revision != prior_revision
                or transition.outcome_id
            ):
                raise ValueError("assigned transition is invalid")
        elif transition.kind == "execution_admitted":
            if (
                prior_status != SubgoalStatus.ASSIGNED.value
                or transition.status != SubgoalStatus.RUNNING.value
                or not transition.owner
                or transition.owner != prior_owner
                or not transition.method
                or transition.method != prior_method
                or transition.state_revision != prior_revision
                or transition.outcome_id
            ):
                raise ValueError("execution-admitted transition is invalid")
        elif transition.kind == "progressed":
            if (
                prior_status not in {
                    SubgoalStatus.RUNNING.value,
                    SubgoalStatus.PROGRESSED.value,
                }
                or transition.status != SubgoalStatus.PROGRESSED.value
                or transition.owner != prior_owner
                or transition.method != prior_method
                or not transition.outcome_id
            ):
                raise ValueError("progress transition is invalid")
        elif transition.kind == "handed_off":
            if (
                prior_status not in {
                    SubgoalStatus.RUNNING.value,
                    SubgoalStatus.PROGRESSED.value,
                }
                or transition.status != SubgoalStatus.HANDED_OFF.value
                or not transition.owner
                or transition.owner == prior_owner
                or transition.method
                or not transition.outcome_id
            ):
                raise ValueError("handoff transition is invalid")
        elif transition.kind in {"blocked", "completed"}:
            expected_status = (
                SubgoalStatus.BLOCKED.value
                if transition.kind == "blocked"
                else SubgoalStatus.COMPLETED.value
            )
            if (
                prior_status not in {
                    SubgoalStatus.RUNNING.value,
                    SubgoalStatus.PROGRESSED.value,
                }
                or transition.status != expected_status
                or transition.owner
                or transition.method
                or not transition.outcome_id
            ):
                raise ValueError("terminal worker transition is invalid")
        elif transition.kind == "cancelled":
            if (
                prior_status
                in {
                    SubgoalStatus.BLOCKED.value,
                    SubgoalStatus.COMPLETED.value,
                    SubgoalStatus.CANCELLED.value,
                }
                or transition.status != SubgoalStatus.CANCELLED.value
                or transition.owner
                or transition.method
                or transition.outcome_id
            ):
                raise ValueError("cancel transition is invalid")
        else:
            raise ValueError("transition kind is invalid")
        prior_status = transition.status
        prior_owner = transition.owner
        prior_method = transition.method
        prior_revision = transition.state_revision
    if state.transitions:
        tip = state.transitions[-1]
        if (
            state.status.value != tip.status
            or state.owner != tip.owner
            or state.method != tip.method
            or state.state_revision != tip.state_revision
        ):
            raise ValueError("current subgoal state does not match transition-chain tip")
    elif (
        state.status != SubgoalStatus.PROPOSED
        or state.owner
        or state.method
        or state.state_revision != "0"
    ):
        raise ValueError("untransitioned subgoal state must be the initial proposal")
    admitted_events = [
        transition
        for transition in state.transitions
        if transition.kind == "execution_admitted"
    ]
    if len(admitted_events) != len(state.admissions):
        raise ValueError("admissions do not match execution-admitted events")
    for event, admission in zip(admitted_events, state.admissions):
        if (
            event.owner != admission.owner
            or event.method != admission.method
            or event.state_revision != admission.state_revision
        ):
            raise ValueError("admission does not match its transition event")
    derived_outcomes = tuple(
        transition.outcome_id
        for transition in state.transitions
        if transition.outcome_id
    )
    if state.processed_outcomes != derived_outcomes:
        raise ValueError("processed outcomes do not match transition events")
    if state.is_terminal:
        if not state.terminal_reason:
            raise ValueError("terminal subgoal must have a reason")
        if (
            state.status == SubgoalStatus.BLOCKED
            and state.terminal_reason != "blocked"
        ):
            raise ValueError("blocked subgoal has an invalid terminal reason")
        if (
            state.status == SubgoalStatus.COMPLETED
            and state.terminal_reason != "complete"
        ):
            raise ValueError("completed subgoal has an invalid terminal reason")
    elif state.terminal_reason:
        raise ValueError("nonterminal subgoal cannot have a terminal reason")


def new_subgoal(request_id: str, stop_condition: str) -> SubgoalState:
    request = _required(request_id, "request_id")
    condition = _required(stop_condition, "stop_condition")
    return SubgoalState(
        request_id=request,
        subgoal_id=_stable_digest({
            "request_id": request,
            "stop_condition": condition,
            "subgoal": "root",
        }),
        stop_condition=condition,
    )


def transition_token(state: SubgoalState) -> str:
    """Return the exact compare-and-set token for one validated pre-transition state."""
    _validate_state(state)
    return _stable_digest(state.to_dict())


def assign_and_admit(
    state: SubgoalState,
    *,
    owner: str,
    method: str,
) -> SubgoalState:
    """Assign one owner and admit its exact execution tuple once."""
    target_owner = _required(owner, "owner")
    target_method = _required(method, "method")
    if state.is_terminal:
        raise ValueError("terminal subgoal cannot be assigned")
    if state.owner and state.owner != target_owner:
        raise ValueError("subgoal requires a typed handoff before owner change")
    admission = Admission(
        request_id=state.request_id,
        subgoal_id=state.subgoal_id,
        owner=target_owner,
        method=target_method,
        state_revision=state.state_revision,
    )
    if admission.key in {item.key for item in state.admissions}:
        raise DuplicateAdmissionError("subgoal execution tuple was already admitted")
    assigned = _transition(
        state,
        kind="assigned",
        status=SubgoalStatus.ASSIGNED,
        owner=target_owner,
        method=target_method,
    )
    running = _transition(
        assigned,
        kind="execution_admitted",
        status=SubgoalStatus.RUNNING,
        owner=target_owner,
        method=target_method,
        admission=admission,
    )
    return running


def apply_worker_outcome(
    state: SubgoalState,
    *,
    outcome_id: str,
    outcome: str,
    source_owner: str,
    next_owner: str = "",
    verified_revision: str = "",
) -> SubgoalState:
    """Apply one typed worker outcome; summary prose is intentionally not accepted."""
    identity = _required(outcome_id, "outcome_id")
    if identity in state.processed_outcomes:
        return state
    if state.is_terminal:
        raise ValueError("terminal subgoal cannot accept a worker outcome")
    owner = _required(source_owner, "source_owner")
    if owner != state.owner:
        raise ValueError("worker outcome source is not the current owner")
    typed = TypedOutcome(outcome)
    target = _optional(next_owner, "next_owner")
    revision = _optional(verified_revision, "verified_revision")
    if revision and revision == state.state_revision:
        revision = ""

    if typed == TypedOutcome.HANDOFF:
        if not target or target == owner:
            raise ValueError("handoff requires a different next owner")
        updated = _transition(
            state,
            kind="handed_off",
            status=SubgoalStatus.HANDED_OFF,
            owner=target,
            method="",
            state_revision=revision or state.state_revision,
            outcome_id=identity,
        )
        updated = _transition(
            updated,
            kind="assigned",
            status=SubgoalStatus.ASSIGNED,
            owner=target,
            method="",
        )
    elif typed == TypedOutcome.BLOCKED:
        if target:
            raise ValueError("blocked outcome cannot carry a next owner")
        updated = _transition(
            state,
            kind="blocked",
            status=SubgoalStatus.BLOCKED,
            owner="",
            method="",
            state_revision=revision or state.state_revision,
            terminal_reason="blocked",
            outcome_id=identity,
        )
    elif typed == TypedOutcome.COMPLETE:
        if target:
            raise ValueError("complete outcome cannot carry a next owner")
        updated = _transition(
            state,
            kind="completed",
            status=SubgoalStatus.COMPLETED,
            owner="",
            method="",
            state_revision=revision or state.state_revision,
            terminal_reason="complete",
            outcome_id=identity,
        )
    else:
        if target:
            raise ValueError("progress outcome cannot carry a next owner")
        updated = _transition(
            state,
            kind="progressed",
            status=SubgoalStatus.PROGRESSED,
            owner=owner,
            method=state.method,
            state_revision=revision or state.state_revision,
            outcome_id=identity,
        )
    return updated


def cancel(state: SubgoalState, reason: str = "operator_stop") -> SubgoalState:
    if state.is_terminal:
        return state
    return _transition(
        state,
        kind="cancelled",
        status=SubgoalStatus.CANCELLED,
        owner="",
        method="",
        terminal_reason=_required(reason, "reason"),
    )


def block_duplicate(state: SubgoalState) -> SubgoalState:
    """Terminalize a current-state duplicate without admitting another execution."""
    if state.is_terminal:
        return state
    identity = _stable_digest({
        "kind": "duplicate_admission",
        "method": state.method,
        "owner": state.owner,
        "request_id": state.request_id,
        "state_revision": state.state_revision,
        "subgoal_id": state.subgoal_id,
    })
    if identity in state.processed_outcomes:
        return state
    return _transition(
        state,
        kind="blocked",
        status=SubgoalStatus.BLOCKED,
        owner="",
        method="",
        terminal_reason="blocked",
        outcome_id=identity,
    )


def stop_condition_satisfied(
    state: SubgoalState,
    observed_events: Iterable[str] = (),
) -> bool:
    events = set(observed_events)
    if state.status == SubgoalStatus.BLOCKED:
        return True
    if state.stop_condition == "operator_stop":
        return state.status == SubgoalStatus.CANCELLED
    if state.stop_condition == "response_emitted":
        return "final_response" in events
    if state.stop_condition in {"actions_complete", "objective_proved"}:
        return state.status == SubgoalStatus.COMPLETED
    return False
