"""Observed-state projection + classifier tests (STRIPS forward-planner retired).

The forward planner (candidate_targets_from_state / available_hops / gate_decision) was removed in the
planner-as-gate retirement; what remains here covers the still-live observed-state surface:
controlled_principals_from_state projection, the reconcile -> graph-fact chain, render (observed-state
only), engagement_phase classification, access-context keying, and the SharpHound/SharpGPOAbuse classifier.
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


# --- engagement_phase classification ----------------------------------------------------------------

def test_phase_is_exploitation_when_grounded_hop_exists_without_graph_facts():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_samwell_foothold()],
        hops=[_hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local")],
    )

    assert engagement_state.engagement_phase(state).startswith("EXPLOITATION")


# --- render (observed-state only) -------------------------------------------------------------------

def test_render_no_longer_emits_planning_lines():
    # render is observed-state only; planning (NEXT GROUNDED ACTIONS / Phase) is retired.
    state = engagement_state.EngagementState(
        objective="reach essos DA", footholds=[_samwell_foothold()], graph_facts=[_gpo_fact("winterfell")]
    )
    out = engagement_state.render_engagement_state(state)
    assert "NEXT GROUNDED ACTIONS" not in out
    assert "Phase:" not in out


def test_render_empty_state_unchanged():
    out = engagement_state.render_engagement_state(engagement_state.EngagementState(objective="x"))
    assert "(no observed state yet)" in out
    assert "NEXT GROUNDED ACTIONS" not in out


# --- end-to-end projection -> reconcile -> graph fact ----------------------------------------------

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


def test_reconcile_projects_foothold_to_gpo_graph_fact():
    # The projection chain: foothold identity -> UPN principal -> reconcile (real MCP shape) -> graph fact.
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


# --- Part 1: collect-graph classifier + access-context keying ---------------------------------------

def test_classifier_maps_sharphound_to_collect_graph():
    assert intent_classifier.classify_tool_call(
        "execute_assembly", {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"}
    ) == ("collect-graph", "")


def test_classifier_maps_targeted_sharphound_domain_scope():
    assert intent_classifier.classify_tool_call(
        "execute_assembly",
        {
            "assembly_name": "SharpHound.exe",
            "assembly_arguments": "-c All --Domain target.example.local --OutputDirectory C:\\Users\\Public",
        },
    ) == ("collect-graph", "target.example.local")


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
        hops=[_hop("membership", "north.sevenkingdoms.local", "da:north.sevenkingdoms.local")],
    )
    k0 = engagement_state.access_context_key(base, fh)
    k1 = engagement_state.access_context_key(escalated, fh)
    assert k0 and k1 and k0 != k1  # a new privilege re-enables a fresh collection


def test_access_context_key_does_not_change_for_dumped_hash_or_unbound_creds():
    fh = _samwell_foothold()
    base = engagement_state.EngagementState(objective="x", footholds=[fh])
    artifacts_only = engagement_state.EngagementState(
        objective="x",
        footholds=[fh],
        hops=[
            _hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local"),
            _hop("dcsync-user", "arya@north.sevenkingdoms.local", "creds:arya@north.sevenkingdoms.local"),
        ],
    )
    assert engagement_state.access_context_key(base, fh) == engagement_state.access_context_key(artifacts_only, fh)


def test_access_context_key_does_not_change_for_kerberos_context_without_new_authority():
    fh = _samwell_foothold()
    base = engagement_state.EngagementState(objective="x", footholds=[fh])
    contextual = engagement_state.EngagementState(
        objective="x",
        footholds=[fh],
        hops=[
            _hop(
                "ensure-account-kerberos-context",
                "cersei@sevenkingdoms.local",
                "kerberos-account-context:cersei@sevenkingdoms.local@callback:2",
            ),
        ],
    )
    assert engagement_state.access_context_key(base, fh) == engagement_state.access_context_key(contextual, fh)


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

    # The RECON/collection-needed signal lives in the kept classification functions (consumed by
    # model.py's terminal/blocked reporting), not in the observed-state render.
    assert engagement_state.current_access_collection_missing(state) is True
    assert engagement_state.engagement_phase(state).startswith("RECON — current access")
    assert "GRAPH COLLECTION NEEDED" not in engagement_state.render_engagement_state(state)


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

    assert engagement_state.current_access_collection_missing(state) is True
    assert engagement_state.engagement_phase(state).startswith("RECON — current access")
