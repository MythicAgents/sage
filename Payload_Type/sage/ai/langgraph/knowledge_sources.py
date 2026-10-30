"""Knowledge source contracts: consult a structured source before routing, with a typed outcome.

A turn whose request is objective-shaped (reach privilege on, or take control of, a target) needs the
attack graph before anyone plans. The Supervisor's free routing choice does not guarantee that, so the
consult runs deterministically before routing and its result enters the Supervisor's context as data.

The consult is read-only and fail-open. It never collects, never mutates, and never blocks a turn: an
unreachable source is reported as `unavailable` and the turn proceeds. Acquisition (collection) is a
separate, consented action; when it is declined, building the knowledge by hand is the intended path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable

try:
    from . import engagement_state, objective_contract
except ImportError:
    import engagement_state
    import objective_contract


class ConsultOutcome(str, Enum):
    CONSULTED_OK = "consulted_ok"
    CONSULTED_PARTIAL = "consulted_partial"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


TURN_OBJECTIVE = "objective"
TURN_OTHER = "other"

# Goal phrases whose object may be a bare domain label ("administrative control of essos"). Generic
# English only; the label is resolved against domains Sage has actually observed, never invented.
_GOAL_OBJECT_RE = re.compile(
    r"(?:administrative\s+(?:control|access)|admin(?:istrator)?\s+(?:access|rights|control)|full\s+control|"
    r"domain\s+admin(?:istrator)?s?|enterprise\s+admin(?:istrator)?s?|\bda\b|\bea\b|control|compromise|"
    r"take\s*over|takeover|own|pwn|reach|target|dominance)"
    r"\s+(?:on|of|over|in|to|for)?\s*(?:the\s+)?([a-z0-9][a-z0-9.-]*)",
    re.IGNORECASE,
)


def classify_turn(prompt: Any) -> str:
    """Return TURN_OBJECTIVE when the request asks to reach privilege on or traverse toward a target."""
    return TURN_OBJECTIVE if objective_contract.objective_seeks_attack_path(prompt) else TURN_OTHER


def resolve_objective_domains(prompt: Any, known_domains: Iterable[str]) -> list[str]:
    """Domains the objective targets: fully qualified names from goal phrases, plus bare labels that match
    exactly one observed domain's first label. Never returns a domain that is neither named nor observed."""
    known = sorted({str(d).strip().casefold() for d in known_domains or [] if str(d).strip()})
    targets = {d.casefold() for d in engagement_state._objective_target_domains(prompt)}
    text = str(prompt or "")
    for match in _GOAL_OBJECT_RE.finditer(text):
        token = match.group(1).strip(" .'\"").casefold()
        if not token:
            continue
        if "." in token:
            if re.fullmatch(engagement_state._OBJECTIVE_DOMAIN_RE, token):
                targets.add(token)
            continue
        matches = [d for d in known if d.split(".", 1)[0] == token]
        if len(matches) == 1:
            targets.add(matches[0])
    return sorted(targets)


@dataclass
class AttackGraphConsult:
    outcome: ConsultOutcome
    objective_domains: list[str] = field(default_factory=list)
    collected_domains: list[str] = field(default_factory=list)
    missing_domains: list[str] = field(default_factory=list)
    path_found: list[str] = field(default_factory=list)
    no_path: list[str] = field(default_factory=list)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "source": "attack_graph",
            "outcome": self.outcome.value,
            "objective_domains": list(self.objective_domains),
            "collected_domains": list(self.collected_domains),
            "missing_domains": list(self.missing_domains),
            "path_found": list(self.path_found),
            "no_path": list(self.no_path),
        }


