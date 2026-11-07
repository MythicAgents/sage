"""One definition of a Mythic task's terminal state and one record per canonical effect.

Every ledger writer that credits a task (the raw issuer, a capability, task-history reconciliation and the
credential-store observer) classifies the task with :func:`classify_task` from Mythic's own task record, and
records what it saw as an *observation* on one contribution: the pair (Mythic task display ID, canonical effect).
A row keyed by a canonical effect holds every contribution; its status and satisfied effects are derived from the
contributions whose proof the runtime proof boundary admits. Writers enrich the row; none replaces another's
evidence, and the store merges a writer's row against the stored head inside its transaction.

Mythic keeps one ``status`` field, overwritten at every stage, so the agent's own outcome is not recoverable from
it once a completion function ran. The classifier therefore never infers agent success from a final status; it
uses Mythic's status constants and the once-set stage timestamps.

Pure stdlib plus the proof boundary. No evaluation, range or operator package is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

try:
    from . import proof_boundary
except ImportError:  # pragma: no cover - flat import for tests that put ai/langgraph on sys.path
    import proof_boundary  # type: ignore


# --- task classification ----------------------------------------------------------------------------------------

NOT_AGENT_PROCESSED = "not_agent_processed"
AGENT_ERROR_OR_FAILED = "agent_error_or_failed"
POSTPROCESSING_ERROR_WITH_OUTPUT = "postprocessing_error_with_output"
TERMINAL_SUCCESS = "terminal_success"
UNKNOWN = "unknown"
TASK_CLASSES = (NOT_AGENT_PROCESSED, AGENT_ERROR_OR_FAILED, POSTPROCESSING_ERROR_WITH_OUTPUT, TERMINAL_SUCCESS, UNKNOWN)

# Mythic's task status constants (mythic-docker/src/rabbitmq/structs.go, PT_TASK_FUNCTION_STATUS_*).
MYTHIC_COMPLETION_FUNCTION_ERROR = "Error: completion function - click cog to check stderr"
MYTHIC_COMPLETION_FUNCTION_RUNNING = "Completion Function Running..."
_MYTHIC_PRE_AGENT_ERRORS = frozenset(status.casefold() for status in (
    "Error: processing arguments",
    "OPSEC Pre Blocked",
    "Error: creating task",
    "Error: opsec check failed to run",
    "OPSEC Post Blocked",
    "Error: Task Interception Failed",
))
_MYTHIC_PRE_AGENT_STATES = frozenset(status.casefold() for status in (
    "OPSEC Pre Check Running...",
    "preparing task for agent...",
    "OPSEC Post Check Running...",
    "submitted",
    "intercepted for custom checks",
))
# Statuses Mythic shows while it is still finishing a task the agent already returned. A writer may wait briefly
# for these to settle; the classifier treats them as not terminal.
MYTHIC_SETTLING_STATES = frozenset(status.casefold() for status in (
    MYTHIC_COMPLETION_FUNCTION_RUNNING,
    "SubTask Completion Function Running...",
    "Group Completion Function Running...",
    "processed, agent sending responses...",
    "agent processing",
    "delegating tasks...",
))
# The final status Mythic writes for success (PT_TASK_FUNCTION_STATUS_COMPLETED) and the agent-written spelling.
_SUCCESS_STATES = frozenset({"success", "completed"})
_FAILURE_WORDS = ("error", "failed", "cancel")
MAX_STATUS_CHARS = 80


@dataclass(frozen=True)
class TaskRecord:
    """The Mythic task metadata the classifier reads. ``has_output`` is whether any response row exists."""

    task_id: str
    callback_id: str = ""
    operation_id: str = ""
    completed: bool | None = None
    status: str = ""
    processing_at: str = ""
    processed_at: str = ""
    has_output: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "callback_id": self.callback_id,
            "operation_id": self.operation_id,
            "completed": self.completed,
            "status": self.status[:MAX_STATUS_CHARS],
            "processing_at": self.processing_at,
            "processed_at": self.processed_at,
            "has_output": self.has_output,
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def task_record_from_row(row: Mapping[str, Any] | None, *, has_output: bool | None = None) -> TaskRecord | None:
    """Normalize one Mythic ``task`` row (GraphQL field names) into a :class:`TaskRecord`."""
    if not isinstance(row, Mapping):
        return None
    task_id = _text(row.get("display_id") if row.get("display_id") is not None else row.get("task_id"))
    if not task_id:
        return None
    callback = row.get("callback") if isinstance(row.get("callback"), Mapping) else {}
    callback_id = _text(callback.get("display_id") if callback else row.get("callback_id"))
    if has_output is None:
        responses = row.get("responses")
        if isinstance(responses, list):
            has_output = bool(responses)
        elif isinstance(row.get("has_output"), bool):
            has_output = row.get("has_output")
    completed = row.get("completed")
    return TaskRecord(
        task_id=task_id,
        callback_id=callback_id,
        operation_id=_text(row.get("operation_id")),
        completed=completed if isinstance(completed, bool) else None,
        status=_text(row.get("status")),
        processing_at=_text(row.get("status_timestamp_processing") or row.get("processing_at")),
        processed_at=_text(row.get("status_timestamp_processed") or row.get("processed_at")),
        has_output=has_output,
    )


def classify_task(record: TaskRecord | Mapping[str, Any] | None) -> str:
    """Classify one Mythic task from its record. Never infers the agent's outcome from an overwritten status.

    * ``not_agent_processed``: Mythic refused or never handed the task to an agent.
    * ``agent_error_or_failed``: a terminal error or failure status other than the completion-function error.
    * ``postprocessing_error_with_output``: the agent processed the task and returned output, and only Mythic's
      completion function failed afterwards. Reviewable, never achieved from output alone.
    * ``terminal_success``: Mythic's final success status on a task the agent processed and answered.
    * ``unknown``: not terminal, or the record is missing or inconsistent (for example a completed task with no
      agent-processing timestamp, or a completion-function error with no output).
    """
    if isinstance(record, Mapping):
        record = task_record_from_row(record)
    if not isinstance(record, TaskRecord):
        return UNKNOWN
    status = record.status.strip()
    folded = status.casefold()
    if folded in _MYTHIC_PRE_AGENT_ERRORS:
        return NOT_AGENT_PROCESSED
    if not record.processing_at:
        if folded in _MYTHIC_PRE_AGENT_STATES or (record.completed is not True and not folded):
            return NOT_AGENT_PROCESSED
        return UNKNOWN
    if record.completed is not True or folded in MYTHIC_SETTLING_STATES:
        return UNKNOWN
    if status == MYTHIC_COMPLETION_FUNCTION_ERROR:
        if record.processed_at and record.has_output is True:
            return POSTPROCESSING_ERROR_WITH_OUTPUT
        return UNKNOWN
    if folded in _SUCCESS_STATES:
        return TERMINAL_SUCCESS if record.processed_at else UNKNOWN
    if any(word in folded for word in _FAILURE_WORDS):
        return AGENT_ERROR_OR_FAILED
    return UNKNOWN


def envelope_terminal_status(classification: str, record: TaskRecord | None) -> str:
    """The terminal status a proof envelope may carry for this task.

    Only ``terminal_success`` yields an admissible status (Mythic's own success word). Every other class yields its
    class name, which the proof boundary does not accept as terminal success.
    """
    if classification == TERMINAL_SUCCESS and record is not None:
        folded = record.status.strip().casefold()
        return folded if folded in _SUCCESS_STATES else "success"
    return classification or UNKNOWN


def is_settling(record: TaskRecord | None) -> bool:
    """Whether Mythic is still finishing a task the agent returned (a short wait may yield the final status)."""
    if record is None:
        return False
    return record.completed is not True or record.status.strip().casefold() in MYTHIC_SETTLING_STATES


# --- canonical effect identity ----------------------------------------------------------------------------------

ACCOUNT_SECRET = "account-secret"
STRENGTH_WORKING = "working"
STRENGTH_MATERIAL = "material"
_STRENGTH_RANK = {STRENGTH_WORKING: 2, STRENGTH_MATERIAL: 1}

_REALM_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$")
_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9._$-]*$")
_MATERIAL_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class CanonicalEffect:
    """A typed effect. ``identity`` keys its ledger row; ``technique``/``target`` are the row's display label."""

    kind: str
    account: str
    realm: str
    strength: str
    material_kind: str = ""

    @property
    def subject(self) -> str:
        return f"{self.account}@{self.realm}"

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.subject}"

    @property
    def technique(self) -> str:
        return self.kind

    @property
    def target(self) -> str:
        return self.subject


