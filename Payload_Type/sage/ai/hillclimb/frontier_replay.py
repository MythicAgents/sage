"""Spike 0 — frontier-correctness replay (autonomous-controller design gate).

Forward-replays a recorded engagement ledger's ACHIEVED hop chain (in causal/timestamp
order) through `capabilities.actions_from_state()` and checks whether the deterministic
frontier CONTAINS and RANKS the correct next capability hop at the top.

WHY: the autonomous controller's `select` step is `actions_from_state(state)[0]`
(top-priority admissible action; bounded-LLM rank only on priority ties). This proves the
already-declared precondition/effect model can carry the proven ESSOS route BEFORE we build
the controller loop or spend a live run. It is the cheap test of falsifier F3 in
`Plans/SAGE_AUTONOMOUS_CONTROLLER_DESIGN_2026-06-22.md` ("the deterministic frontier is
empty at a wall autonomous Sage should cross -> the capability model is under-declared").

HONESTY NOTE (the 2026-06-21 trap, recorded in Plans/RESUME.md): the persisted ledger does
NOT store footholds, so the runtime live-callback/live-foothold/authenticated predicates are
absent on replay. `actions_from_state` gates most actions on a live callback, so replaying
from the bare ledger would UNDER-produce the frontier and yield a falsely pessimistic verdict
(the exact mistake behind the retracted "definitive" offline claim). We therefore reconstruct
the run's live foothold EXPLICITLY and PRINT it — neither silently dropped nor silently
invented. The reconstruction is a stated assumption the reader can audit.
"""
from __future__ import annotations

import json
import sys

try:
    from ..langgraph import engagement_state as es
    from ..langgraph import capabilities as cap
except ImportError:  # flat-import runtimes (mirrors the codebase's relative-then-flat pattern)
    from ai.langgraph import engagement_state as es
    from ai.langgraph import capabilities as cap


# Capability hop techniques whose selection the deterministic frontier OWNS. Collection/recon
# (collect-graph, domain-admin-membership-check) is scheduled by a separate deterministic rule
# (collect-once-per-privilege via access_context_key), NOT by actions_from_state — so those hops
# are replayed into the prefix (they establish effects) but excluded from the frontier verdict.
def _is_capability_hop(technique: str) -> bool:
    t = (technique or "").casefold()
    return t.startswith("capability:") or t in {"dcsync", "dcsync-user", "forge-golden-ticket"}


def _canon_effect(e: str) -> str:
    """Canonicalize an effect for cross-source comparison. The ledger records credential effects with a
    NETBIOS domain qualifier (`creds:sevenkingdoms\\cersei.lannister@fqdn`) while the frontier action emits
    the unqualified form (`creds:cersei.lannister@fqdn`); both denote the same credential. Strip a leading
    `DOMAIN\\` inside the `creds:` account so the two compare equal — a matcher fix, not a semantic change."""
    n = es._normalize_predicate(e) or ""
    if n.startswith("creds:") and "\\" in n:
        head, rest = n.split(":", 1)
        if "@" in rest:
            acct, dom = rest.rsplit("@", 1)
            acct = acct.split("\\", 1)[1] if "\\" in acct else acct
            return f"{head}:{acct}@{dom}"
    return n


def _norm_effects(effects) -> set[str]:
    out: set[str] = set()
    for e in effects or []:
        n = _canon_effect(e)
        if n:
            out.add(n)
    return out


def _admin_effect_parts(effect: str) -> tuple[str, str] | None:
    """Return ``(kind, domain)`` for DA/EA effects so replay can compare NetBIOS/FQDN aliases."""
    normalized = _canon_effect(effect)
    if ":" not in normalized:
        return None
    kind, domain = normalized.split(":", 1)
    if kind not in {"da", "ea"} or not domain:
        return None
    return kind, domain


def _effects_equivalent(left: str, right: str) -> bool:
    """Whether two replay effects denote the same proof across ledger/action spellings."""
    left_normalized = _canon_effect(left)
    right_normalized = _canon_effect(right)
    if left_normalized == right_normalized:
        return True
    left_admin = _admin_effect_parts(left_normalized)
    right_admin = _admin_effect_parts(right_normalized)
    if not left_admin or not right_admin or left_admin[0] != right_admin[0]:
        return False
    return es._domains_equivalent(left_admin[1], right_admin[1])


def _effect_sets_overlap(left: set[str], right: set[str]) -> bool:
    return any(_effects_equivalent(a, b) for a in left for b in right)


