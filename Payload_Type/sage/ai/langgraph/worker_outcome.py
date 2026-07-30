"""Typed worker-outcome contract for supervisor/worker control-state (P0 of the control-state reprioritization).

THE PROBLEM (the `1116` 461K-token loop): a worker's bounded-stop is a LOCAL stop, invisible to the supervisor.
The supervisor reads "worker did not act" as "delegate again" and re-issues the identical instruction 48 times.
Both components behave correctly in isolation and form a global infinite loop.

THE CONTRACT: a worker returns a typed, serializable `WorkerOutcome` that the supervisor consumes to make a
deterministic routing decision. "Worker is blocked on X" becomes SHARED control state carrying a blocker
identity, an invalidation condition (what must change for it to clear), and a routing target (the prerequisite
capability / next owner). The supervisor then distinguishes retry-worker / route-to-owner / stop instead of
blindly re-delegating.

This module is PURE and OFFLINE — no Sage runtime imports, no live range, no I/O. The langgraph supervisor and
worker wire into it in a later step; this is the testable contract + decision logic first.

Design notes baked in from the Codex/Grok reviews of the reprioritization plan:
  - Outcome typing is a VERSIONED contract (CONTRACT_VERSION), serialized across the agent boundary — not an
    internal enum. A future field add bumps the version.
  - The fingerprint is designed WITH the revision model (not after): a fingerprint is
    `normalized_action + observed_state_revision + impl_version`. Suppression keys on all three, so a legitimate
    retry after creds/graph/tool-version change is a NEW fingerprint and is NOT over-suppressed.
  - The decision routes to a prerequisite/owner to ADVANCE work, not only to STOP — addressing the active-run
    Mythic→BloodHound failure where "graph analysis required next" was answered by re-delegating the broad
    objective (four identical SharpHound ingests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Any

CONTRACT_VERSION = 1
HANDOFF_METADATA_VERSION = 3
KNOWN_WORKERS = frozenset({
    "Generalist",
    "Mythic_Operator",
    "Mythic_Payload",
    "BloodHound",
    "MCP_Manager",
    "Sandbox",
    "Autonomous_Executor",
})


class Outcome(str, Enum):
    PROGRESS = "progress"     # forward progress was made; normal continue
    BLOCKED = "blocked"       # cannot proceed until an invalidation condition changes
    COMPLETE = "complete"     # the objective / sub-objective is achieved
    HANDOFF = "handoff"       # needs a different owner/capability to proceed


class Decision(str, Enum):
    PROCEED = "proceed"               # progress/complete -> continue normally
    RETRY_WORKER = "retry_worker"     # new blocker or state changed -> one admissible (re)attempt
    ROUTE_TO_OWNER = "route_to_owner" # blocked/handoff with a prerequisite/owner -> advance the work there
    STOP = "stop"                     # same blocker at same state, no route -> terminal (kills the 1116 loop)


@dataclass(frozen=True)
class Blocker:
    fingerprint: str             # stable identity of THIS blocker (e.g. capability + missing prerequisite)
    invalidation_condition: str  # what must change for the blocker to clear (the durable "until X" fact)


@dataclass(frozen=True)
class WorkerOutcome:
    outcome: Outcome
    state_revision: str               # the observed-state / evidence revision that produced this outcome
    impl_version: str = ""            # capability/tool impl version (a fix invalidates a stale fingerprint)
    blocker: Blocker | None = None    # present when outcome == BLOCKED
    next_capability: str = ""         # the prerequisite/next capability to pursue (route target)
    next_owner: str = ""              # the agent/owner that must act next (routing, not just "stop")
    note: str = ""

    def to_dict(self) -> dict:
        d = {
            "contract_version": CONTRACT_VERSION,
            "outcome": self.outcome.value,
            "state_revision": self.state_revision,
            "impl_version": self.impl_version,
            "next_capability": self.next_capability,
            "next_owner": self.next_owner,
            "note": self.note,
        }
        if self.blocker is not None:
            d["blocker"] = {
                "fingerprint": self.blocker.fingerprint,
                "invalidation_condition": self.blocker.invalidation_condition,
            }
        return d

    @staticmethod
    def from_dict(d: dict) -> "WorkerOutcome":
        b = d.get("blocker")
        return WorkerOutcome(
            outcome=Outcome(d["outcome"]),
            state_revision=str(d.get("state_revision", "")),
            impl_version=str(d.get("impl_version", "")),
            blocker=Blocker(str(b["fingerprint"]), str(b.get("invalidation_condition", ""))) if b else None,
            next_capability=str(d.get("next_capability", "")),
            next_owner=str(d.get("next_owner", "")),
            note=str(d.get("note", "")),
        )


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def action_fingerprint(action: str, state_revision: str, impl_version: str = "") -> str:
    """Stable key over a normalized action + the observed-state revision it ran against + the impl version.

    Two attempts are 'the same' (suppressible) ONLY when all three match. A changed `state_revision` (new creds,
    fresh graph, new callback context) or a bumped `impl_version` (a capability fix) yields a NEW fingerprint, so
    a legitimate retry is never suppressed (the Codex §4 over-suppression guard)."""
    return f"{_norm(action)}|rev={state_revision}|impl={impl_version}"


def supervisor_decision(history: list[WorkerOutcome], new: WorkerOutcome) -> Decision:
    """Deterministic supervisor decision over the outcome history + the newest worker outcome.

    Core anti-loop rule: after the SAME blocker fingerprint has already been seen at the SAME state revision, the
    supervisor must NOT re-delegate — it STOPs. That single rule makes the 1116 461K-token redelegation tail
    impossible. Before that, a blocked worker with a prerequisite/owner is ROUTED (advance the work); a
    first-seen blocker with no route gets one admissible retry; progress/complete proceed.
    """
    if new.outcome in (Outcome.PROGRESS, Outcome.COMPLETE):
        return Decision.PROCEED

    fp = new.blocker.fingerprint if new.blocker is not None else None
    repeated_same_state = fp is not None and any(
        o.blocker is not None
        and o.blocker.fingerprint == fp
        and o.state_revision == new.state_revision
        for o in history
    )
    if repeated_same_state:
        # We have already acted on this exact blocker at this exact state and it came back unchanged.
        # Re-delegating is the loop. Stop (a terminal blocker), regardless of any route that didn't help.
        return Decision.STOP

    if new.next_capability or new.next_owner:
        return Decision.ROUTE_TO_OWNER   # first sight: advance to the prerequisite/owner, don't re-delegate blindly

    if new.outcome == Outcome.BLOCKED:
        return Decision.RETRY_WORKER     # first sight of this blocker, no route -> one admissible attempt

    return Decision.STOP                 # handoff with no target -> nothing to route to


def outcome_from_capability_result(capability: str, result: dict, state_revision: str) -> WorkerOutcome:
    """Map an `execute_capability` tool result into a typed outcome.

    A failed/blocked capability (`ok` is False) becomes a BLOCKED outcome whose blocker fingerprint is keyed on
    (capability, reason) — INVARIANT to how the supervisor paraphrases the delegation. That invariance is the
    whole point: the old action-signature stall detector keyed on the supervisor's wording, so 48 reworded
    re-delegations of the same blocked action never tripped it. `state_revision` is the caller's progress epoch,
    so a blocker only counts as 'repeated at the same state' until real progress is made (the over-suppression
    guard). A structured prerequisite field (`run_first` / `next_capability` / `prerequisite`) becomes the route
    target; free-text reasons are NOT parsed (kept generic / range-agnostic)."""
    ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
    if ok:
        return WorkerOutcome(Outcome.PROGRESS, state_revision)
    reason = next_cap = ""
    if isinstance(result, dict):
        reason = str(result.get("reason") or result.get("error") or result.get("verdict") or "")
        # `suggested_capability` is the real prerequisite-hint key emitted by execute_capability / the adapter
        # (e.g. {"suggested_capability": "adcs-ca-private-key-export"} on a blocked adcs-certificate-auth);
        # `run_first`/`next_capability`/`prerequisite` are accepted as generic synonyms.
        next_cap = str(result.get("suggested_capability") or result.get("run_first")
                       or result.get("next_capability") or result.get("prerequisite") or "")
        capability = capability or str(result.get("capability") or "")
    fingerprint = f"{_norm(capability)}::{_norm(reason)}"
    return WorkerOutcome(Outcome.BLOCKED, state_revision,
                         blocker=Blocker(fingerprint, reason), next_capability=next_cap)


def decide_capability_outcome(history: list[WorkerOutcome], capability: str, result: dict,
                              progress_epoch: int) -> tuple[WorkerOutcome, Decision]:
    """Pure: build the typed outcome from a capability result + decide. The state wrapper manages the epoch and
    appends to history; this keeps the loop-breaking decision fully unit-testable without the agent runtime."""
    outcome = outcome_from_capability_result(capability, result, str(progress_epoch))
    return outcome, supervisor_decision(history, outcome)


@dataclass
class LoopBreakerState:
    """Per-solve state for the supervisor/worker loop-breaker. The Model holds one of these and RE-CREATES it
    each solve (a Sage session reuses one Model, so a prior objective's blockers must never leak into the next)."""
    outcomes: list = field(default_factory=list)
    progress_epoch: int = 0
    last_turn_key: str = ""


def worker_evidence_records(messages: list[Any]) -> tuple[str, ...]:
    """Return unique semantic tool observations without provider invocation identity."""
    records: set[str] = set()
    for message in messages:
        if getattr(message, "type", "") != "tool":
            continue
        name = str(getattr(message, "name", "") or "")
        if (
            not name
            or name.startswith("transfer_to_")
            or name in {
                "handback_to_supervisor",
                "summarize_and_handback",
                "request_continuation",
                "respond_to_user",
            }
        ):
            continue
        records.add(repr({
            "content": str(getattr(message, "content", "") or ""),
            "name": name,
        }))
    return tuple(sorted(records))


def evidence_revision(records: Any) -> str:
    """Digest a cumulative set of canonical semantic observations."""
    normalized = tuple(sorted({
        str(record)
        for record in records
        if isinstance(record, str) and record
    }))
    if not normalized:
        return ""
    return hashlib.sha256(repr(normalized).encode("utf-8")).hexdigest()


def worker_evidence_revision(messages: list[Any]) -> str:
    """Digest semantic non-control results; retries and prose cannot advance revision."""
    records = worker_evidence_records(messages)
    if not records:
        return ""
    return evidence_revision(records)


def current_operator_turn(messages: list[Any]) -> list[Any]:
    """Return only messages after the latest exact real-operator boundary."""
    latest_operator = -1
    for index, message in enumerate(messages):
        kwargs = getattr(message, "additional_kwargs", {}) or {}
        if (
            getattr(message, "type", "") == "human"
            and not kwargs.get("_hide_from_stream")
            and not kwargs.get("_delegated_to")
        ):
            latest_operator = index
    return list(messages[latest_operator + 1 :])


def current_turn_evidence_records(messages: list[Any]) -> tuple[str, ...]:
    """Return semantic evidence admitted only after the current operator boundary."""
    return worker_evidence_records(current_operator_turn(messages))


def build_handoff_metadata(
    *,
    source_worker: str,
    source_turn_id: str,
    source_seq: int,
    reason: str,
    summary: str,
    outcome: Any,
    next_owner: Any = "",
    verified_revision: Any = "",
) -> dict[str, Any] | None:
    if source_worker not in KNOWN_WORKERS or not source_turn_id or source_seq <= 0:
        return None
    try:
        typed_outcome = Outcome(outcome)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(next_owner, str)
        or next_owner not in {*KNOWN_WORKERS, ""}
        or next_owner == source_worker
        or not isinstance(verified_revision, str)
    ):
        return None
    if (
        (typed_outcome == Outcome.HANDOFF and not next_owner)
        or (typed_outcome != Outcome.HANDOFF and bool(next_owner))
    ):
        return None
    summary_digest = hashlib.sha256(str(summary).encode("utf-8")).hexdigest()
    outcome_id = hashlib.sha256(
        repr({
            "next_owner": next_owner,
            "outcome": typed_outcome.value,
            "source_seq": source_seq,
            "source_turn_id": source_turn_id,
            "source_worker": source_worker,
            "verified_revision": verified_revision,
        }).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": HANDOFF_METADATA_VERSION,
        "source_worker": source_worker,
        "source_turn_id": source_turn_id,
        "source_seq": source_seq,
        "outcome_id": outcome_id,
        "outcome": typed_outcome.value,
        "next_owner": next_owner,
        "verified_revision": verified_revision,
        "summary_digest": summary_digest,
    }