def _account_and_realm(principal: str, realm: str) -> tuple[str, str] | None:
    """Structured account and realm. A ``NETBIOS\\`` prefix is dropped only when it names the realm's first label;
    any other prefix is ambiguous and yields None."""
    realm = realm.strip().casefold()
    account = principal.strip().casefold()
    if not _REALM_RE.fullmatch(realm):
        return None
    if "\\" in account:
        prefix, _, rest = account.partition("\\")
        if prefix != realm.split(".", 1)[0] or "\\" in rest:
            return None
        account = rest
    if not account or not _ACCOUNT_RE.fullmatch(account):
        return None
    return account, realm


def canonical_effect(effect: Any) -> CanonicalEffect | None:
    """The typed identity of an effect, or None when the effect has no registered type.

    Registered: an account's secret, in any of its namespaces: ``creds:<account>@<realm>`` and
    ``krbtgt-hash:<realm>`` (a working credential) and ``cred-material:<kind>:<account>@<realm>`` (material).
    Anything else, including host-scoped material such as ``cred-material:dump:<host>``, is untyped and keeps its
    writer's row identity.
    """
    text = " ".join(_text(effect).casefold().split())
    prefix, sep, rest = text.partition(":")
    if not sep or not rest:
        return None
    if prefix == "creds":
        principal, at, realm = rest.rpartition("@")
        parsed = _account_and_realm(principal, realm) if at else None
        return CanonicalEffect(ACCOUNT_SECRET, parsed[0], parsed[1], STRENGTH_WORKING) if parsed else None
    if prefix == "krbtgt-hash":
        parsed = _account_and_realm("krbtgt", rest)
        return CanonicalEffect(ACCOUNT_SECRET, parsed[0], parsed[1], STRENGTH_WORKING) if parsed else None
    if prefix == "cred-material":
        kind, colon, subject = rest.partition(":")
        principal, at, realm = subject.rpartition("@")
        if not colon or not at or not _MATERIAL_KIND_RE.fullmatch(kind):
            return None
        parsed = _account_and_realm(principal, realm)
        return CanonicalEffect(ACCOUNT_SECRET, parsed[0], parsed[1], STRENGTH_MATERIAL, kind) if parsed else None
    return None