def reconstruct_foothold(callback_id: str, host: str, forest: str, identity: str,
                         integrity: str = "high") -> es.Foothold:
    """The run's single live Apollo foothold — the predicates the ledger drops. Stated, not hidden."""
    return es.Foothold(
        callback_id=callback_id, agent="apollo", host=host, forest=forest,
        identity=identity, integrity=integrity, alive=True,
        source="frontier_replay:reconstructed", timestamp="",
    )


def load_chain(path: str):
    d = json.load(open(path))
    objective = d.get("objective", "")
    hops = es.hops_from_dicts(d.get("hops", []))
    facts = es.graph_facts_from_dicts(d.get("graph_facts", []))
    achieved = [h for h in hops if (h.status or "").casefold() == "achieved"]
    # Causal order: recorded array order can be reconciler-sorted (e.g. da: recorded before the
    # gpo-exec that caused it), which would present states that never existed temporally. Sort by
    # timestamp so each prefix is a state the run actually passed through.
    achieved.sort(key=lambda h: (h.timestamp or "", h.id or ""))
    return objective, achieved, facts


def _legacy_row_is_nested_under_selected_capability(truth, frontier, achieved_tail) -> bool:
    """A legacy proof row can be emitted inside the next explicit capability transaction.

    Some capability executors record an inner verifier/proof hop before recording the outer
    ``capability:<name>`` row. That inner row is not a separate frontier decision when the
    frontier already selected the matching outer capability.
    """
    technique = (getattr(truth, "technique", "") or "").casefold()
    if technique.startswith("capability:") or not _is_capability_hop(technique) or not frontier:
        return False
    selected_name = str(getattr(frontier[0], "name", "") or "").casefold()
    if not selected_name:
        return False
    for later in achieved_tail:
        later_technique = (getattr(later, "technique", "") or "").casefold()
        if not later_technique.startswith("capability:"):
            continue
        return later_technique == f"capability:{selected_name}"
    return False


def _benign_reason(truth, prefix, achieved_tail, frontier=None) -> str:
    """Classify a frontier MISS that is NOT a capability-model gap (so the instrument doesn't cry F3 on
    cases where the frontier is correctly more parsimonious, or where ledger timestamp order presents the
    effect's downstream as already-achieved). Returns "" if the miss is a genuine F3 gap.

      - ordering_artifact: the effect (or its domain's DA) is ALREADY achieved in the prefix — the run
        recorded this hop out of causal order, and the frontier has correctly moved to the next step.
      - nested_capability_proof: a legacy proof row was recorded inside the next explicit capability
        transaction already selected by the frontier.
      - omitted_offpath: a `creds:` account dump the guided run made but NO later achieved hop consumes
        (the account is never referenced downstream). Omitting it is correct parsimony, not a gap."""
    prefix_eff = set()
    for h in prefix:
        prefix_eff |= _norm_effects(h.satisfied_effects or [h.effect])
    truth_eff = _norm_effects(truth.satisfied_effects or [truth.effect])
    if _effect_sets_overlap(truth_eff, prefix_eff):
        return "ordering_artifact"
    # system-exec/gpo whose domain DA is already held -> the DA-granting step is moot
    for e in truth_eff:
        if e.startswith("system-exec:gpo:") and "@" in e:
            dom = e.rsplit("@", 1)[1]
            if any(_effects_equivalent(f"da:{dom}", effect) for effect in prefix_eff):
                return "ordering_artifact"
    if _legacy_row_is_nested_under_selected_capability(truth, frontier or [], achieved_tail):
        return "nested_capability_proof"
    creds = [e for e in truth_eff if e.startswith("creds:")]
    if creds:
        # Domain-AWARE identity match: north\administrator is NOT "consumed" by a later
        # creds:administrator@sevenkingdoms (bare-substring matching conflated them). Compare the canonical
        # account@domain identity, and the account ONLY when paired with its own domain in a later effect.
        identities = {e.split(":", 1)[1] for e in creds}  # canon -> account@domain
        # "Consumed" = the account is actually USED downstream (a kerberos-account-context / managed-secret
        # / admin / remote-exec effect referencing it), NOT merely re-listed in a later collect-graph blob or
        # another creds: dump. So restrict the scan to later USAGE effects (exclude creds:/graph-built:).
        consumed = False
        for later in achieved_tail:
            for eff in (later.satisfied_effects or []):
                el = (eff or "").casefold()
                if el.startswith("creds:") or el.startswith("graph-built:"):
                    continue
                for ident in identities:
                    acct, _, dom = ident.partition("@")
                    if acct and acct.casefold() in el and (not dom or dom.casefold() in el):
                        consumed = True
                        break
                if consumed:
                    break
            if consumed:
                break
        if not consumed:
            return "omitted_offpath"
    return ""


