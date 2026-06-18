"""C1 — ground-truth milestone reader for the Sage eval gauge (Phase 0).

Turns "what a completed run actually achieved" into a VERIFIED milestone vector,
read from the durable engagement ledger — never from trace text.

Design (see Plans/SAGE_EVAL_GAUGE_PHASE0_ISA.md):
  * A milestone counts only when an *achieved* hop carries its effect AND the
    PROOF appropriate to that effect is present. We do not hardcode proof keys:
    we reuse Sage's own verifier.
      - verify-on-record families (CREDENTIAL_TECHNIQUES / GRANT_TECHNIQUES) store
        `evidence.verify_verdict` directly -> read it.
      - every other technique stores the raw probe keys that
        `engagement_state.verify_effect` checks against TECHNIQUE_MODEL["verify"]
        -> replay verify_effect over the stored evidence.
  * Milestone -> effect mapping is a GOAD-chain default a Scenario may override.
    OBJECTIVE / cert effects are scenario-supplied because their exact effect
    tokens are not yet confirmed in code.
  * READ-ONLY. This module must leave all on-disk state byte-identical; callers
    can assert that with `ledger_state_hash()` before/after.

This is the outcome/ground-truth layer only. Sub-hop tradecraft (logon sessions,
security context, wrong-target execution) is measured separately by C1b
(process_state.py), kept apart so this ground-truth anchor stays pure.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

try:  # package import
    from ..langgraph import engagement_ledger
    from ..langgraph import engagement_state as es
except Exception:  # script / sys.path import (mirrors the repo's langgraph pattern)
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import engagement_ledger  # type: ignore
    import engagement_state as es  # type: ignore


class Milestone(IntEnum):
    """The GOAD attack chain, in order. `furthest_milestone` is the max reached."""
    FOOTHOLD = 0
    GRAPH_COLLECTED = 1
    SYSTEM_ON_HOST = 2
    DA_CHILD = 3
    KRBTGT_DUMPED = 4
    CROSS_DOMAIN_CONTEXT = 5
    REPLICATION_RIGHTS_PARENT = 6
    DCSYNC_PARENT = 7
    CERT_FORGED = 8
    OBJECTIVE = 9


@dataclass
class MilestoneSpec:
    """How to detect one milestone from verified effects.

    effect_prefixes: an effect proves this milestone if its normalized form starts
        with any of these (e.g. "krbtgt-hash:").
    domain_role: if set, the effect's domain tail must equal scenario.domains[role]
        (binds "child" vs "parent" vs "objective"); None means any domain counts.
    """
    effect_prefixes: tuple[str, ...]
    domain_role: str | None = None


# Default GOAD chain. Scenarios may override per-milestone (esp. CERT_FORGED / OBJECTIVE).
DEFAULT_SPEC: dict[Milestone, MilestoneSpec] = {
    Milestone.GRAPH_COLLECTED: MilestoneSpec(("graph-built:",)),
    Milestone.SYSTEM_ON_HOST: MilestoneSpec(("system:",)),
    Milestone.DA_CHILD: MilestoneSpec(("da:", "ea:"), domain_role="child"),
    Milestone.KRBTGT_DUMPED: MilestoneSpec(("krbtgt-hash:",), domain_role="child"),
    Milestone.CROSS_DOMAIN_CONTEXT: MilestoneSpec(("da:", "ea:", "kerberos-context:"), domain_role="parent"),
    Milestone.REPLICATION_RIGHTS_PARENT: MilestoneSpec(("ds-replication-rights:", "krbtgt-hash:"), domain_role="parent"),
    Milestone.DCSYNC_PARENT: MilestoneSpec(("krbtgt-hash:", "creds:"), domain_role="parent"),
    # CERT_FORGED + OBJECTIVE are scenario-supplied (effect tokens unconfirmed in code).
}


@dataclass
class Scenario:
    """A fixed evaluation scenario: which engagement to read and how to bind domains."""
    name: str
    engagement_id: str
    # role -> domain, e.g. {"child": "north.sevenkingdoms.local",
    #                       "parent": "sevenkingdoms.local",
    #                       "objective": "essos.local"}
    domains: dict[str, str] = field(default_factory=dict)
    # Per-milestone spec overrides (supply CERT_FORGED / OBJECTIVE here).
    spec_overrides: dict[Milestone, MilestoneSpec] = field(default_factory=dict)
    # Which milestones this scenario can prove; default = all keys in the merged spec + FOOTHOLD.
    milestone_subset: tuple[Milestone, ...] | None = None
    # Independent cross-checks (ISC-3): milestone -> callable returning real-world truth.
    # e.g. {Milestone.GRAPH_COLLECTED: lambda: bloodhound_domain_info_nonempty()}
    direct_probes: dict[Milestone, Callable[[], bool]] = field(default_factory=dict)

    def spec(self) -> dict[Milestone, MilestoneSpec]:
        merged = dict(DEFAULT_SPEC)
        merged.update(self.spec_overrides)
        return merged


@dataclass
class GroundTruth:
    scenario: str
    milestones: dict[Milestone, bool]
    furthest: Milestone
    # Milestones where an independent direct probe DISAGREED with the ledger.
    # A non-empty list is a gauge-validity alarm ("who verifies the verifier?").
    probe_disagreements: list[Milestone] = field(default_factory=list)


# --- proof predicate (the wall between ground truth and the substring trap) ---------------------------

def _norm(effect: str) -> str:
    try:
        return es._normalize_predicate(effect)  # reuse Sage's normalization for token parity
    except Exception:
        return str(effect or "").strip().casefold()


def hop_is_proven(hop: dict, *, require_run_provenance: bool = False) -> bool:
    """True iff this hop is achieved AND its effect is PROVEN by the appropriate verifier.

    Reuses Sage's verifier rather than guessing evidence keys:
      - if evidence carries `verify_verdict` (verify-on-record families), read it;
      - otherwise replay `verify_effect` over the stored probe-key evidence.
    """
    if not isinstance(hop, dict):
        return False
    if str(hop.get("status", "")).casefold() != "achieved":
        return False
    ev = hop.get("evidence")
    if not isinstance(ev, dict):
        return False
    if require_run_provenance and str(ev.get("provenance", "run")).casefold() == "durable":
        return False
    if "verify_verdict" in ev:
        return str(ev.get("verify_verdict", "")).casefold() == "achieved"
    try:
        return es.verify_effect(str(hop.get("technique", "")), str(hop.get("target", "")), ev) == "achieved"
    except Exception:
        return False


def _hop_effects(hop: dict) -> set[str]:
    out: set[str] = set()
    primary = hop.get("effect")
    if primary:
        out.add(_norm(primary))
    for e in hop.get("satisfied_effects") or []:
        if e:
            out.add(_norm(e))
    return {e for e in out if e}


def _effect_domain(effect: str, prefix: str) -> str:
    """The domain/identity tail of an effect after its prefix; the realm half for user@domain."""
    tail = effect[len(prefix):].strip() if effect.startswith(prefix) else ""
    return tail.split("@")[-1] if "@" in tail else tail


def _milestone_met(spec: MilestoneSpec, proven_effects: set[str], domains: dict[str, str]) -> bool:
    want_domain = _norm(domains.get(spec.domain_role, "")) if spec.domain_role else None
    for effect in proven_effects:
        for prefix in spec.effect_prefixes:
            if not effect.startswith(prefix):
                continue
            if want_domain is None:
                return True
            if _norm(_effect_domain(effect, prefix)) == want_domain:
                return True
    return False


# --- the reader ---------------------------------------------------------------------------------------

def read_ground_truth(
    scenario: Scenario,
    *,
    engagement_id: str | None = None,
    foothold_seen: bool | None = None,
    require_run_provenance: bool = False,
) -> GroundTruth:
    """Read the verified milestone vector for `scenario` from its durable ledger. READ-ONLY.

    `engagement_id` overrides `scenario.engagement_id` — the live runner pins a FRESH per-run id via
    SAGE_ENGAGEMENT_ID and passes the same id here, so C1 reads that run's clean ledger rather than a
    stale static one (the per-reset-UUID staleness fix)."""
    eid = engagement_id if engagement_id is not None else scenario.engagement_id
    data = engagement_ledger.load(eid)  # never raises; {} skeleton if missing
    hops = [h for h in (data.get("hops") or []) if isinstance(h, dict)]

    proven_effects: set[str] = set()
    any_proven = False
    for hop in hops:
        if hop_is_proven(hop, require_run_provenance=require_run_provenance):
            any_proven = True
            proven_effects |= _hop_effects(hop)

    spec = scenario.spec()
    candidates = scenario.milestone_subset or tuple(
        m for m in Milestone if m == Milestone.FOOTHOLD or m in spec
    )

    milestones: dict[Milestone, bool] = {}
    for m in candidates:
        if m == Milestone.FOOTHOLD:
            # Not a ledger effect: footholds come from live callbacks at runtime.
            milestones[m] = bool(foothold_seen) if foothold_seen is not None else any_proven
        elif m in spec:
            milestones[m] = _milestone_met(spec[m], proven_effects, scenario.domains)
        else:
            milestones[m] = False  # scenario asked for a milestone with no spec -> not measurable

    furthest = Milestone.FOOTHOLD
    for m in sorted(milestones):
        if milestones[m]:
            furthest = m

    # ISC-3: independent cross-checks. Disagreement = gauge-validity alarm, surfaced loudly.
    disagreements: list[Milestone] = []
    for m, probe in scenario.direct_probes.items():
        if m not in milestones:
            continue
        try:
            if bool(probe()) != milestones[m]:
                disagreements.append(m)
        except Exception:
            disagreements.append(m)  # a probe that can't run is itself a disagreement to investigate

    return GroundTruth(
        scenario=scenario.name,
        milestones=milestones,
        furthest=furthest,
        probe_disagreements=disagreements,
    )


# --- no-mutation guard (ISC-4) ------------------------------------------------------------------------

def ledger_state_hash(engagement_id: str | None = None) -> str:
    """SHA-256 over the on-disk ledger bytes for an engagement (or the whole state dir).

    Tests assert this is byte-identical before/after a C1 call: a reader that mutates
    the state it measures is a corrupted gauge.
    """
    h = hashlib.sha256()
    if engagement_id is not None:
        path = engagement_ledger.ledger_path(engagement_id)
        paths = [path] if os.path.exists(path) else []
    else:
        state_dir = engagement_ledger.state_dir()
        paths = []
        if os.path.isdir(state_dir):
            for name in sorted(os.listdir(state_dir)):
                p = os.path.join(state_dir, name)
                if os.path.isfile(p):
                    paths.append(p)
    for p in paths:
        h.update(p.encode("utf-8"))
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()