def latest_admitted_handoff(messages: list[Any], current_turn_id: str) -> tuple[dict[str, Any], str] | None:
    turn_messages = current_operator_turn(messages)
    latest_operator = len(messages) - len(turn_messages) - 1
    candidate: tuple[dict[str, Any], str, int] | None = None
    for index, message in enumerate(turn_messages, start=latest_operator + 1):
        kwargs = getattr(message, "additional_kwargs", {}) or {}
        metadata = kwargs.get("_worker_outcome")
        if not isinstance(metadata, dict):
            continue
        candidate = (metadata, str(getattr(message, "content", "") or ""), index)
    if candidate is None:
        return None
    metadata, summary, index = candidate
    required = {
        "schema_version",
        "source_worker",
        "source_turn_id",
        "source_seq",
        "outcome_id",
        "outcome",
        "next_owner",
        "verified_revision",
        "summary_digest",
    }
    if set(metadata) != required:
        return None
    if (
        metadata.get("schema_version") != HANDOFF_METADATA_VERSION
        or metadata.get("source_worker") not in KNOWN_WORKERS
        or metadata.get("source_turn_id") != current_turn_id
        or metadata.get("outcome") not in {item.value for item in Outcome}
        or metadata.get("next_owner") not in {*KNOWN_WORKERS, ""}
        or not isinstance(metadata.get("outcome_id"), str)
        or len(metadata.get("outcome_id", "")) != 64
        or not isinstance(metadata.get("verified_revision"), str)
        or not isinstance(metadata.get("source_seq"), int)
        or metadata.get("source_seq", 0) <= 0
        or metadata.get("summary_digest") != hashlib.sha256(summary.encode("utf-8")).hexdigest()
    ):
        return None
    for later in messages[index + 1 :]:
        kwargs = getattr(later, "additional_kwargs", {}) or {}
        if isinstance(kwargs.get("_worker_outcome"), dict):
            return None
        if getattr(later, "name", "") in KNOWN_WORKERS and not kwargs.get("_is_completion_header"):
            return None
    return metadata, summary