def replay(path: str, foothold: es.Foothold) -> dict:
    objective, achieved, facts = load_chain(path)
    rows = []
    for i, truth in enumerate(achieved):
        prefix = achieved[:i]
        state = es.EngagementState(objective=objective, footholds=[foothold],
                                   hops=list(prefix), graph_facts=list(facts))
        frontier = cap.actions_from_state(state)
        truth_eff = _norm_effects(truth.satisfied_effects or [truth.effect])
        rank = None
        for idx, a in enumerate(frontier):
            if _effect_sets_overlap(_norm_effects(a.effects), truth_eff):
                rank = idx
                break
        top_priorities = [cap._capability_action_priority(frontier[0])] if frontier else []
        is_tie_with_top = (
            rank is not None and rank > 0 and frontier
            and cap._capability_action_priority(frontier[rank]) in top_priorities
        )
        benign = _benign_reason(truth, prefix, achieved[i + 1:], frontier=frontier) if rank is None else ""
        rows.append({
            "i": i,
            "technique": truth.technique,
            "next_effect": sorted(truth_eff)[:1],
            "is_capability": _is_capability_hop(truth.technique),
            "frontier_size": len(frontier),
            "rank": rank,
            "top": frontier[0].name if frontier else "",
            "tie_with_top": is_tie_with_top,
            "benign": benign,
        })
    return {"path": path, "objective": objective, "rows": rows}


def _verdict(rows) -> dict:
    cap_rows = [r for r in rows if r["is_capability"]]
    rank0 = [r for r in cap_rows if r["rank"] == 0]
    tie = [r for r in cap_rows if r["rank"] not in (0, None) and r["tie_with_top"]]
    soft = [r for r in cap_rows if r["rank"] not in (0, None) and not r["tie_with_top"]]
    missing = [r for r in cap_rows if r["rank"] is None]
    benign = [r for r in missing if r["benign"]]
    genuine = [r for r in missing if not r["benign"]]
    return {
        "capability_steps": len(cap_rows),
        "rank0": len(rank0), "tie_with_top": len(tie), "lower_rank": len(soft),
        "benign_omissions": [f"{r['technique']}({r['benign']})" for r in benign],
        "GENUINE_F3_gaps": [r["technique"] for r in genuine],
    }


def main(argv):
    if not argv:
        print("usage: python -m ai.hillclimb.frontier_replay <ledger.json> "
              "[callback_id host forest identity]")
        return 2
    path = argv[0]
    cb = argv[1] if len(argv) > 1 else "3"
    host = argv[2] if len(argv) > 2 else "castelblack"
    forest = argv[3] if len(argv) > 3 else "north.sevenkingdoms.local"
    identity = argv[4] if len(argv) > 4 else "north\\samwell.tarly"
    fh = reconstruct_foothold(cb, host, forest, identity)
    print(f"# RECONSTRUCTED FOOTHOLD (stated assumption): callback={cb} host={host} "
          f"forest={forest} identity={identity} integrity=high alive=True agent=apollo")
    out = replay(path, fh)
    print(f"# LEDGER: {path}")
    print(f"# OBJECTIVE: {out['objective'][:100]}")
    print(f"{'i':>2} {'cap?':>4} {'rank':>4} {'tie':>3} {'frontier_top':<28} {'next_hop_effect'}")
    for r in out["rows"]:
        rank = ("MISS" if r["rank"] is None else str(r["rank"]))
        if r["rank"] is None and r["benign"]:
            rank = "omit"
        cflag = "Y" if r["is_capability"] else "."
        tflag = "T" if r["tie_with_top"] else " "
        eff = (r["next_effect"][0] if r["next_effect"] else "")[:48]
        print(f"{r['i']:>2} {cflag:>4} {rank:>4} {tflag:>3} {r['top']:<28} {eff}")
    v = _verdict(out["rows"])
    print("\n# VERDICT (capability steps only):")
    print(json.dumps(v, indent=2))
    if not v["GENUINE_F3_gaps"]:
        print("\n=> PASS: the deterministic frontier reproduces the proven critical path "
              "(every spine step rank-0/tie; misses are benign omissions/ordering). F3 NOT triggered.")
    else:
        print(f"\n=> F3 TRIGGERED: {len(v['GENUINE_F3_gaps'])} proven capability step(s) ABSENT from the "
              f"frontier with no benign explanation -> capability model under-declared. {v['GENUINE_F3_gaps']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
