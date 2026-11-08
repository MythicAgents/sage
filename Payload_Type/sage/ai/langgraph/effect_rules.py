"""The code rules that derive one engagement effect from others, as a pure evaluator.

A derived gain is a logical implication over already-proven premises, not a new observation: holding domain admin
on a domain for which you hold a usable context implies that domain's replication rights, and so on. The same three
implications drive two consumers — the runtime gate's ``satisfied_predicates`` and the ledger reconciler that
persists a ``derived_rule`` row — so they must come from one place. This module is that place.

``evaluate`` names each derivation fully: a stable rule id and version, the exact output effect, the premise
predicates it consumes (which the ledger resolves to admitted rows), and the live eligibility conditions it
requires (a live callback, context or foothold — current-state checks, never fabricated premise rows). ``expand``
is the set form the runtime calculation needs, reproducing the previous ``_expand_implications`` output exactly.
``apply_rule`` is the single structural step both the evaluator and the proof boundary use to turn one rule's exact
premises and conditions into its output effect, so a persisted derivation can be recomputed without trusting a
serialized output. Pure: no store, no model call, no task issue. Premise parsing is reused from ``engagement_state``
through a call-time import, so the two never drift and there is no import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RULE_VERSION = "v1"
# Stable rule identities. A persisted derivation names one of these; an unknown id fails admission.
RULE_KERBEROS_CONTEXT = "kerberos-context-from-live-callback"
RULE_DA_REPLICATION = "replication-rights-from-domain-admin"
RULE_GPO_REPLICATION = "replication-rights-from-governed-gpo"
RULES: dict[str, str] = {
    RULE_KERBEROS_CONTEXT: RULE_VERSION,
    RULE_DA_REPLICATION: RULE_VERSION,
    RULE_GPO_REPLICATION: RULE_VERSION,
}

_DA_EFFECT_PREFIXES = ("da:", "ea:")
_CONTEXT_CONDITION_PREFIX = "kerberos-context:"
_FOOTHOLD_CONDITION_PREFIX = "live-foothold:"


@dataclass(frozen=True)
class Derivation:
    """One output effect a code rule derives, with everything needed to admit it durably.

    ``premises`` are predicates that must resolve to admitted ledger rows; ``conditions`` are live-state predicates
    (a live callback, context or foothold) that must currently hold but are never persisted as premise rows."""

    rule_id: str
    rule_version: str
    output_effect: str
    premises: tuple[str, ...]
    conditions: tuple[str, ...] = ()


def _es():
    try:
        from . import engagement_state as es
    except ImportError:  # pragma: no cover - flat import for tests on sys.path
        import engagement_state as es  # type: ignore
    return es


def apply_rule(rule_id: Any, premises: tuple[str, ...], conditions: tuple[str, ...]) -> str:
    """The output effect the named rule yields from exactly these premises and conditions, or '' if it does not
    fire. The single structural step shared by :func:`evaluate` and the proof boundary: it recomputes the output
    from the inputs and never trusts a serialized value, so an unknown rule, a changed or foreign premise, a wrong
    arity or a condition that does not match its premise all yield ''. It does not check that a condition is *live*;
    that is the reconciler's current-state check before it persists a derivation."""
    es = _es()
    rule_id = str(rule_id)
    premises = tuple(premises)
    conditions = tuple(conditions)

    if rule_id == RULE_KERBEROS_CONTEXT:
        if len(premises) != 1 or len(conditions) != 1:
            return ""
        parsed = es._parse_kerberos_context_effect(premises[0])
        if not parsed:
            return ""
        domain, callback_id = parsed
        if domain and conditions[0] == f"live-callback:{callback_id}":
            return f"kerberos-context:{domain}"
        return ""

    if rule_id == RULE_DA_REPLICATION:
        if len(premises) != 1 or len(conditions) != 1:
            return ""
        premise, condition = premises[0], conditions[0]
        domain = next((premise[len(p):].strip() for p in _DA_EFFECT_PREFIXES if premise.startswith(p)), "")
        target = next((condition[len(p):].strip() for p in (_CONTEXT_CONDITION_PREFIX, _FOOTHOLD_CONDITION_PREFIX)
                       if condition.startswith(p)), "")
        if domain and target and es._domains_equivalent(domain, target):
            return f"ds-replication-rights:{target}"
        return ""

    if rule_id == RULE_GPO_REPLICATION:
        if len(premises) != 2 or conditions:
            return ""
        system_premise = next((p for p in premises if p.startswith("system:")), "")
        gpo_premise = next((p for p in premises if p.startswith("gpo-domain:")), "")
        if not system_premise or not gpo_premise:
            return ""
        gpo = system_premise[len("system:"):].strip()
        governed_gpo, _, domain = gpo_premise[len("gpo-domain:"):].partition(":")
        if gpo and governed_gpo.strip() == gpo and domain.strip():
            return f"ds-replication-rights:{domain.strip()}"
        return ""

    return ""