def canonical_from_identity(identity: Any) -> CanonicalEffect | None:
    """The typed effect a canonical row identity names (``account-secret:<account>@<realm>``)."""
    kind, sep, subject = _text(identity).casefold().partition(":")
    if kind != ACCOUNT_SECRET or not sep:
        return None
    principal, at, realm = subject.rpartition("@")
    parsed = _account_and_realm(principal, realm) if at else None
    return CanonicalEffect(ACCOUNT_SECRET, parsed[0], parsed[1], STRENGTH_WORKING) if parsed else None


def working_effect(canonical: CanonicalEffect) -> str:
    """The working-credential predicate of an account secret."""
    if canonical.account == "krbtgt":
        return f"krbtgt-hash:{canonical.realm}"
    return f"creds:{canonical.subject}"


def material_effect(canonical: CanonicalEffect, kind: str) -> str:
    return f"cred-material:{kind or 'unknown'}:{canonical.subject}"


# --- contributions ----------------------------------------------------------------------------------------------

_VOLATILE_PROOF_KEYS = frozenset({"captured_at", "persistence_state"})
_WRITER_ORDER = ("capability", "raw", "reconcile", "credential_store", "review", "operator")
_OBSERVATION_SLOTS = ("admitted", "rejected")
# Task output that rows stored before Phase 2 group D carried in evidence.
_OUTPUT_PREVIEW_KEYS = frozenset({"result_preview", "reconciled_setup_preview"})


def writer_kind(technique: str, evidence: Mapping[str, Any] | None) -> str:
    """Which writer produced an observation: one observation slot per writer on a contribution."""
    evidence = evidence if isinstance(evidence, Mapping) else {}
    technique = _text(technique).casefold()
    source = _text(evidence.get("source")).casefold()
    if source in {"credential_review", "operator_review"}:
        return "review" if source == "credential_review" else "operator"
    if technique.startswith("capability:"):
        return "capability"
    if technique in {"credential-store", "credential-material"} or evidence.get("credential_id") not in (None, ""):
        # Each credential-store entry is its own observation (one task can produce several, for example NTLM and
        # AES keys for one account).
        credential = _text(evidence.get("credential_id"))
        return f"credential_store:{credential}" if credential else "credential_store"
    if source == "task_history_reconcile":
        return "reconcile"
    if source == "issue_task":
        return "raw"
    return source or "writer"


