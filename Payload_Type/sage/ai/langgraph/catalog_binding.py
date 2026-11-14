"""Bind a supervised catalog step to one action that current evidence supports.

The caller derives the actions (`capabilities.actions_from_state`) and the complete `execute_capability` arguments
for each from one ledger snapshot. This module names each one (`candidate_ref`), renders the list for the
Supervisor, checks a brief's selection against it and checks an approved call against the candidate it came from.
Available means supported by current evidence, not guaranteed to succeed or relevant to the objective, and an empty
list never means nothing is possible.

Pure and offline: nothing here reads Mythic, the ledger or a model.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Iterable, Mapping

try:
    from . import handoff_brief as _handoff_brief
    from .request_contract import action_fingerprint
except ImportError:  # pragma: no cover - flat import layout
    import handoff_brief as _handoff_brief
    from request_contract import action_fingerprint

MAX_CHARS = 6000
# A brief's `cred:`/`task:` reference becomes the input a procedure binds a proved key by (`credential_id`,
# `credential_proof_task_id`), only for the procedures whose executor reads it (MythicTools._KRBTGT_FORGE_CAPABILITIES).
REFERENCE_INPUTS = {"cred": "credential_id", "task": "credential_proof_task_id"}
# Several `cred:` references name rows of one key set; the executor resolves them to one proof (`credential_ids`).
REFERENCE_SET_INPUT = "credential_ids"
REFERENCE_PROCEDURES = frozenset({"forge-golden-ticket", "ensure-kerberos-context"})
_TITLE = "[Sage catalog candidates — supported by current evidence, for this model call]"
_OTHER_WORK = ("`collect-graph`, control-plane reads and explicit off-catalog tool steps (allowed_tools) remain "
               "available, and new evidence or a new operator request can add candidates.")


@dataclass(frozen=True)
class Candidate:
    ref: str
    procedure: str
    target: str
    callback: str
    effects: tuple[str, ...]
    action: Mapping[str, Any]
    inputs: Mapping[str, Any]


def _callback(value: Any) -> str:
    text = str(value if value is not None else "").strip().lstrip("#")
    return str(int(text)) if text.isdigit() else text


def candidate_ref(request_id: str, action: Mapping[str, Any], inputs: Mapping[str, Any]) -> str:
    """The exact bound arguments' canonical fingerprint, scoped to one request so a ref never crosses requests."""
    digest = action_fingerprint("execute_capability", {"action": dict(action), "inputs": dict(inputs)})
    return "cand-" + hashlib.sha256(f"{request_id}\n{digest}".encode()).hexdigest()[:16]


