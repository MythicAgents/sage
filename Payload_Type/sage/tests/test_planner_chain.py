"""Offline integration eval for the forward planner's effect-chaining (#1).

This is the confidence anchor that replaces 25-minute live validation runs: it drives the FULL modeled
attack chain through available_hops -> record_hop_result deterministically and asserts the planner names
each next hop, foothold -> NORTH DA -> forest-root (sevenkingdoms) DA, in milliseconds. If this passes,
the planner can chain the modeled path without a live run; if a future change breaks chaining, this fails
in CI instead of being discovered on a live solve.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402

NOW = "2026-06-09T12:00:00Z"
TTL = 600


def _samwell_north_foothold():
    return es.Foothold(
        callback_id="3", agent="merlin", host="CASTELBLACK", forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly", integrity="medium", alive=True, source="test", timestamp=NOW,
    )


def _post_ingest_state():
    """Grounded state right after the GOAD foothold collects+ingests: samwell holds GenericWrite on the
    STARKWALLPAPER GPO, which governs the NORTH domain (gpo-domain link)."""
    return es.EngagementState(
        objective="reach essos DA",
        footholds=[_samwell_north_foothold()],
        graph_facts=[
            es.GraphFact("generic-write:gpo:starkwallpaper", "bloodhound:cypher", NOW, TTL),
            es.GraphFact("gpo-domain:starkwallpaper:north.sevenkingdoms.local", "bloodhound:cypher", NOW, TTL),
        ],
    )


def _drive_chain(state, max_rounds=25):
    """Greedily take the top available hop each round, record it achieved, repeat until the planner has
    nothing left. Returns the ordered (technique, target) sequence and the final state."""
    sequence = []
    for _ in range(max_rounds):
        hops = es.available_hops(state)
        if not hops:
            break
        technique, target, _reason = hops[0]
        sequence.append((technique, target))
        effect = es._technique_effect(technique, target)
        callback_id = "3"
        satisfied = [effect]
        prefix, _, effect_domain = effect.partition(":")
        if prefix in {"da", "ea"} and effect_domain:
            satisfied.append(f"kerberos-context:{effect_domain}@callback:{callback_id}")
        state = es.record_effect_result(
            state,
            technique,
            target,
            effect,
            "achieved",
            {"source": "eval", "callback_id": callback_id},
            NOW,
            preconditions=es._technique_preconditions(technique, target),
            satisfied_effects=satisfied,
        )
    return sequence, state


def test_planner_chains_foothold_to_forest_root_offline():
    sequence, final = _drive_chain(_post_ingest_state())

    # The first hop is the graph-derived GPO abuse; effect-chaining carries the rest.
    assert sequence[0] == ("gpo-abuse", "starkwallpaper")
    for expected in (
        ("gpo-abuse", "starkwallpaper"),
        ("dcsync", "north.sevenkingdoms.local"),            # gpo-abuse -> ds-replication-rights -> dcsync
        ("golden-ticket", "north.sevenkingdoms.local"),     # krbtgt-hash -> NORTH DA
        ("sid-history-escalation", "north.sevenkingdoms.local"),  # child -> forest-root DA
        ("dcsync", "sevenkingdoms.local"),                  # forest-root replication
    ):
        assert expected in sequence, f"planner never named {expected}; got {sequence}"

    # Forest-root climb achieved in the modeled chain.
    effects = final.achieved_effects()
    assert "da:north.sevenkingdoms.local" in effects
    assert "da:sevenkingdoms.local" in effects
    assert "krbtgt-hash:sevenkingdoms.local" in effects


def test_planner_chain_terminates_no_loop():
    # The chain must converge (every hop's effect is recorded -> excluded next round), not loop forever.
    sequence, _ = _drive_chain(_post_ingest_state(), max_rounds=25)
    assert len(sequence) < 12  # the modeled NORTH->forest-root chain is ~5 hops; well under the cap
    assert len(sequence) == len(set(sequence))  # no hop repeats


def test_planner_idle_without_graph_or_effects():
    # A bare foothold (no graph facts, no achieved effects) yields no available hop -> agent must collect,
    # not flail. This is the RECON-phase grounding: nothing to chain yet.
    state = es.EngagementState(objective="x", footholds=[_samwell_north_foothold()])
    assert es.available_hops(state) == []


def test_gate_chains_gpo_abuse_to_dcsync_with_gpo_domain():
    # Regression for the 2026-06-09 planner<->gate mismatch: the gate's decision-state must carry the
    # gpo-domain link so _expand_implications chains gpo-abuse (system:{gpo}) -> ds-replication-rights ->
    # dcsync PROCEEDs. Without gpo-domain the gate DEFERred dcsync while the planner kept naming it -> churn.
    state = es.EngagementState(
        objective="x",
        footholds=[_samwell_north_foothold()],
        hops=[es.Hop(id="gpo-abuse:starkwallpaper", technique="gpo-abuse", target="starkwallpaper",
                     effect="system:starkwallpaper", status="achieved", evidence={"provenance": "run"},
                     preconditions=[], satisfied_effects=["system:starkwallpaper"], source="t", timestamp=NOW)],
        graph_facts=[es.GraphFact("gpo-domain:starkwallpaper:north.sevenkingdoms.local", "live-probe", NOW, 600)],
    )
    assert "ds-replication-rights:north.sevenkingdoms.local" in state.satisfied_predicates()
    d, _ = es.gate_decision("dcsync", "north.sevenkingdoms.local", state)
    assert d == es.GateDecision.PROCEED