def _task_key(evidence: Mapping[str, Any], proof: Mapping[str, Any]) -> str:
    return _text(proof.get("task_id") or evidence.get("mythic_task_id") or evidence.get("task_id"))


def _callback_key(evidence: Mapping[str, Any], proof: Mapping[str, Any]) -> str:
    return _text(proof.get("callback_id") or evidence.get("callback_id") or evidence.get("callback_display_id"))


def build_observation(
    *,
    technique: str,
    target: str,
    effect: str,
    satisfied_effects: Iterable[str],
    preconditions: Iterable[str],
    evidence: Mapping[str, Any],
    proof_envelope: Mapping[str, Any] | None,
    admitted: bool,
    admission_reason: str,
    source: str,
    timestamp: str,
    pending_reason: str = "",
) -> dict[str, Any]:
    """One writer's observation of one task and effect, as the reducer computed it."""
    canonical = canonical_effect(effect)
    strength = canonical.strength if canonical is not None else ""
    return {
        "technique": _text(technique),
        "target": _text(target),
        "effect": _text(effect),
        "strength": strength,
        "material_kind": canonical.material_kind if canonical is not None else "",
        "satisfied_effects": [str(item) for item in satisfied_effects or []],
        "preconditions": [str(item) for item in preconditions or []],
        "evidence": dict(evidence or {}),
        "proof_envelope": dict(proof_envelope or {}),
        "admitted": bool(admitted),
        "admission_reason": _text(admission_reason),
        "source": _text(source),
        "timestamp": _text(timestamp),
        "pending_reason": "" if admitted else _text(pending_reason),
    }


def legacy_contribution(payload: Mapping[str, Any], *, writer: str = "legacy") -> dict[str, Any] | None:
    """A row stored before typed rows existed, as one ``legacy`` observation of its task.

    It keeps the stored evidence and envelope, minus task-output previews, which stay only in the row's earlier events
    and never enter a new one. Admission re-runs under the current rule, so a historical
    achievement whose proof predates the shared task classification is held pending, never grandfathered. The schema
    migration names the slot per source row (``legacy:<hop>``) so several old rows for one task stay distinct.
    """
    if not isinstance(payload, Mapping) or not payload.get("effect"):
        return None
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    proof = payload.get("proof_envelope") if isinstance(payload.get("proof_envelope"), Mapping) else {}
    if not proof and isinstance(evidence.get("proof_envelope"), Mapping):
        proof = evidence["proof_envelope"]
    evidence = {k: v for k, v in evidence.items() if k not in _OUTPUT_PREVIEW_KEYS}
    achieved = _text(payload.get("status")).casefold() == "achieved"
    observation = build_observation(
        technique=_text(payload.get("technique")), target=_text(payload.get("target")),
        effect=_text(payload.get("effect")), satisfied_effects=payload.get("satisfied_effects") or [],
        preconditions=payload.get("preconditions") or [], evidence=evidence, proof_envelope=proof,
        admitted=achieved, admission_reason=_text(evidence.get("proof_admission_reason")),
        source=_text(payload.get("source")), timestamp=_text(payload.get("timestamp")),
        pending_reason=_text(payload.get("pending_reason")),
    )
    return {
        "task_id": _task_key(evidence, proof),
        "callback_id": _callback_key(evidence, proof),
        "writers": {writer: {"admitted" if achieved else "rejected": observation}},
    }


def contribution_row(canonical: CanonicalEffect, writer: str, observation: Mapping[str, Any]) -> dict[str, Any]:
    """A canonical row payload carrying exactly one observation (merge it onto the current head)."""
    evidence = observation.get("evidence") if isinstance(observation.get("evidence"), Mapping) else {}
    proof = observation.get("proof_envelope") if isinstance(observation.get("proof_envelope"), Mapping) else {}
    slot = "admitted" if observation.get("admitted") else "rejected"
    observation = dict(observation)
    review = evidence.get("credential_review") if isinstance(evidence.get("credential_review"), Mapping) else None
    if review is not None:
        # A credential review's record is the task's persisted decision: it lives once on the contribution, keyed by
        # its review ID, and the observation keeps only the ID.
        observation["evidence"] = {k: v for k, v in evidence.items() if k != "credential_review"}
    contribution = {
        "task_id": _task_key(evidence, proof),
        "callback_id": _callback_key(evidence, proof),
        "writers": {writer: {slot: observation}},
    }
    if review is not None and _text(review.get("review_id")):
        contribution["reviews"] = {_text(review.get("review_id")): dict(review)}
    return {
        "id": canonical.identity,
        "technique": canonical.technique,
        "target": canonical.target,
        "canonical_effect": canonical.identity,
        "contributions": [contribution],
    }


