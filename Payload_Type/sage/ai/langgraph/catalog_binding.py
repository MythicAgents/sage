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
import re
from typing import Any, Iterable, Mapping

try:
    from .capabilities import (
        CapabilityAction, _input_text, _normalize, _target_fields, forge_target_domain, forge_ticket_domain,
        refreshes_kerberos_context,
    )
    from .request_contract import action_fingerprint
except ImportError:  # pragma: no cover - flat import layout
    from capabilities import (
        CapabilityAction, _input_text, _normalize, _target_fields, forge_target_domain, forge_ticket_domain,
        refreshes_kerberos_context,
    )
    from request_contract import action_fingerprint

MAX_CHARS = 6000
# A brief's `cred:`/`task:` reference becomes the input a procedure binds a proved key by (`credential_id`,
# `credential_proof_task_id`), only for the procedures whose executor reads it (MythicTools._KRBTGT_FORGE_CAPABILITIES)
# and only when the candidate reads a key: an `ensure-kerberos-context` flagged to refresh reads none.
REFERENCE_INPUTS = {"cred": "credential_id", "task": "credential_proof_task_id"}
# Several `cred:` references name rows of one key set; the executor resolves them to one proof (`credential_ids`).
REFERENCE_SET_INPUT = "credential_ids"
REFERENCE_PROCEDURES = frozenset({"forge-golden-ticket", "ensure-kerberos-context"})
# The procedures whose service proof an operator-named host may target (the executor reads `proof_host` for both), and
# the bound inputs that pin it: an approved call must carry them exactly as bound.
PROOF_HOST_PROCEDURES = frozenset({"forge-golden-ticket", "ensure-kerberos-context"})
PROOF_HOST_INPUTS = ("proof_host", "proof_resource_source")
PROOF_HOST_SOURCE = "operator-named"
# A re-verify request pins the refresh bit in the candidate; an approved call may not add or drop it.
REFRESH_INPUTS = ("force_refresh", "force_revalidate", "refresh_current_context")
_DOMAIN_KEYS = ("domain", "source_domain", "target_domain", "effect_domain", "forge_domain")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
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
    head = [_TITLE, "A catalog step (allowed_capabilities) must name exactly one candidate_ref below, that procedure "
                    "and its callback. Sage runs exactly that action after approval; the step's targets are only "
                    "recorded."]
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


def _reference_use(brief: Any, candidate: Candidate) -> tuple[dict[str, list[str]], tuple[str, ...], str]:
    """How `candidate` uses the brief's references: the numbers a forge pins by kind, the references it does not use
    (distinct, in the brief's order) and the first conflicting reference ("" when none)."""
    bound = list(_values({"action": candidate.action, "inputs": candidate.inputs}))
    supplied: dict[str, list[str]] = {"cred": [], "task": []}
    unused: list[str] = []
    pins_key = candidate.procedure in REFERENCE_PROCEDURES and not refreshes_kerberos_context(
        candidate.action.get("intent"), candidate.inputs)
    intent = candidate.action.get("intent") if isinstance(candidate.action.get("intent"), Mapping) else {}
    # Reviewed material pins its source task in the candidate: that task's reference is used, any other conflicts.
    pins_source = isinstance(intent.get("credential_material"), Mapping)
    for reference in getattr(brief, "inputs_by_reference", ()) or ():
        kind, _, number = str(reference).lstrip("@").partition(":")
        word = "task" if kind == "task" else "cred"
        if any(word in key and text == number for key, text in bound):
            continue
        if pins_source and word == "task":
            return supplied, tuple(dict.fromkeys(unused)), str(reference)
        if not pins_key:
            # A candidate that binds no key by reference never reads it: its action is the same without it, so
            # admission drops it from the step and the step outcome names it.
            unused.append(reference)
            continue
        # On a forge a supplied reference is used, never ignored: it pins the key the forge binds. Several
        # credentials name one key set; a second, different proof task conflicts.
        if word == "task" and supplied["task"] and number != supplied["task"][0]:
            return supplied, tuple(dict.fromkeys(unused)), str(reference)
        supplied[word].append(number)
    return supplied, tuple(dict.fromkeys(unused)), ""


def proof_domain(candidate: Candidate) -> str:
    """The domain the candidate's service proof must prove, as the executor reads it; "" for other procedures. A forge
    proves the domain its ticket is requested for, else the one it is issued in; a Kerberos context its target domain,
    else its domain."""
    intent = candidate.action.get("intent") if isinstance(candidate.action.get("intent"), Mapping) else {}
    inputs = dict(candidate.inputs)
    if candidate.procedure == "forge-golden-ticket":
        action = CapabilityAction(name=candidate.procedure, target=candidate.target, intent=dict(intent))
        return forge_target_domain(action, inputs) or forge_ticket_domain(action, inputs)
    if candidate.procedure == "ensure-kerberos-context":
        fields = _target_fields(candidate.target)
        return _normalize(_input_text(inputs, "target_domain", "effect_domain") or intent.get("target_domain")
                          or intent.get("effect_domain") or fields.get("target_domain")
                          or _input_text(inputs, "domain", "source_domain") or intent.get("domain")
                          or intent.get("source_domain") or fields.get("domain"))
    return ""