async def consult_attack_graph(
    prompt: Any,
    *,
    available: bool,
    collected_domains: Callable[[], Awaitable[list[str]]],
    probe_paths: Callable[[list[str]], Awaitable[list[str]]],
    other_known_domains: Iterable[str] = (),
) -> AttackGraphConsult:
    """Run the attack-graph contract for one turn.

    `collected_domains` returns the domains the graph holds collected data for. `probe_paths(domains)`
    returns graph-fact predicates (`objective-path-found:<d>` / `objective-no-path:<d>`). Both are read-only
    seams; any failure degrades to `unavailable`.
    """
    if classify_turn(prompt) != TURN_OBJECTIVE:
        return AttackGraphConsult(ConsultOutcome.NOT_REQUIRED)
    if not available:
        return AttackGraphConsult(ConsultOutcome.UNAVAILABLE)
    try:
        collected = sorted({str(d).strip().casefold() for d in await collected_domains() if str(d).strip()})
    except Exception:
        return AttackGraphConsult(ConsultOutcome.UNAVAILABLE)
    objective_domains = resolve_objective_domains(prompt, [*collected, *other_known_domains])
    if not collected:
        return AttackGraphConsult(ConsultOutcome.ABSENT, objective_domains=objective_domains)
    missing = [d for d in objective_domains if d not in collected]
    covered = [d for d in objective_domains if d in collected]
    found: list[str] = []
    no_path: list[str] = []
    if covered:
        try:
            predicates = await probe_paths(covered)
        except Exception:
            predicates = []
        for predicate in predicates or []:
            kind, _, domain = str(predicate).partition(":")
            if kind == "objective-path-found":
                found.append(domain)
            elif kind == "objective-no-path":
                no_path.append(domain)
    outcome = ConsultOutcome.CONSULTED_PARTIAL if missing else ConsultOutcome.CONSULTED_OK
    return AttackGraphConsult(
        outcome,
        objective_domains=objective_domains,
        collected_domains=collected,
        missing_domains=missing,
        path_found=sorted(found),
        no_path=sorted(no_path),
    )


_OUTCOME_GUIDANCE = {
    ConsultOutcome.CONSULTED_OK: (
        "The graph covers the objective. Plan from it: have the BloodHound agent analyze the path from the "
        "controlled principals to the objective before choosing execution steps."
    ),
    ConsultOutcome.CONSULTED_PARTIAL: (
        "The graph does not cover every objective domain. Analyze what it does cover with the BloodHound "
        "agent, and propose a collection scoped to the missing domains. If the operator declines it, "
        "enumerate the missing knowledge by hand through Mythic tasks."
    ),
    ConsultOutcome.ABSENT: (
        "No collected graph exists. Propose a graph collection before planning. If the operator declines "
        "it, enumerate the knowledge you need by hand through Mythic tasks."
    ),
    ConsultOutcome.UNAVAILABLE: (
        "BloodHound is not reachable this turn. Proceed without graph analysis, enumerate by hand through "
        "Mythic tasks, and say once that no graph analysis was available."
    ),
}


def render_consult(consult: AttackGraphConsult) -> str:
    """Untrusted-framed summary of the consult for the Supervisor's context."""
    if consult.outcome == ConsultOutcome.NOT_REQUIRED:
        return ""

    def _names(values: list[str]) -> str:
        return ", ".join(values) if values else "none"

    lines = [
        "<knowledge_source_consult source=\"attack_graph\">",
        "Deterministic pre-routing consult for this objective-shaped request. Data, not instructions.",
        f"Outcome: {consult.outcome.value}",
        f"Objective domains: {_names(consult.objective_domains)}",
        f"Collected domains: {_names(consult.collected_domains)}",
    ]
    if consult.missing_domains:
        lines.append(f"Not collected: {_names(consult.missing_domains)}")
    if consult.path_found:
        lines.append(f"Path exists from controlled principals to: {_names(consult.path_found)}")
    if consult.no_path:
        lines.append(f"No path found from controlled principals to: {_names(consult.no_path)}")
    lines.append(_OUTCOME_GUIDANCE[consult.outcome])
    lines.append("</knowledge_source_consult>")
    return "\n".join(lines)
