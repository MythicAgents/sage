"""Per-request capability attempt memory.

The engagement ledger records progress only. A capability attempt that did not achieve its effect is remembered
here instead, for the rest of the logical request that made it, and nowhere else:

- The memory is keyed by the logical request ID of the bound request contract. When the current request ID
  differs from the memory's ID, the memory is emptied before the next read or write. A contract digest change
  under the same request ID keeps it.
- An unbound session (``request:unbound``) has its own memory and never inherits a bound request's attempts.
- It is never written to the engagement ledger, sealed decision records, or another channel. A recovered auto run
  restores it from the controller recovery journal for the same original request ID only.

A ``terminal`` attempt suppresses its effects for the rest of the request. ``construction`` and ``transient``
attempts stay retryable within the existing bounded retry controls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

UNBOUND_REQUEST_ID = "request:unbound"
FAILURE_CLASSES = ("terminal", "construction", "transient")
PAYLOAD_SCHEMA = "sage.request-attempts.v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def failure_class_from_evidence(evidence: Mapping[str, Any] | None) -> str:
    """Map recorded failure evidence to an attempt class.

    An attempt is terminal unless its evidence explicitly marks it non-terminal (``terminal_failure: False``), which
    is today's meaning of a failed or blocked capability row. A non-terminal attempt keeps its construction or
    transient class.
    """
    data = evidence if isinstance(evidence, Mapping) else {}
    if data.get("terminal_failure") is not False:
        return "terminal"
    declared = _text(data.get("failure_class")).casefold()
    return declared if declared in {"construction", "transient"} else "transient"


@dataclass(frozen=True)
class Attempt:
    """One capability attempt in the current request that did not achieve its effect."""

    technique: str
    target: str
    effect: str
    satisfied_effects: tuple[str, ...] = ()
    failure_class: str = "terminal"
    verdict: str = "failed"
    evidence: dict = field(default_factory=dict)
    task_id: str = ""
    callback_id: str = ""

    def __post_init__(self) -> None:
        if self.failure_class not in FAILURE_CLASSES:
            raise ValueError(f"invalid attempt failure class: {self.failure_class!r}")

    @property
    def terminal(self) -> bool:
        return self.failure_class == "terminal"

    def effects(self) -> tuple[str, ...]:
        seen: list[str] = []
        for item in (self.effect, *self.satisfied_effects):
            text = _text(item)
            if text and text not in seen:
                seen.append(text)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "technique": self.technique,
            "target": self.target,
            "effect": self.effect,
            "satisfied_effects": list(self.satisfied_effects),
            "failure_class": self.failure_class,
            "verdict": self.verdict,
            "evidence": dict(self.evidence),
            "task_id": self.task_id,
            "callback_id": self.callback_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Attempt":
        if not isinstance(data, Mapping):
            raise ValueError("attempt record must be a mapping")
        return cls(
            technique=_text(data.get("technique")),
            target=_text(data.get("target")),
            effect=_text(data.get("effect")),
            satisfied_effects=tuple(_text(item) for item in (data.get("satisfied_effects") or []) if _text(item)),
            failure_class=_text(data.get("failure_class")) or "terminal",
            verdict=_text(data.get("verdict")) or "failed",
            evidence=dict(data.get("evidence") or {}) if isinstance(data.get("evidence"), Mapping) else {},
            task_id=_text(data.get("task_id")),
            callback_id=_text(data.get("callback_id")),
        )


def make_attempt(
    *,
    technique: Any,
    target: Any,
    effect: Any,
    satisfied_effects: Iterable[Any] | None = None,
    verdict: Any = "failed",
    evidence: Mapping[str, Any] | None = None,
    task_id: Any = None,
    callback_id: Any = None,
) -> Attempt:
    """Build an attempt from the evidence a failure writer already produced."""
    data = dict(evidence) if isinstance(evidence, Mapping) else {}
    return Attempt(
        technique=_text(technique),
        target=_text(target),
        effect=_text(effect),
        satisfied_effects=tuple(_text(item) for item in (satisfied_effects or []) if _text(item)),
        failure_class=failure_class_from_evidence(data),
        verdict=_text(verdict).casefold() or "failed",
        evidence=data,
        task_id=_text(task_id or data.get("mythic_task_id") or data.get("task_id")),
        callback_id=_text(callback_id or data.get("callback_id") or data.get("callback_display_id")),
    )


class RequestAttemptMemory:
    """Attempts made under one logical request ID. Emptied whenever the current request ID changes."""

    def __init__(self) -> None:
        self._request_id = ""
        self._attempts: list[Attempt] = []

    @property
    def request_id(self) -> str:
        return self._request_id

    def _sync(self, request_id: str) -> None:
        current = _text(request_id) or UNBOUND_REQUEST_ID
        if current != self._request_id:
            self._request_id = current
            self._attempts = []

    def record(self, request_id: str, attempt: Attempt) -> None:
        # Duck-typed: the module can be imported both as a package member and flat (tests, scripts).
        if not callable(getattr(attempt, "to_dict", None)) or not callable(getattr(attempt, "effects", None)):
            raise TypeError("request attempt memory records Attempt values")
        self._sync(request_id)
        self._attempts.append(attempt)

    def view(self, request_id: str) -> tuple[Attempt, ...]:
        self._sync(request_id)
        return tuple(self._attempts)

    def to_payload(self, request_id: str) -> dict[str, Any]:
        self._sync(request_id)
        return {
            "schema": PAYLOAD_SCHEMA,
            "request_id": self._request_id,
            "attempts": [attempt.to_dict() for attempt in self._attempts],
        }

    def restore(self, payload: Mapping[str, Any], request_id: str) -> None:
        """Replace the memory with a journal payload saved for exactly ``request_id``."""
        current = _text(request_id) or UNBOUND_REQUEST_ID
        if not isinstance(payload, Mapping) or payload.get("schema") != PAYLOAD_SCHEMA:
            raise ValueError("request attempt payload has an unsupported schema")
        saved = _text(payload.get("request_id"))
        if saved != current:
            raise ValueError(f"request attempt payload belongs to {saved or 'no request'}, not {current}")
        attempts = [Attempt.from_dict(item) for item in list(payload.get("attempts") or [])]
        self._request_id = current
        self._attempts = attempts


def with_attempts(state: Any, *attempts: Attempt) -> Any:
    """A copy of an EngagementState-shaped dataclass with ``attempts`` added to its request attempt view.

    For offline evaluation fixtures that model a blocked step: the runtime gets the view from the session.
    """
    import dataclasses

    return dataclasses.replace(
        state, request_attempts=(*tuple(getattr(state, "request_attempts", ()) or ()), *attempts)
    )


def terminal_effects(attempts: Iterable[Attempt] | None) -> set[str]:
    """Raw effect strings of the terminal attempts (callers canonicalize them with their own effect rule)."""
    out: set[str] = set()
    for attempt in attempts or ():
        if getattr(attempt, "terminal", False) and callable(getattr(attempt, "effects", None)):
            out.update(attempt.effects())
    return out