def qualified_proof_host(candidate: Candidate, value: Any, known_domains: Iterable[str] = ()) -> tuple[str, str]:
    """(host, "") when `value` names one host of the domain the candidate's service proof must prove, qualified there
    (case, a trailing dot, a first label or a UNC path's host are the same host); else ("", why it cannot be used).
    A domain's name is never a host: the candidate's own domains and `known_domains` (a child domain's DCs answer
    for its name) are refused, as are the proof domain's first label and any name outside the proof domain."""
    if candidate.procedure not in PROOF_HOST_PROCEDURES:
        return "", f"`{candidate.procedure}` takes no proof host"
    domain = proof_domain(candidate)
    text = str(value or "").strip().strip("'\"")
    if text.startswith("\\\\"):
        text = text[2:].split("\\", 1)[0]
    host = text.rstrip(".").casefold()
    if not domain:
        return "", "the bound action names no domain for its service proof"
    if not _HOST_RE.fullmatch(host) or host.replace(".", "").isdigit():     # an IPv6 literal fails the pattern
        return "", "it is not a host name"
    named = {_normalize(source.get(key)) for source in (candidate.action.get("intent"), _target_fields(
        candidate.target), candidate.inputs) if isinstance(source, Mapping) for key in _DOMAIN_KEYS}
    qualified = host if "." in host else f"{host}.{domain}"
    if host == domain.split(".", 1)[0] or qualified in {domain, *named, *map(_normalize, known_domains)}:
        return "", "it names a domain, not a host"
    host = qualified
    if host.split(".", 1)[1] != domain:
        return "", f"it is not a host in {domain}, the domain this step's service proof must prove"
    return host, ""


def unused_references(brief: Any, candidate: Candidate) -> tuple[str, ...]:
    """The brief's references that its bound `candidate` does not use; admission drops them from the stored brief."""
    return _reference_use(brief, candidate)[1]


def bind(brief: Any, found: tuple[Candidate, ...] | None, *, reason: str = "") -> tuple[Candidate | None, str]:
    """The one current candidate a catalog brief selects, or None and the factual reason it cannot be bound.

    The brief must list the candidate's procedure; it may also list others (the procedure it feeds), which the step
    does not run: admission narrows the step to the candidate's procedure. Likewise a held reference that a candidate
    reading no key does not use is dropped at admission (`unused_references`), not refused."""
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
    if candidate.procedure not in procedures:
        return None, f"candidate_ref {ref} runs `{candidate.procedure}`, not {named}"
    callback = _callback(getattr(brief, "callback_display_id", ""))
    if callback != candidate.callback:
        return None, (f"candidate_ref {ref} runs on callback {candidate.callback or 'none'}, not callback "
                      f"{callback or 'none'}")
    supplied, _unused, conflict = _reference_use(brief, candidate)
    if conflict:
        return None, f"candidate_ref {ref} does not use the input reference {conflict}"
    credentials = credential_set(supplied["cred"])
    pins: dict[str, Any] = {REFERENCE_INPUTS["task"]: supplied["task"][0]} if supplied["task"] else {}
    if len(credentials) == 1:
        pins[REFERENCE_INPUTS["cred"]] = credentials[0]
    elif credentials:
        pins[REFERENCE_SET_INPUT] = credentials
    # An admitted operator-named proof host is pinned like a key reference; one outside the proof domain never is.
    host = qualified_proof_host(candidate, brief.proof_host)[0] if getattr(brief, "proof_host", "") else ""
    if host:
        pins.update(dict(zip(PROOF_HOST_INPUTS, (host, PROOF_HOST_SOURCE))))
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
    # Card-time augmentation may add a resolved proof target the candidate lacks; a pinned one must stay as bound.
    changed += [key for key in PROOF_HOST_INPUTS if key in candidate.inputs and key not in inputs]
    # The refresh bit is pinned: the call may neither add nor drop it, so a re-verify cannot become an ordinary
    # DCSync (which would read the old proof) and an ordinary DCSync cannot silently become a re-verify.
    def refresh_bit(values: Mapping[str, Any]) -> bool:
        return any(bool(values.get(key)) for key in REFRESH_INPUTS)
    if refresh_bit(inputs) != refresh_bit(candidate.inputs):
        return "the call changed the refresh bit"
    return f"the call changed bound inputs: {', '.join(sorted(changed))}" if changed else ""
