"""Append-only request lifecycle evidence and reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable


TOOL_TERMINAL_PHASES = frozenset({"completed", "error", "stopped", "cancelled"})
DELEGATION_TERMINAL_PHASES = frozenset({"finished", "error", "stopped", "cancelled"})
REQUEST_TERMINAL_STATUSES = frozenset(
    {"complete", "blocked", "stopped", "cancelled", "error"}
)
SUBGOAL_CONTROL_PHASES = frozenset(
    {
        "assigned",
        "execution_admitted",
        "handed_off",
        "blocked",
        "completed",
        "progressed",
        "cancelled",
    }
)
CONTROL_TRANSITION_FIELDS = frozenset(
    {"event_id", "kind", "phase", "content"}
)
SUBGOAL_TRANSITION_FIELDS = frozenset(
    {
        "event_id",
        "kind",
        "prior_status",
        "status",
        "owner",
        "method",
        "state_revision",
        "outcome_id",
    }
)


def control_transition_errors(
    transitions: Iterable[dict[str, Any]],
    *,
    require_terminal: bool = True,
) -> tuple[str, ...]:
    """Validate the finite request/subgoal control grammar used in transcripts."""
    rows = list(transitions)
    errors: list[str] = []
    seen: set[str] = set()
    installed_indexes: list[int] = []
    terminal_indexes: list[int] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != CONTROL_TRANSITION_FIELDS:
            errors.append(f"control transition {index} has an invalid schema")
            continue
        if any(not isinstance(row.get(key), str) for key in CONTROL_TRANSITION_FIELDS):
            errors.append(f"control transition {index} fields must be strings")
            continue
        event_id = row["event_id"]
        phase = row["phase"]
        content = row["content"]
        if not event_id or row["kind"] != "control_transition" or not phase:
            errors.append(f"control transition {index} has an invalid identity")
            continue
        if event_id in seen:
            errors.append(f"duplicate control transition event id: {event_id}")
        seen.add(event_id)
        if phase == "request_installed":
            installed_indexes.append(index)
            if content != "request contract installed":
                errors.append("request_installed content is invalid")
            continue
        if phase == "request_terminal":
            terminal_indexes.append(index)
            if content not in REQUEST_TERMINAL_STATUSES:
                errors.append("request_terminal status is invalid")
            continue
        if phase not in SUBGOAL_CONTROL_PHASES:
            errors.append(f"unknown control transition phase: {phase}")
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            errors.append(f"{phase} control content is not typed JSON")
            continue
        if not isinstance(payload, dict) or set(payload) != SUBGOAL_TRANSITION_FIELDS:
            errors.append(f"{phase} control content has an invalid schema")
            continue
        if any(not isinstance(payload.get(key), str) for key in SUBGOAL_TRANSITION_FIELDS):
            errors.append(f"{phase} control content fields must be strings")
            continue
        if payload["event_id"] != event_id or payload["kind"] != phase:
            errors.append(f"{phase} control content does not match its event identity")

    if len(installed_indexes) != 1:
        errors.append(
            f"request_installed transition count={len(installed_indexes)}"
        )
    if require_terminal and len(terminal_indexes) != 1:
        errors.append(
            f"request_terminal transition count={len(terminal_indexes)}"
        )
    elif not require_terminal and len(terminal_indexes) > 1:
        errors.append(
            f"request_terminal transition count={len(terminal_indexes)}"
        )
    if len(installed_indexes) == 1 and installed_indexes[0] != 0:
        errors.append("request_installed is not the first control transition")
    if len(terminal_indexes) == 1 and terminal_indexes[0] != len(rows) - 1:
        errors.append("request_terminal is not the last control transition")
    if (
        len(installed_indexes) == 1
        and len(terminal_indexes) == 1
        and terminal_indexes[0] <= installed_indexes[0]
    ):
        errors.append("request_terminal does not follow request_installed")
    return tuple(errors)


def stable_event_id(request_id: str, kind: str, external_identity: str) -> str:
    values = (request_id, kind, external_identity)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("stable event identity fields must be non-empty strings")
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


@dataclass(frozen=True)
class RequestEvent:
    record_id: str
    event_id: str
    kind: str
    phase: str
    content: str
    projected: bool
    projection_key: str
    metadata: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "event_id": self.event_id,
            "kind": self.kind,
            "phase": self.phase,
            "content": self.content,
            "projected": self.projected,
            "projection_key": self.projection_key,
            "metadata": dict(self.metadata),
        }


class RequestEventLedger:
    """One request's immutable event records; mutations only append."""

    def __init__(self, request_id: str):
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        self.request_id = request_id
        self._events: list[RequestEvent] = []

    @property
    def events(self) -> tuple[RequestEvent, ...]:
        return tuple(self._events)

    def actual_events(
        self,
        *,
        event_id: str = "",
        kind: str = "",
        phase: str = "",
    ) -> tuple[RequestEvent, ...]:
        return tuple(
            event
            for event in self._events
            if not event.projected
            and (not event_id or event.event_id == event_id)
            and (not kind or event.kind == kind)
            and (not phase or event.phase == phase)
        )

    def _append(
        self,
        *,
        event_id: str,
        kind: str,
        phase: str,
        content: str = "",
        projected: bool = False,
        projection_key: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RequestEvent:
        if any(
            not isinstance(value, str) or not value
            for value in (event_id, kind, phase)
        ):
            raise ValueError("event identity, kind, and phase must be non-empty strings")
        if not isinstance(content, str) or not isinstance(projection_key, str):
            raise ValueError("event content and projection key must be strings")
        normalized_metadata = tuple(sorted(
            (str(key), str(value))
            for key, value in (metadata or {}).items()
        ))
        sequence = len(self._events) + 1
        record_id = hashlib.sha256(
            repr({
                "content": content,
                "event_id": event_id,
                "kind": kind,
                "metadata": normalized_metadata,
                "phase": phase,
                "projected": projected,
                "projection_key": projection_key,
                "request_id": self.request_id,
                "sequence": sequence,
            }).encode("utf-8")
        ).hexdigest()
        event = RequestEvent(
            record_id=record_id,
            event_id=event_id,
            kind=kind,
            phase=phase,
            content=content,
            projected=projected,
            projection_key=projection_key,
            metadata=normalized_metadata,
        )
        self._events.append(event)
        return event

    def record(
        self,
        *,
        event_id: str,
        kind: str,
        phase: str,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RequestEvent:
        return self._append(
            event_id=event_id,
            kind=kind,
            phase=phase,
            content=content,
            metadata=metadata,
        )

    def record_once(
        self,
        *,
        event_id: str,
        kind: str,
        phase: str,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[RequestEvent, bool]:
        existing = self.actual_events(
            event_id=event_id,
            kind=kind,
            phase=phase,
        )
        if existing:
            return existing[0], False
        return (
            self.record(
                event_id=event_id,
                kind=kind,
                phase=phase,
                content=content,
                metadata=metadata,
            ),
            True,
        )

    def record_projection(
        self,
        *,
        event_id: str,
        kind: str,
        phase: str,
        projection_key: str,
    ) -> RequestEvent:
        return self._append(
            event_id=event_id,
            kind=kind,
            phase=phase,
            projected=True,
            projection_key=projection_key,
        )

    def phase_count(
        self,
        event_id: str,
        kind: str,
        phase: str,
        *,
        projected: bool,
    ) -> int:
        return sum(
            event.event_id == event_id
            and event.kind == kind
            and event.phase == phase
            and event.projected is projected
            for event in self._events
        )

    def should_project(self, event_id: str, kind: str, phase: str) -> bool:
        return (
            self.phase_count(event_id, kind, phase, projected=False) == 1
            and self.phase_count(event_id, kind, phase, projected=True) == 0
        )

    def open_lifecycles(self) -> tuple[tuple[str, str], ...]:
        opened: list[tuple[str, str]] = []
        for kind, start, terminals in (
            ("tool", "started", TOOL_TERMINAL_PHASES),
            ("delegation", "opened", DELEGATION_TERMINAL_PHASES),
        ):
            event_ids = {
                event.event_id
                for event in self._events
                if not event.projected and event.kind == kind
            }
            for event_id in sorted(event_ids):
                if (
                    self.phase_count(event_id, kind, start, projected=False)
                    and not any(
                        self.phase_count(event_id, kind, phase, projected=False)
                        for phase in terminals
                    )
                ):
                    opened.append((kind, event_id))
        return tuple(opened)

    def close_open(self, phase: str = "stopped") -> tuple[RequestEvent, ...]:
        if phase not in {"stopped", "cancelled", "error"}:
            raise ValueError("open lifecycle terminal phase is invalid")
        closed = []
        for kind, event_id in self.open_lifecycles():
            opened = self.actual_events(event_id=event_id, kind=kind)[0]
            event, created = self.record_once(
                event_id=event_id,
                kind=kind,
                phase=phase,
                metadata=dict(opened.metadata),
            )
            if created:
                closed.append(event)
        return tuple(closed)

    def reconcile(self, *, require_final: bool = True) -> dict[str, Any]:
        errors: list[str] = []
        actual = [event for event in self._events if not event.projected]
        lifecycle_ids = {(event.kind, event.event_id) for event in actual}
        for kind, event_id in sorted(lifecycle_ids):
            if kind == "tool":
                starts = self.phase_count(event_id, kind, "started", projected=False)
                terminals = sum(
                    self.phase_count(event_id, kind, phase, projected=False)
                    for phase in TOOL_TERMINAL_PHASES
                )
                if starts != 1:
                    errors.append(f"{event_id}: tool started count={starts}")
                if terminals != 1:
                    errors.append(f"{event_id}: tool terminal count={terminals}")
                if starts == terminals == 1:
                    start_index = next(
                        index
                        for index, event in enumerate(actual)
                        if event.event_id == event_id
                        and event.kind == kind
                        and event.phase == "started"
                    )
                    terminal_index = next(
                        index
                        for index, event in enumerate(actual)
                        if event.event_id == event_id
                        and event.kind == kind
                        and event.phase in TOOL_TERMINAL_PHASES
                    )
                    if terminal_index <= start_index:
                        errors.append(f"{event_id}: tool terminal precedes start")
            elif kind == "delegation":
                starts = self.phase_count(event_id, kind, "opened", projected=False)
                terminals = sum(
                    self.phase_count(event_id, kind, phase, projected=False)
                    for phase in DELEGATION_TERMINAL_PHASES
                )
                if starts != 1:
                    errors.append(f"{event_id}: delegation opened count={starts}")
                if terminals != 1:
                    errors.append(f"{event_id}: delegation terminal count={terminals}")
                if starts == terminals == 1:
                    start_index = next(
                        index
                        for index, event in enumerate(actual)
                        if event.event_id == event_id
                        and event.kind == kind
                        and event.phase == "opened"
                    )
                    terminal_index = next(
                        index
                        for index, event in enumerate(actual)
                        if event.event_id == event_id
                        and event.kind == kind
                        and event.phase in DELEGATION_TERMINAL_PHASES
                    )
                    if terminal_index <= start_index:
                        errors.append(
                            f"{event_id}: delegation terminal precedes open"
                        )
            elif kind == "final_response":
                emitted = self.phase_count(event_id, kind, "emitted", projected=False)
                if emitted != 1:
                    errors.append(f"{event_id}: final response count={emitted}")

        for event in actual:
            if event.kind not in {"tool", "delegation", "final_response"}:
                continue
            projections = self.phase_count(
                event.event_id,
                event.kind,
                event.phase,
                projected=True,
            )
            if projections != 1:
                errors.append(
                    f"{event.event_id}:{event.phase}: projection count={projections}"
                )
            elif next(
                index
                for index, candidate in enumerate(self._events)
                if candidate.projected
                and candidate.event_id == event.event_id
                and candidate.kind == event.kind
                and candidate.phase == event.phase
            ) <= self._events.index(event):
                errors.append(
                    f"{event.event_id}:{event.phase}: projection precedes evidence"
                )
        for projection in (
            event for event in self._events if event.projected
        ):
            matches = [
                event
                for event in actual
                if event.event_id == projection.event_id
                and event.kind == projection.kind
                and event.phase == projection.phase
            ]
            if len(matches) != 1:
                errors.append(
                    f"{projection.event_id}:{projection.phase}: "
                    f"evidence count={len(matches)}"
                )
            if (
                projection.kind in {"tool", "delegation"}
                and projection.projection_key != f"event:{projection.event_id}"
            ):
                errors.append(
                    f"{projection.event_id}:{projection.phase}: "
                    "projection key does not use the evidence event id"
                )
        if not any(event.kind == "operator_input" for event in actual):
            errors.append("operator input is missing")
        if not any(event.kind == "control_transition" for event in actual):
            errors.append("typed control transition is missing")
        errors.extend(
            control_transition_errors(
                [
                    {
                        "event_id": event.event_id,
                        "kind": event.kind,
                        "phase": event.phase,
                        "content": event.content,
                    }
                    for event in actual
                    if event.kind == "control_transition"
                ],
                require_terminal=require_final,
            )
        )
        final_count = sum(
            event.kind == "final_response" and event.phase == "emitted"
            for event in actual
        )
        if require_final and final_count != 1:
            errors.append(f"request final response count={final_count}")
        if require_final and final_count == 1:
            terminal_controls = [
                event
                for event in actual
                if event.kind == "control_transition"
                and event.phase == "request_terminal"
            ]
            if len(terminal_controls) != 1:
                errors.append(
                    f"request terminal transition count={len(terminal_controls)}"
                )
            else:
                final_event = next(
                    event
                    for event in actual
                    if event.kind == "final_response"
                    and event.phase == "emitted"
                )
                if actual.index(final_event) <= actual.index(terminal_controls[0]):
                    errors.append(
                        "final response precedes request terminal transition"
                    )
        return {
            "ok": not errors,
            "request_id": self.request_id,
            "event_count": len(actual),
            "projection_count": len(self._events) - len(actual),
            "open": list(self.open_lifecycles()),
            "errors": errors,
        }

    def reconstruct_transcript(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for event in self._events:
            if event.projected:
                continue
            if event.kind not in {
                "operator_input",
                "control_transition",
                "tool",
                "delegation",
                "final_response",
            }:
                continue
            rows.append({
                "event_id": event.event_id,
                "kind": event.kind,
                "phase": event.phase,
                "content": event.content,
            })
        return rows


def event_ids(events: Iterable[RequestEvent]) -> tuple[str, ...]:
    return tuple(event.event_id for event in events)
