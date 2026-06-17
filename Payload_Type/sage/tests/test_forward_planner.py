"""Forward-planner tests (Part 2 of the STRIPS-planner fix).

Covers the loop-breaker: candidate_targets_from_state + available_hops + the render NEXT GROUNDED
ACTIONS section + controlled_principals_from_state, and an end-to-end projection→planner chain that
mirrors the 2026-06-09 clean-run stall (graph built, but no attack hop ever proposed).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402
import graph_reconciler  # noqa: E402
import intent_classifier  # noqa: E402

NOW = "2026-06-09T12:00:00Z"
TTL = 600


def _samwell_foothold(alive=True):
    return engagement_state.Foothold(
        callback_id="2",
        agent="apollo",
        host="CASTELBLACK",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=alive,
        source="test",
        timestamp=NOW,
    )


def _gpo_fact(host="winterfell"):
    return engagement_state.GraphFact(
        predicate=f"generic-write:gpo:{host}", source="bloodhound:cypher", timestamp=NOW, ttl_seconds=TTL
    )


def _hop(technique, target, effect, status="achieved"):
    return engagement_state.Hop(
        id=f"{technique}:{target}", technique=technique, target=target, effect=effect, status=status,
        evidence={"provenance": "run"}, preconditions=[], satisfied_effects=[effect],
        source="test", timestamp=NOW,
    )


# --- controlled_principals_from_state ---------------------------------------------------------------

def test_controlled_principals_projects_foothold_to_upn():
    state = engagement_state.EngagementState(objective="x", footholds=[_samwell_foothold()])
    assert graph_reconciler.controlled_principals_from_state(state) == [
        "samwell.tarly@north.sevenkingdoms.local"
    ]


def test_controlled_principals_skips_dead_foothold():
    state = engagement_state.EngagementState(objective="x", footholds=[_samwell_foothold(alive=False)])
    assert graph_reconciler.controlled_principals_from_state(state) == []


def test_controlled_principals_includes_dcsynced_creds():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_samwell_foothold()],
        hops=[_hop("dcsync-user", "cersei.lannister@sevenkingdoms.local",
                   "creds:cersei.lannister@sevenkingdoms.local")],
    )
    principals = graph_reconciler.controlled_principals_from_state(state)
    assert "samwell.tarly@north.sevenkingdoms.local" in principals
    assert "cersei.lannister@sevenkingdoms.local" in principals


# --- candidate_targets_from_state -------------------------------------------------------------------

def test_candidate_targets_inverts_graph_and_effect_predicates():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_samwell_foothold()],
        hops=[_hop("dcsync", "sevenkingdoms.local", "krbtgt-hash:sevenkingdoms.local")],
        graph_facts=[_gpo_fact("winterfell")],
    )
    cands = set(engagement_state.candidate_targets_from_state(state))
    assert ("gpo-abuse", "winterfell") in cands
    assert ("golden-ticket", "sevenkingdoms.local") in cands
    assert ("sid-history-escalation", "sevenkingdoms.local") in cands
    # krbtgt-hash still enumerates ticket-forge candidates; DCSync candidates come from explicit
    # replication rights or DA/EA paired with a live callback-scoped Kerberos context.


# --- available_hops ---------------------------------------------------------------------------------

def test_available_hops_surfaces_gpo_abuse_when_graph_confirms_edge():
    # Mirrors the clean-run state right after ingest: live samwell foothold + graph shows GenericWrite
    # on the GPO affecting winterfell. The planner MUST name gpo-abuse so the agent stops re-collecting.
    state = engagement_state.EngagementState(
        objective="reach essos DA", footholds=[_samwell_foothold()], graph_facts=[_gpo_fact("winterfell")]
    )
    hops = engagement_state.available_hops(state)
    assert ("gpo-abuse", "winterfell") in [(t, tgt) for t, tgt, _ in hops]


def test_available_hops_empty_without_graph():
    # No graph facts -> graph-derived preconditions are UNKNOWN -> nothing is affirmatively available.
    state = engagement_state.EngagementState(objective="x", footholds=[_samwell_foothold()])
    assert engagement_state.available_hops(state) == []


def test_available_hops_excludes_already_achieved():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_samwell_foothold()],
        hops=[_hop("gpo-abuse", "winterfell", "system:winterfell")],  # already achieved
        graph_facts=[_gpo_fact("winterfell")],
    )
    assert ("gpo-abuse", "winterfell") not in [(t, tgt) for t, tgt, _ in engagement_state.available_hops(state)]


def test_available_hops_chains_golden_ticket_after_krbtgt():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_samwell_foothold()],
        hops=[_hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local")],
    )
    available = [(t, tgt) for t, tgt, _ in engagement_state.available_hops(state)]
    assert ("golden-ticket", "north.sevenkingdoms.local") in available


def test_phase_is_exploitation_when_grounded_hop_exists_without_graph_facts():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_samwell_foothold()],
        hops=[_hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local")],
    )

    assert engagement_state.engagement_phase(state).startswith("EXPLOITATION")


# --- render NEXT GROUNDED ACTIONS -------------------------------------------------------------------

def test_render_includes_next_grounded_actions_when_available():
    state = engagement_state.EngagementState(
        objective="reach essos DA", footholds=[_samwell_foothold()], graph_facts=[_gpo_fact("winterfell")]
    )
    out = engagement_state.render_engagement_state(state)
    assert "NEXT GROUNDED ACTIONS" in out
    assert "gpo-abuse → winterfell" in out
    assert "do NOT re-collect" in out


def test_render_empty_state_unchanged():
    out = engagement_state.render_engagement_state(engagement_state.EngagementState(objective="x"))
    assert "(no observed state yet)" in out
    assert "NEXT GROUNDED ACTIONS" not in out


# --- end-to-end projection -> planner (the chain that was never wired) -------------------------------

class _FakeTool:
    """Query-aware fake mirroring the real per-target-kind reconcile calls + data.literals shape."""

    def __init__(self):
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        if "(t:GPO)" in args.get("query", ""):
            return json.dumps({"info_type": "run", "success": True, "data": {
                "nodes": {}, "edges": [],
                "literals": [{"value": "STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL", "key": "name"}]}})
        return json.dumps({"info_type": "run", "success": True,
                           "data": {"nodes": {}, "edges": [], "literals": []}})


class _FakeMCP:
    def __init__(self, tool):
        self.tool = tool

    async def get_tool_by_name(self, name, server_name=None):
        del name, server_name
        return self.tool


def test_reconcile_then_plan_names_gpo_abuse():
    # The chain that was never wired: foothold identity -> UPN principal -> reconcile (real MCP shape) ->
    # graph fact -> forward planner names gpo-abuse. This is the loop-breaker, end to end.
    import asyncio

    foothold = _samwell_foothold()
    principals = graph_reconciler.controlled_principals_from_state(
        engagement_state.EngagementState(objective="x", footholds=[foothold])
    )
    assert principals == ["samwell.tarly@north.sevenkingdoms.local"]

    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCP(_FakeTool()), principals, "reach essos DA", NOW, TTL
    ))
    preds = [f.predicate for f in facts]
    assert "generic-write:gpo:starkwallpaper" in preds
    assert "gpo-domain:starkwallpaper:north.sevenkingdoms.local" in preds  # enables the effect-chain

    state = engagement_state.EngagementState(objective="x", footholds=[foothold], graph_facts=facts)
    assert ("gpo-abuse", "starkwallpaper") in [(t, tgt) for t, tgt, _ in engagement_state.available_hops(state)]


def test_gpo_grounded_action_suppressed_after_downstream_domain_proof():
    foothold = _samwell_foothold()
    facts = [
        engagement_state.GraphFact(
            predicate="generic-write:gpo:wallpaperpolicy",
            source="bloodhound:cypher",
            timestamp=NOW,
            ttl_seconds=TTL,
        ),
        engagement_state.GraphFact(
            predicate="gpo-domain:wallpaperpolicy:child.example.local",
            source="bloodhound:cypher",
            timestamp=NOW,
            ttl_seconds=TTL,
        ),
    ]
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        graph_facts=facts,
        hops=[
            _hop(
                "dcsync",
                "child.example.local",
                "krbtgt-hash:child.example.local",
            )
        ],
    )

    available = [(t, tgt) for t, tgt, _ in engagement_state.available_hops(state)]

    assert ("gpo-abuse", "wallpaperpolicy") not in available


# --- sync guard: every modeled, structurally-enumerable technique has a candidate source -------------

# --- Part 1: collect-graph (deterministic collect-once-per-privilege) -------------------------------

def test_classifier_maps_sharphound_to_collect_graph():
    assert intent_classifier.classify_tool_call(
        "execute_assembly", {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"}
    ) == ("collect-graph", "")


def test_classifier_does_not_mark_sharphound_registration_as_collect_graph():
    assert intent_classifier.classify_tool_call(
        "register_file", {"filename": "SharpHound.exe"}
    ) is None


def test_classifier_keeps_sharpgpoabuse_as_gpo_abuse_not_collect():
    technique, _ = intent_classifier.classify_tool_call(
        "execute_assembly", {"assembly": "SharpGPOAbuse.exe", "arguments": "--gponame StarkWallpaper --AddComputerTask"}
    )
    assert technique == "gpo-abuse"


def test_access_context_key_changes_with_privilege():
    fh = _samwell_foothold()
    base = engagement_state.EngagementState(objective="x", footholds=[fh])
    escalated = engagement_state.EngagementState(
        objective="x", footholds=[fh],
        hops=[_hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local")],
    )
    k0 = engagement_state.access_context_key(base, fh)
    k1 = engagement_state.access_context_key(escalated, fh)
    assert k0 and k1 and k0 != k1  # a new privilege re-enables a fresh collection


def test_collect_graph_gate_first_proceeds_then_skips_same_access():
    fh = _samwell_foothold()
    state = engagement_state.EngagementState(objective="x", footholds=[fh])
    key = engagement_state.access_context_key(state, fh)
    decision, _ = engagement_state.gate_decision("collect-graph", key, state)
    assert decision == engagement_state.GateDecision.PROCEED  # first collection at this access proceeds

    built = engagement_state.record_hop_result(
        state, "collect-graph", key, "achieved", {"source": "t", "graph_verified": True}, NOW
    )
    decision2, _ = engagement_state.gate_decision("collect-graph", key, built)
    assert decision2 == engagement_state.GateDecision.SKIP  # re-collection at the same access is SKIPped


def test_collect_graph_not_a_planner_candidate():
    # collect-graph is an action the agent takes in RECON, never a forward-planner "next attack hop".
    fh = _samwell_foothold()
    key = engagement_state.access_context_key(engagement_state.EngagementState(objective="x", footholds=[fh]), fh)
    built = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="x", footholds=[fh]),
        "collect-graph", key, "achieved", {"source": "t"}, NOW,
    )
    assert all(t != "collect-graph" for t, _, _ in engagement_state.available_hops(built))


def test_phase_requests_collection_after_access_key_changes_and_no_next_action():
    fh = _samwell_foothold()
    base = engagement_state.EngagementState(objective="reach essos.local forest", footholds=[fh])
    old_key = engagement_state.access_context_key(base, fh)
    state = engagement_state.EngagementState(
        objective="reach essos.local forest",
        footholds=[fh],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="domain-collected:sevenkingdoms.local",
                source="bloodhound:test",
                timestamp=NOW,
                ttl_seconds=TTL,
            )
        ],
        hops=[
            _hop("collect-graph", old_key, f"graph-built:{old_key}"),
            _hop("forge-golden-ticket", "sevenkingdoms.local", "da:sevenkingdoms.local"),
            _hop(
                "ensure-kerberos-context",
                "sevenkingdoms.local",
                "kerberos-context:sevenkingdoms.local@callback:2",
            ),
            _hop("dcsync", "sevenkingdoms.local", "krbtgt-hash:sevenkingdoms.local"),
        ],
    )

    assert engagement_state.current_access_collection_missing(state) is True
    assert engagement_state.engagement_phase(state).startswith("RECON — current access")
    assert "GRAPH COLLECTION NEEDED" in engagement_state.render_engagement_state(state)


def test_legacy_acl_side_quests_do_not_block_collection_after_domain_control():
    fh = _samwell_foothold()
    base = engagement_state.EngagementState(objective="obtain administrative control of essos.local", footholds=[fh])
    old_key = engagement_state.access_context_key(base, fh)
    state = engagement_state.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[fh],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="generic-write:computer:kingslanding",
                source="bloodhound:test",
                timestamp=NOW,
                ttl_seconds=TTL,
            ),
            engagement_state.GraphFact(
                predicate="write-dacl:domain:sevenkingdoms.local",
                source="bloodhound:test",
                timestamp=NOW,
                ttl_seconds=TTL,
            ),
        ],
        hops=[
            _hop("collect-graph", old_key, f"graph-built:{old_key}"),
            _hop("forge-golden-ticket", "sevenkingdoms.local", "da:sevenkingdoms.local"),
            _hop(
                "ensure-kerberos-context",
                "sevenkingdoms.local",
                "kerberos-context:sevenkingdoms.local@callback:2",
            ),
            _hop("dcsync", "sevenkingdoms.local", "krbtgt-hash:sevenkingdoms.local"),
        ],
    )

    available = [(technique, target) for technique, target, _ in engagement_state.available_hops(state)]

    assert ("rbcd-standin", "kingslanding") not in available
    assert ("dcsync-rights-grant", "sevenkingdoms.local") not in available
    assert engagement_state.current_access_collection_missing(state) is True
    assert engagement_state.engagement_phase(state).startswith("RECON — current access")


def test_candidate_sources_cover_enumerable_techniques():
    # dcsync-user (needs a specific user), collect-graph (Part 1, not in this pass), and
    # domain-admin-membership-check (read-proof emitted from a command/result pair) are intentionally not
    # auto-enumerated; every OTHER technique must be reachable from a candidate-source prefix.
    sourced = {tech for _prefix, techs in engagement_state._CANDIDATE_SOURCES for tech in techs}
    exempt = {"dcsync-user", "collect-graph", "domain-admin-membership-check"}
    for technique in engagement_state.TECHNIQUE_MODEL:
        if technique in exempt:
            continue
        assert technique in sourced, f"{technique} has no candidate source — planner will never suggest it"
