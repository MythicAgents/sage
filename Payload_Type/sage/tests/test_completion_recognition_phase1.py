"""Phase-1 objective-completion recognition: an in-domain DA hop should complete WITHOUT a separately-recorded
kerberos-context effect, while cross-forest objectives stay strict and under-reach is prevented.

Negative cases are first-class (Advisor review): a low-priv in-domain callback, DA-without-krbtgt, a cross-forest
objective without an in-domain foothold, and a parent-domain foothold must all NOT complete.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402


CHILD_DA_OBJECTIVE = "escalate to Domain Admin of north.sevenkingdoms.local and DCSync its krbtgt"
ESSOS_OBJECTIVE = "obtain administrative control of essos.local"


def _foothold(callback_id="14", forest="north.sevenkingdoms.local", identity="NORTH\\samwell.tarly",
              agent="apollo", alive=True):
    return es.Foothold(
        callback_id=callback_id, agent=agent, host="castelblack", forest=forest,
        identity=identity, integrity="medium", alive=alive, source="test",
        timestamp="2026-06-19T12:00:00Z",
    )


def _achieved_hop(effect, task_id="1", technique="capability:seed", target="seed"):
    return es.Hop(
        id=f"{technique}:{target}", technique=technique, target=target, effect=effect,
        status="achieved", evidence={"mythic_task_id": task_id, "source": "test"},
        preconditions=[], satisfied_effects=[effect], source="test",
        timestamp="2026-06-19T12:00:00Z",
    )


def _complete(state):
    return es.engagement_phase(state).startswith("COMPLETE-CANDIDATE")


def test_in_domain_da_with_krbtgt_completes_without_kerberos_context():
    # child-da happy path: DA + krbtgt proven from a live in-domain callback, no kerberos-context effect.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold()],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north.sevenkingdoms.local", "664",
                          technique="dcsync", target="north.sevenkingdoms.local"),
        ],
    )
    candidates = es.objective_completion_candidates(state)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["domain"] == "north.sevenkingdoms.local"
    assert c["access_effect"].startswith("in-domain-callback:")
    assert c["callback_id"] == "14"
    assert c.get("key_effect") == "krbtgt-hash:north.sevenkingdoms.local"
    assert _complete(state)


def test_in_domain_da_without_krbtgt_does_not_complete():
    # Objective demands krbtgt; DA + in-domain callback held, but krbtgt not dumped yet -> keep climbing.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold()],
        hops=[_achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local")],
    )
    assert not _complete(state)


def test_low_priv_in_domain_callback_without_da_does_not_complete():
    # Advisor case 2: a live in-domain callback with NO verifier-proven da:/ea: must not complete.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold()],
        hops=[_achieved_hop("creds:castelblack", "10", target="castelblack")],
    )
    assert es.objective_completion_candidates(state) == []
    assert not _complete(state)


def test_cross_forest_objective_stays_strict_without_in_domain_callback_or_cert():
    # Foothold is in NORTH; objective is ESSOS. No essos kerberos-context, no cert, no essos foothold.
    state = es.EngagementState(
        objective=ESSOS_OBJECTIVE,
        footholds=[_foothold(forest="north.sevenkingdoms.local", identity="NORTH\\samwell.tarly")],
        hops=[
            _achieved_hop("da:essos.local", "655", target="domain=essos.local"),
            _achieved_hop("krbtgt-hash:essos.local", "664", technique="dcsync", target="essos.local"),
        ],
    )
    assert es.objective_completion_candidates(state) == []
    assert not _complete(state)


def test_netbios_forest_label_matches_child_fqdn():
    # Degraded env: no NetBIOS->FQDN map, so the foothold forest is the bare label "north". It still witnesses
    # the FQDN target via the bare-label arm of _domains_equivalent.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(forest="north", identity="NORTH\\samwell.tarly")],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north.sevenkingdoms.local", "664",
                          technique="dcsync", target="north.sevenkingdoms.local"),
        ],
    )
    assert _complete(state)


def test_cross_forest_same_netbios_label_does_not_complete():
    # The cross-forest leak (Forge): a live foothold in a DIFFERENT forest that merely shares the NetBIOS
    # label "north" must NOT witness in-domain access to north.sevenkingdoms.local. Matching is forest-only.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(forest="north.otherforest.local", identity="NORTH\\evil")],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north.sevenkingdoms.local", "664",
                          technique="dcsync", target="north.sevenkingdoms.local"),
        ],
    )
    assert es.objective_completion_candidates(state) == []
    assert not _complete(state)


def test_completes_when_da_and_krbtgt_domain_forms_differ():
    # da: recorded FQDN, krbtgt-hash recorded NetBIOS (record-path disagreement). Domain-equivalent matching
    # must still complete rather than silently loop forever (the relocated-bug under-reach Forge flagged).
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(forest="north.sevenkingdoms.local", identity="NORTH\\samwell.tarly")],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north", "664", technique="dcsync", target="north"),
        ],
    )
    assert _complete(state)


def test_parent_domain_foothold_does_not_satisfy_child_in_domain():
    # Only a parent-domain (sevenkingdoms.local) foothold while the objective targets child north.sevenkingdoms.local.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(forest="sevenkingdoms.local", identity="SEVENKINGDOMS\\cersei.lannister")],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north.sevenkingdoms.local", "664",
                          technique="dcsync", target="north.sevenkingdoms.local"),
        ],
    )
    assert es.objective_completion_candidates(state) == []
    assert not _complete(state)


def test_in_domain_da_implies_ds_replication_rights():
    # DA from a live in-domain foothold -> ds-replication-rights (so dcsync becomes selectable). Fixes the
    # DA->DCSync loop where the right was only implied from a live kerberos-context the in-forest path lacked.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(forest="north.sevenkingdoms.local", identity="NORTH\\samwell.tarly")],
        hops=[_achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local")],
    )
    assert "ds-replication-rights:north.sevenkingdoms.local" in state.satisfied_predicates()


def test_same_netbios_label_cross_forest_does_not_imply_replication():
    # A foothold NORTH\\evil in an UNRELATED forest (north.otherforest.local) + da:north.sevenkingdoms.local
    # must NOT grant ds-replication-rights — FQDN-only matching; the bare "north" label is not trusted (the
    # cross-forest DCSync hazard Forge caught).
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(forest="north.otherforest.local", identity="NORTH\\evil")],
        hops=[_achieved_hop("da:north.sevenkingdoms.local", "1", target="domain=north.sevenkingdoms.local")],
    )
    assert "ds-replication-rights:north.sevenkingdoms.local" not in state.satisfied_predicates()


def test_cross_forest_da_without_context_or_in_domain_foothold_keeps_gate():
    # foothold is NORTH; da:essos.local but no kerberos-context:essos and no in-domain essos foothold ->
    # replication rights NOT implied (cross-forest must still earn the usable context first).
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold(forest="north.sevenkingdoms.local", identity="NORTH\\samwell.tarly")],
        hops=[_achieved_hop("da:essos.local", "1", target="domain=essos.local")],
    )
    assert "ds-replication-rights:essos.local" not in state.satisfied_predicates()


def test_dead_in_domain_callback_does_not_complete():
    # A dead (not alive) in-domain foothold is not usable access -> no candidate.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(alive=False)],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north.sevenkingdoms.local", "664",
                          technique="dcsync", target="north.sevenkingdoms.local"),
        ],
    )
    assert es.objective_completion_candidates(state) == []
    assert not _complete(state)


def test_sage_control_callback_is_not_an_in_domain_witness():
    # A live SAGE control callback in the target domain must not count as offensive in-domain access.
    state = es.EngagementState(
        objective=CHILD_DA_OBJECTIVE,
        footholds=[_foothold(agent="sage")],
        hops=[
            _achieved_hop("da:north.sevenkingdoms.local", "655", target="domain=north.sevenkingdoms.local"),
            _achieved_hop("krbtgt-hash:north.sevenkingdoms.local", "664",
                          technique="dcsync", target="north.sevenkingdoms.local"),
        ],
    )
    assert es.objective_completion_candidates(state) == []
    assert not _complete(state)