def observe_capability_outcome(state: LoopBreakerState, capability: str, result: dict, turn_key: str,
                               progressed: bool | None = None) -> bool:
    """Pure transition (mutates `state`); returns True iff the solve should HALT (the same blocker recurred with
    no progress). Dedup rules are baked in here so they are unit-tested, not buried in agent glue:
      - an EMPTY `turn_key` is NEVER counted — an unkeyed observation cannot be safely deduped, so counting it
        could manufacture a same-turn 'repeat' and falsely halt; skip it entirely;
      - a `turn_key` equal to the previous one is a same-worker-turn re-fire -> ignored;
      - a fresh `turn_key` (a genuine re-delegation) is counted.

    PROGRESS vs OUTCOME are decoupled: the progress EPOCH advances on `progressed` when the caller supplies it
    (a VERIFIED state change — e.g. the controller's expected-effect diff), otherwise it falls back to the
    self-reported `result.ok`. The OUTCOME classification (PROGRESS/BLOCKED + blocker fingerprint) is ALWAYS
    derived from the real `result`, never overwritten — so a slow/idempotent real success is not mislabeled a
    terminal blocker, and a self-reported success with no verified effect does not advance the state revision
    that the over-suppression guard keys on."""
    if not turn_key or turn_key == state.last_turn_key:
        return False
    state.last_turn_key = turn_key
    made_progress = progressed if progressed is not None else (isinstance(result, dict) and bool(result.get("ok", True)))
    if made_progress:
        state.progress_epoch += 1
    outcome, decision = decide_capability_outcome(state.outcomes, capability, result, state.progress_epoch)
    state.outcomes.append(outcome)
    return decision == Decision.STOP