def candidates(request_id: str, bound: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> tuple[Candidate, ...]:
    """One candidate per distinct (action payload, inputs) pair, in the producer's order."""
    found: dict[str, Candidate] = {}
    for action, inputs in bound:
        ref = candidate_ref(request_id, action, inputs)
        found.setdefault(ref, Candidate(
            ref=ref, procedure=str(action.get("name") or ""), target=str(action.get("target") or ""),
            callback=_callback(inputs.get("callback_id")),
            effects=tuple(str(item) for item in action.get("effects") or ()),
            action=json.loads(json.dumps(action)), inputs=json.loads(json.dumps(inputs)),
        ))
    return tuple(found.values())


def render(found: tuple[Candidate, ...] | None, *, reason: str = "", max_chars: int = MAX_CHARS) -> str:
    """The Supervisor's candidate section. Unavailable is never shown as empty; nothing is dropped silently."""
    if found is None:
        return (f"{_TITLE}\nUNAVAILABLE: {reason or 'the candidates could not be derived'}. This is not an empty list; "
                f"no catalog step can be bound until it is readable. {_OTHER_WORK}")
    if not found:
        return (f"{_TITLE}\nNo catalog procedure is supported by current evidence. That is not a finding that nothing "
                f"is possible: {_OTHER_WORK}")
    head = [_TITLE, "A catalog step (allowed_capabilities) must name exactly one candidate_ref below, that procedure, "
                    "its callback and targets the candidate names. Sage runs exactly that action after approval."]
    lines, used = [], len("\n".join(head)) + 200
    for item in found:
        line = (f"- candidate_ref={item.ref} | {item.procedure} -> {item.target} | callback "
                f"{item.callback or 'none'} | effect: {', '.join(item.effects) or 'none'}")
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    omitted = len(found) - len(lines)
    tail = [f"Omitted {omitted} candidate(s) that did not fit this view; they cannot be selected from it."] if omitted \
        else [f"Omitted: none; all {len(found)} candidate(s) are listed."]
    return "\n".join(head + lines + tail)


def _values(value: Any, key: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for name, item in value.items():
            yield from _values(item, str(name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _values(item, key)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        yield key.casefold(), str(value)


def credential_set(values: Any) -> list[str]:
    """Credential IDs as one canonical set: distinct, in numeric order, so every pass compares the same list."""
    items = values if isinstance(values, (list, tuple)) else () if values in (None, "") else (values,)
    return sorted({str(item).strip() for item in items} - {""}, key=lambda text: (len(text), text))


def bind(brief: Any, found: tuple[Candidate, ...] | None, *, reason: str = "") -> tuple[Candidate | None, str]:
    """The one current candidate a catalog brief selects, or None and the factual reason it cannot be bound."""
    procedures = tuple(getattr(brief, "allowed_capabilities", ()) or ())
    named = ", ".join(f"`{name}`" for name in procedures) or "the procedure"
    ref = str(getattr(brief, "candidate_ref", "") or "").strip()
    if not procedures:
        return None, ("`execute_capability` runs a catalog procedure: name it in allowed_capabilities with one current "
                      "candidate_ref")
    if found is None:
        return None, f"the catalog candidates are unavailable ({reason or 'unreadable'}), so {named} cannot be bound"
    if not ref:
        return None, f"{named} names no candidate_ref; a catalog step must select one current candidate"
    candidate = next((item for item in found if item.ref == ref), None)
    if candidate is None:
        offered = sum(item.procedure in procedures for item in found)
        return None, (f"candidate_ref {ref} is not supported by current evidence; {offered} current candidate(s) "
                      f"run {named}")
    if procedures != (candidate.procedure,):
        return None, f"candidate_ref {ref} runs `{candidate.procedure}`, not {named}"
    callback = _callback(getattr(brief, "callback_display_id", ""))
    if callback != candidate.callback:
        return None, (f"candidate_ref {ref} runs on callback {candidate.callback or 'none'}, not callback "
                      f"{callback or 'none'}")
    # The candidate's own names in any equivalent form (case, trailing dot, its host's short label, account forms).
    scope = _handoff_brief.action_scope(candidate.target, candidate.action.get("intent"),
                                        candidate.action.get("source_facts") or ())
    stray = [target for target in getattr(brief, "targets", ()) or () if not scope.names_current(target)]
    if stray:
        return None, f"candidate_ref {ref} ({candidate.target}) does not act on {', '.join(stray)}"
    bound = list(_values({"action": candidate.action, "inputs": candidate.inputs}))
    supplied: dict[str, list[str]] = {"cred": [], "task": []}
    for reference in getattr(brief, "inputs_by_reference", ()) or ():
        kind, _, number = str(reference).lstrip("@").partition(":")
        word = "task" if kind == "task" else "cred"
        if any(word in key and text == number for key, text in bound):
            continue
        # A supplied reference is used, never ignored: it pins the key a forge binds, or the step cannot be bound.
        # Several credentials name one key set; a second, different proof task conflicts.
        conflict = word == "task" and bool(supplied["task"]) and number != supplied["task"][0]
        if candidate.procedure not in REFERENCE_PROCEDURES or conflict:
            return None, f"candidate_ref {ref} does not use the input reference {reference}"
        supplied[word].append(number)
    credentials = credential_set(supplied["cred"])
    pins: dict[str, Any] = {REFERENCE_INPUTS["task"]: supplied["task"][0]} if supplied["task"] else {}
    if len(credentials) == 1:
        pins[REFERENCE_INPUTS["cred"]] = credentials[0]
    elif credentials:
        pins[REFERENCE_SET_INPUT] = credentials
    return (replace(candidate, inputs={**candidate.inputs, **pins}) if pins else candidate), ""


def _within(call: Any, bound: Any) -> bool:
    """Whether `call` is `bound` with keys only removed (Sage drops card secrets and supplied paths), never changed."""
    if isinstance(call, Mapping) and isinstance(bound, Mapping):
        return all(key in bound and _within(value, bound[key]) for key, value in call.items())
    return call == bound


def _same(call: Any, bound: Any) -> bool:
    """Equal, treating a scalar the binder rewrote between text and number (a credential id) as the same value."""
    scalar = (str, int)
    return call == bound or (isinstance(call, scalar) and isinstance(bound, scalar) and not isinstance(call, bool)
                             and not isinstance(bound, bool) and str(call).strip() == str(bound).strip())


def call_matches(args: Mapping[str, Any], candidate: Candidate) -> str:
    """Empty when an approved `execute_capability` call is the candidate's action; otherwise what differs."""
    action, inputs = args.get("action"), args.get("inputs")
    if not isinstance(action, Mapping) or not isinstance(inputs, Mapping):
        return "the call is not a complete bound action"
    if str(action.get("name") or "") != candidate.procedure or str(action.get("target") or "") != candidate.target:
        return f"the call is not `{candidate.procedure}` on {candidate.target}"
    if _callback(inputs.get("callback_id")) != candidate.callback:
        return f"the call does not run on callback {candidate.callback}"
    if not _within(action, candidate.action):
        return "the call's action differs from the current candidate"
    changed = [key for key in inputs if key in candidate.inputs and not _same(inputs[key], candidate.inputs[key])]
    return f"the call changed bound inputs: {', '.join(sorted(changed))}" if changed else ""