def _live_foothold_domains(predicates: set[str]) -> set[str]:
    """Dotted forest FQDNs of live in-domain footholds (mirrors ``_expand_implications``: a bare NetBIOS label is
    rejected because it collides across unrelated forests)."""
    return {
        value for value in (
            predicate.split(":", 1)[1].strip()
            for predicate in predicates
            if predicate.startswith(("authenticated:", "live-foothold:")) and ":" in predicate
            and not predicate.endswith(":*")
        )
        if "." in value
    }


def _gpo_domains(predicates: set[str]) -> dict[str, str]:
    gpo_domain: dict[str, str] = {}
    for predicate in predicates:
        if predicate.startswith("gpo-domain:"):
            gpo, _, domain = predicate[len("gpo-domain:"):].partition(":")
            if gpo.strip() and domain.strip():
                gpo_domain[gpo.strip()] = domain.strip()
    return gpo_domain


def evaluate(predicates: set[str]) -> list[Derivation]:
    """Every derivation the three code rules produce over ``predicates``, each fully named. Pure; never raises.

    The rules and their semantics are exactly those of the previous ``engagement_state._expand_implications``:
    (1) a domain's Kerberos context from a context effect held on a live callback; (2) a domain's replication
    rights from domain/enterprise admin on that domain when the right is usable (a live in-domain foothold, or a
    live cross-forest Kerberos context); (3) a domain's replication rights from SYSTEM on a GPO that governs it.
    """
    es = _es()
    predicates = set(predicates)
    results: list[Derivation] = []

    live_callbacks = {
        predicate[len("live-callback:"):].strip()
        for predicate in predicates if predicate.startswith("live-callback:")
    }
    live_context_domains = es._live_kerberos_context_domains(predicates)
    live_foothold_domains = _live_foothold_domains(predicates)
    gpo_domain = _gpo_domains(predicates)

    def add(rule_id, premises, conditions):
        output = apply_rule(rule_id, premises, conditions)
        if output:
            results.append(Derivation(rule_id, RULE_VERSION, output, tuple(premises), tuple(conditions)))

    for predicate in predicates:
        parsed = es._parse_kerberos_context_effect(predicate)
        if parsed and parsed[1] in live_callbacks:
            add(RULE_KERBEROS_CONTEXT, (predicate,), (f"live-callback:{parsed[1]}",))

    for predicate in predicates:
        for prefix in _DA_EFFECT_PREFIXES:
            if not predicate.startswith(prefix):
                continue
            domain = predicate[len(prefix):].strip()
            if not domain:
                continue
            context_matches = {c for c in live_context_domains if es._domains_equivalent(domain, c)}
            foothold_matches = {c for c in live_foothold_domains if es._domains_equivalent(domain, c)}
            for usable in (context_matches or foothold_matches):
                condition = (f"{_CONTEXT_CONDITION_PREFIX}{usable}" if usable in context_matches
                             else f"{_FOOTHOLD_CONDITION_PREFIX}{usable}")
                add(RULE_DA_REPLICATION, (predicate,), (condition,))

    for predicate in predicates:
        if predicate.startswith("system:"):
            gpo = predicate[len("system:"):].strip()
            domain = gpo_domain.get(gpo)
            if domain:
                add(RULE_GPO_REPLICATION, (predicate, f"gpo-domain:{gpo}:{domain}"), ())
    return results


def expand(predicates: set[str]) -> set[str]:
    """The satisfied-predicate set augmented with every derivation's output effect. Equals the previous
    ``_expand_implications`` output for the same input."""
    return set(predicates) | {derivation.output_effect for derivation in evaluate(predicates)}


def is_known_rule(rule_id: Any, rule_version: Any) -> bool:
    return RULES.get(str(rule_id)) == str(rule_version)