def _observation_digest(observation: Mapping[str, Any]) -> str:
    proof = observation.get("proof_envelope") if isinstance(observation.get("proof_envelope"), Mapping) else {}
    stable = {
        "effect": observation.get("effect"),
        "satisfied_effects": observation.get("satisfied_effects"),
        "admission_reason": observation.get("admission_reason"),
        "proof": {k: v for k, v in proof.items() if k not in _VOLATILE_PROOF_KEYS},
        "review": (observation.get("evidence") or {}).get("review_id") if isinstance(observation.get("evidence"), Mapping) else "",
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def observation_keys(payload: Mapping[str, Any] | None) -> set[tuple[str, str, str, str]]:
    """(task, writer, slot, digest) for every observation a canonical row payload carries."""
    keys: set[tuple[str, str, str, str]] = set()
    for contribution in (payload or {}).get("contributions") or []:
        if not isinstance(contribution, Mapping):
            continue
        task = _text(contribution.get("task_id"))
        for writer, slots in (contribution.get("writers") or {}).items():
            if not isinstance(slots, Mapping):
                continue
            for slot in _OBSERVATION_SLOTS:
                observation = slots.get(slot)
                if isinstance(observation, Mapping):
                    keys.add((task, str(writer), slot, _observation_digest(observation)))
    return keys


def payload_typed_identity(payload: Mapping[str, Any] | None) -> str:
    """The typed identity a stored row owns: its ``canonical_effect``, else its primary effect's typed identity."""
    if not isinstance(payload, Mapping):
        return ""
    if payload.get("canonical_effect"):
        return _text(payload.get("canonical_effect"))
    canonical = canonical_effect(payload.get("effect"))
    return canonical.identity if canonical is not None else ""


def is_canonical_payload(payload: Mapping[str, Any] | None) -> bool:
    return isinstance(payload, Mapping) and bool(payload.get("canonical_effect")) and isinstance(
        payload.get("contributions"), list)


def _merge_writer(current: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any]:
    """An admitted observation is never replaced or erased; a rejected one never erases an admission. The latest
    rejected observation is kept beside it (earlier ones stay in the row's event history)."""
    current = dict(current or {})
    incoming = dict(incoming or {})
    out: dict[str, Any] = {}
    if isinstance(current.get("admitted"), Mapping):
        out["admitted"] = dict(current["admitted"])
    elif isinstance(incoming.get("admitted"), Mapping):
        out["admitted"] = dict(incoming["admitted"])
    rejected = incoming.get("rejected") if isinstance(incoming.get("rejected"), Mapping) else current.get("rejected")
    if isinstance(current.get("rejected"), Mapping) and isinstance(rejected, Mapping) and (
            _observation_digest(current["rejected"]) == _observation_digest(rejected)):
        rejected = current["rejected"]  # a replay of the same rejected observation changes nothing
    if isinstance(rejected, Mapping):
        out["rejected"] = dict(rejected)
    return out


def merge_contributions(current: Iterable[Any], incoming: Iterable[Any]) -> list[dict[str, Any]]:
    """Union by Mythic task: one contribution per task, one observation slot pair per writer."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for source in (current or [], incoming or []):
        for contribution in source:
            if not isinstance(contribution, Mapping):
                continue
            key = _text(contribution.get("task_id"))
            entry = merged.get(key)
            if entry is None:
                entry = {"task_id": key, "callback_id": _text(contribution.get("callback_id")), "writers": {}}
                merged[key] = entry
                order.append(key)
            elif not entry["callback_id"]:
                entry["callback_id"] = _text(contribution.get("callback_id"))
            for writer, slots in (contribution.get("writers") or {}).items():
                if isinstance(slots, Mapping):
                    entry["writers"][str(writer)] = _merge_writer(entry["writers"].get(str(writer)), slots)
            if contribution.get("binding_conflict") and not entry.get("binding_conflict"):
                entry["binding_conflict"] = contribution["binding_conflict"]
            reviews = contribution.get("reviews")
            if isinstance(reviews, Mapping):
                # One record per review ID, first write wins: a reload or a repeat writer never regrades. A changed
                # output or rule is a new review ID beside the old one.
                stored = entry.setdefault("reviews", {})
                for review_key, record in reviews.items():
                    if isinstance(record, Mapping):
                        stored.setdefault(str(review_key), dict(record))
    return [merged[key] for key in sorted(order, key=_task_sort_key)]


def _task_sort_key(task: str) -> tuple[int, int, str]:
    return (0, int(task), "") if task.isdigit() else (1, 0, task)


def _admits(observation: Mapping[str, Any], contribution: Mapping[str, Any], engagement_id: str) -> tuple[bool, str]:
    """Re-run proof admission for one stored observation against its contribution's task and callback."""
    proof = observation.get("proof_envelope") if isinstance(observation.get("proof_envelope"), Mapping) else {}
    evidence = observation.get("evidence") if isinstance(observation.get("evidence"), Mapping) else {}
    task = _text(contribution.get("task_id"))
    callback = _text(contribution.get("callback_id"))
    if not task:
        return False, "contribution has no Mythic task binding"
    try:
        admission = proof_boundary.admit_task_observation(
            proof_boundary.ProofEnvelope.from_dict(dict(proof)),
            claimed_effects=list(observation.get("satisfied_effects") or [observation.get("effect")]),
            current_engagement_id=engagement_id,
            expected_callback_id=callback,
            expected_task_id=task,
            expected_transaction_id=evidence.get("transaction_id") or "",
            expected_terminal_status=evidence.get("terminal_task_status") or evidence.get("terminal_status") or "",
            expected_verifier_id=evidence.get("verifier_id") or "",
            expected_verifier_input_sha256=evidence.get("verifier_input_sha256") or "",
            expected_verifier_result_sha256=evidence.get("verifier_result_sha256") or "",
            expected_artifact_id=evidence.get("artifact_id") or "",
            expected_authorization=evidence,
        )
    except Exception:
        return False, "proof admission failed closed"
    return admission.admitted, admission.reason


def _binding_conflict(contribution: Mapping[str, Any]) -> str:
    """A reason when observations of one task disagree on its callback; such a contribution cannot be admitted."""
    callbacks = set()
    for slots in (contribution.get("writers") or {}).values():
        for slot in _OBSERVATION_SLOTS:
            observation = slots.get(slot) if isinstance(slots, Mapping) else None
            if not isinstance(observation, Mapping):
                continue
            evidence = observation.get("evidence") if isinstance(observation.get("evidence"), Mapping) else {}
            proof = observation.get("proof_envelope") if isinstance(observation.get("proof_envelope"), Mapping) else {}
            callback = _callback_key(evidence, proof)
            if callback:
                callbacks.add(callback)
    if len(callbacks) > 1:
        return f"observations of task {contribution.get('task_id')} name different callbacks"
    return ""


def _writer_rank(writer: str) -> int:
    kind = writer.split(":", 1)[0]
    return _WRITER_ORDER.index(kind) if kind in _WRITER_ORDER else len(_WRITER_ORDER)


def derive_row(payload: Mapping[str, Any], engagement_id: str) -> dict[str, Any]:
    """Status, effects and headline evidence of a canonical row, from the contributions the proof boundary admits.

    The row is ``achieved`` when at least one observation is admitted; its headline proof and evidence are the
    strongest admitted observation's (working before material, then the earliest task), so every reader that admits
    a row's top-level envelope keeps working. A row with no admitted observation is ``pending`` / ``proof_missing``
    and keeps the latest rejected candidate and its reason.
    """
    canonical = canonical_from_identity(payload.get("canonical_effect"))
    contributions = merge_contributions(payload.get("contributions") or [], [])
    admitted: list[tuple[int, tuple, int, str, dict]] = []
    rejected: list[tuple[str, dict]] = []
    satisfied: list[str] = []
    for contribution in contributions:
        conflict = _binding_conflict(contribution)
        if conflict:
            contribution["binding_conflict"] = conflict
        # Derived on every merge, for views: which writers' observations of this task the proof boundary admits now.
        contribution["admitted_writers"] = []
        for writer, slots in contribution.get("writers", {}).items():
            observation = slots.get("admitted")
            if isinstance(observation, Mapping):
                ok, reason = (False, conflict) if conflict else _admits(observation, contribution, engagement_id)
                if ok:
                    contribution["admitted_writers"].append(writer)
                    rank = _STRENGTH_RANK.get(_text(observation.get("strength")), 0)
                    admitted.append((rank, _task_sort_key(contribution["task_id"]), _writer_rank(writer), writer,
                                     dict(observation)))
                    continue
                rejected.append((_text(observation.get("timestamp")), dict(observation, admission_reason=reason)))
            observation = slots.get("rejected")
            if isinstance(observation, Mapping):
                rejected.append((_text(observation.get("timestamp")), dict(observation)))
    admitted.sort(key=lambda item: (-item[0], item[1], item[2]))
    for _rank, _task, _writer_order, _writer, observation in admitted:
        for effect in observation.get("satisfied_effects") or [observation.get("effect")]:
            other = canonical_effect(effect)
            if canonical is not None and other is not None:
                if other.identity != canonical.identity:
                    continue  # an effect of another account belongs to that account's row
                # One spelling per typed effect, whatever the writer typed (for example a NETBIOS\ prefix).
                effect = working_effect(other) if other.strength == STRENGTH_WORKING else material_effect(
                    other, other.material_kind)
            if effect and effect not in satisfied:
                satisfied.append(effect)
    out = {
        "id": payload.get("id") or payload.get("canonical_effect"),
        "technique": payload.get("technique"),
        "target": payload.get("target"),
        "canonical_effect": payload.get("canonical_effect"),
        "contributions": contributions,
    }
    timestamps = [obs.get("timestamp") for *_ignored, obs in admitted] + [stamp for stamp, _obs in rejected]
    latest = max((str(stamp) for stamp in timestamps if stamp), default="")
    if admitted:
        best = admitted[0][4]
        strongest = [effect for effect in satisfied if canonical_effect(effect) is not None]
        out.update({
            "effect": strongest[0] if strongest else best.get("effect"),
            "status": "achieved",
            "pending_reason": "",
            "satisfied_effects": satisfied,
            "preconditions": list(best.get("preconditions") or []),
            "evidence": dict(best.get("evidence") or {}),
            "proof_envelope": dict(best.get("proof_envelope") or {}),
            "source": best.get("source") or "",
            "timestamp": latest or best.get("timestamp") or "",
        })
        return out
    head = sorted(rejected, key=lambda item: item[0])[-1][1] if rejected else {}
    evidence = dict(head.get("evidence") or {})
    if head.get("admission_reason"):
        evidence["proof_admission_reason"] = head.get("admission_reason")
        evidence.setdefault("proof_persistence_state", proof_boundary.REJECTED)
    reason = _text(head.get("pending_reason")).casefold()
    out.update({
        "effect": head.get("effect") or "",
        "status": "pending",
        "pending_reason": reason if reason in {"awaiting_effect", "proof_missing"} else "proof_missing",
        "satisfied_effects": [head.get("effect")] if head.get("effect") else [],
        "preconditions": list(head.get("preconditions") or []),
        "evidence": evidence,
        "proof_envelope": dict(head.get("proof_envelope") or {}),
        "source": head.get("source") or "",
        "timestamp": latest,
    })
    return out


def merge_rows(current: Mapping[str, Any] | None, incoming: Mapping[str, Any], engagement_id: str) -> dict[str, Any]:
    """The head after merging ``incoming`` onto ``current`` (both canonical row payloads for one identity)."""
    base = dict(current or {})
    if base and not is_canonical_payload(base):
        legacy = legacy_contribution(base)
        base = {
            "id": base.get("id"), "technique": base.get("technique"), "target": base.get("target"),
            "canonical_effect": incoming.get("canonical_effect"), "contributions": [legacy] if legacy else [],
        }
    merged = {
        "id": base.get("id") or incoming.get("id"),
        "technique": base.get("technique") or incoming.get("technique"),
        "target": base.get("target") or incoming.get("target"),
        "canonical_effect": base.get("canonical_effect") or incoming.get("canonical_effect"),
        "contributions": merge_contributions(base.get("contributions") or [], incoming.get("contributions") or []),
    }
    return derive_row(merged, engagement_id)


def contribution_mutation_id(engagement_key: str, identity: str, keys: Iterable[tuple]) -> str:
    """Stable per (operation, canonical effect, task, writer, observation): replaying it is idempotent."""
    blob = json.dumps(sorted(list(key) for key in keys), separators=(",", ":"))
    return hashlib.sha256(f"contribution|{engagement_key}|{identity}|{blob}".encode()).hexdigest()
