import asyncio
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import access_reconciler  # noqa: E402
import engagement_state  # noqa: E402
import graph_reconciler  # noqa: E402


NOW = "2026-06-06T12:00:00Z"
TTL_SECONDS = 300


def _north_foothold():
    return engagement_state.Foothold(
        callback_id="10",
        agent="Apollo",
        host="CASTLEBLACK",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp=NOW,
    )


def _essos_foothold():
    return engagement_state.Foothold(
        callback_id="20",
        agent="Apollo",
        host="MEEREEN",
        forest="essos.local",
        identity="ESSOS\\jorah",
        integrity="high",
        alive=True,
        source="test",
        timestamp=NOW,
    )


def _gpo_fact(timestamp=NOW):
    return engagement_state.GraphFact(
        predicate="generic-write:gpo:winterfell",
        source="bloodhound:cypher",
        timestamp=timestamp,
        ttl_seconds=TTL_SECONDS,
    )


def test_project_graph_predicates_maps_gpo_edge_to_host_predicate():
    records = [
        {
            "principal": "samwell.tarly",
            "type": "GenericWrite",
            "target_kind": "gpo",
            "gpo": "STARKWALLPAPER",
            "linked_computers": ["WINTERFELL.NORTH.SEVENKINGDOMS.LOCAL"],
        }
    ]

    facts = graph_reconciler.project_graph_predicates(records, NOW, TTL_SECONDS)

    assert facts == [
        engagement_state.GraphFact(
            predicate="generic-write:gpo:winterfell",
            source="bloodhound:cypher",
            timestamp=NOW,
            ttl_seconds=TTL_SECONDS,
        )
    ]


def test_gpo_abuse_gate_is_belief_aware_on_graph_acl():
    # belief: unknown != false. The gate only blocks on a graph-derived ACL precondition when
    # graph data has actually been reconciled (so it can deny it); with no graph data it must PROCEED.
    foothold = _north_foothold()

    # (1) No graph data at all -> generic-write is UNKNOWN -> do NOT block (the over-block fix).
    no_graph = engagement_state.EngagementState(objective="gpo abuse", footholds=[foothold])
    decision, _reason = engagement_state.gate_decision("gpo-abuse", "winterfell", no_graph)
    assert decision == engagement_state.GateDecision.PROCEED

    # (2) Graph data present but for a DIFFERENT edge -> graph can now deny -> DEFER.
    wrong_edge = engagement_state.EngagementState(
        objective="gpo abuse",
        footholds=[foothold],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="generic-write:gpo:other",
                source="bloodhound:cypher",
                timestamp=NOW,
                ttl_seconds=TTL_SECONDS,
            )
        ],
    )
    decision, reason = engagement_state.gate_decision("gpo-abuse", "winterfell", wrong_edge)
    assert decision == engagement_state.GateDecision.DEFER
    assert "generic-write:gpo:winterfell" in reason

    # (3) Matching graph edge present -> PROCEED.
    with_graph_fact = engagement_state.EngagementState(
        objective="gpo abuse", footholds=[foothold], graph_facts=[_gpo_fact()]
    )
    decision, reason = engagement_state.gate_decision("gpo-abuse", "winterfell", with_graph_fact)
    assert decision == engagement_state.GateDecision.PROCEED
    assert "preconditions met" in reason


def test_gpo_abuse_belief_aware_no_graph_proceeds_extra():
    # explicit regression guard for the wired-run over-block: foothold present, zero graph data.
    state = engagement_state.EngagementState(objective="gpo abuse", footholds=[_north_foothold()])
    decision, _ = engagement_state.gate_decision("gpo-abuse", "starkwallpaper", state)
    assert decision == engagement_state.GateDecision.PROCEED


def test_dcsync_rights_grant_requires_domain_graph_fact_and_essos_foothold():
    graph_fact = engagement_state.GraphFact(
        predicate="write-dacl:domain:essos.local",
        source="bloodhound:cypher",
        timestamp=NOW,
        ttl_seconds=TTL_SECONDS,
    )
    without_essos_foothold = engagement_state.EngagementState(
        objective="essos DA",
        graph_facts=[graph_fact],
    )
    with_essos_foothold = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[_essos_foothold()],
        graph_facts=[graph_fact],
    )

    decision, reason = engagement_state.gate_decision(
        "dcsync-rights-grant",
        "essos.local",
        without_essos_foothold,
    )
    assert decision == engagement_state.GateDecision.DEFER
    assert "live-foothold:essos.local" in reason

    decision, reason = engagement_state.gate_decision(
        "dcsync-rights-grant",
        "essos.local",
        with_essos_foothold,
    )
    assert decision == engagement_state.GateDecision.PROCEED
    assert "preconditions met" in reason


def test_prune_stale_graph_facts_flips_gpo_abuse_from_proceed_to_defer():
    stale_fact = _gpo_fact(timestamp="2026-06-06T11:54:59Z")
    fresh_fact = engagement_state.GraphFact(
        predicate="generic-write:gpo:castleblack",
        source="bloodhound:cypher",
        timestamp="2026-06-06T11:55:00Z",
        ttl_seconds=TTL_SECONDS,
    )
    state = engagement_state.EngagementState(
        objective="gpo abuse",
        footholds=[_north_foothold()],
        graph_facts=[stale_fact, fresh_fact],
    )

    decision, _reason = engagement_state.gate_decision("gpo-abuse", "winterfell", state)
    assert decision == engagement_state.GateDecision.PROCEED

    pruned = graph_reconciler.prune_stale_graph_facts(state, NOW)

    assert pruned.graph_facts == [fresh_fact]
    decision, reason = engagement_state.gate_decision("gpo-abuse", "winterfell", pruned)
    assert decision == engagement_state.GateDecision.DEFER
    assert "generic-write:gpo:winterfell" in reason


def test_project_graph_predicates_maps_domain_and_computer_edges_with_provenance():
    records = [
        {
            "principal": "arya.stark",
            "type": "WriteDacl",
            "target_kind": "domain",
            "domain": "ESSOS.LOCAL",
        },
        {
            "principal": "arya.stark",
            "type": "GenericAll",
            "target_kind": "computer",
            "computer": "MEEREEN.ESSOS.LOCAL",
        },
    ]

    facts = graph_reconciler.project_graph_predicates(records, NOW, TTL_SECONDS)

    assert [fact.predicate for fact in facts] == [
        "write-dacl:domain:essos.local",
        "generic-write:computer:meereen",
    ]
    assert all(fact.source == "bloodhound:cypher" for fact in facts)
    assert all(fact.timestamp == NOW for fact in facts)
    assert all(fact.ttl_seconds == TTL_SECONDS for fact in facts)


def test_project_graph_predicates_skips_unknown_edges_and_malformed_records():
    records = [
        {"principal": "arya.stark", "type": "MemberOf", "target_kind": "domain", "domain": "essos.local"},
        {"principal": "arya.stark", "type": "GenericWrite", "target_kind": "unknown", "domain": "essos.local"},
        {"principal": "arya.stark", "type": "GenericWrite", "target_kind": "gpo"},
        {"principal": "arya.stark", "type": "GenericWrite", "target_kind": "computer"},
        {"principal": "arya.stark", "type": "GenericWrite", "target_kind": "domain"},
        "not-a-dict",
    ]

    assert graph_reconciler.project_graph_predicates(records, NOW, TTL_SECONDS) == []


def test_empty_graph_facts_preserves_prior_satisfied_predicates_behavior():
    foothold = access_reconciler.project_access(
        [
            {
                "id": 30,
                "agent": "Apollo",
                "host": "WINTERFELL",
                "domain": "NORTH",
                "user": "NORTH\\arya",
                "integrity_level": 3,
            }
        ],
        NOW,
        {"30": True},
    )[0]
    implicit_empty = engagement_state.EngagementState(objective="x", footholds=[foothold])
    explicit_empty = engagement_state.EngagementState(objective="x", footholds=[foothold], graph_facts=[])

    assert implicit_empty.satisfied_predicates() == explicit_empty.satisfied_predicates()


def test_record_hop_result_preserves_graph_facts():
    state = engagement_state.EngagementState(
        objective="gpo abuse",
        footholds=[_north_foothold()],
        graph_facts=[_gpo_fact()],
    )

    updated = engagement_state.record_hop_result(
        state,
        "gpo-abuse",
        "winterfell",
        "pending",
        {"source": "test"},
        NOW,
    )

    assert updated.graph_facts == state.graph_facts


def test_project_graph_position_returns_compact_deduped_view():
    records = [
        {
            "principal": "SAMWELL.TARLY",
            "type": "GenericWrite",
            "target_kind": "computer",
            "computer": "WINTERFELL.NORTH.SEVENKINGDOMS.LOCAL",
            "extra": {"ignored": True},
        },
        {"principal": "", "type": "GenericWrite", "target_kind": "domain", "domain": "essos.local"},
        object(),
    ]

    view = graph_reconciler.project_graph_position(records, ["SAMWELL.TARLY", "samwell.tarly", ""], NOW)

    assert view == {
        "current_principals": ["samwell.tarly"],
        "candidate_edges": [
            {
                "principal": "samwell.tarly",
                "type": "GenericWrite",
                "target_kind": "computer",
                "target": "winterfell",
            }
        ],
    }


def test_prune_stale_graph_facts_drops_unparseable_timestamps():
    state = engagement_state.EngagementState(
        objective="gpo abuse",
        footholds=[_north_foothold()],
        graph_facts=[_gpo_fact(timestamp="not-a-date")],
    )

    pruned = graph_reconciler.prune_stale_graph_facts(state, NOW)

    assert pruned.graph_facts == []


def _literals_response(*values):
    """A BloodHound MCP cypher_query 'run' response with scalar RETURNs under data.literals."""
    return json.dumps({
        "info_type": "run", "success": True,
        "data": {"nodes": {}, "edges": [], "literals": [{"value": v, "key": "name"} for v in values]},
    })


class _FakeTool:
    """Query-aware fake: returns the GPO STARKWALLPAPER for the :GPO query, empty for :Computer/:Domain
    (mirrors the real per-target-kind reconcile calls + the data.literals response shape)."""

    def __init__(self):
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        query = args.get("query", "")
        if "(t:GPO)" in query:
            return _literals_response("STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL")
        return _literals_response()


class _FakeMCPManager:
    def __init__(self, tool=None, raises=False):
        self.tool = tool
        self.raises = raises

    async def get_tool_by_name(self, tool_name, server_name=None):
        del server_name
        if self.raises:
            raise RuntimeError("bloodhound unavailable")
        assert tool_name == "cypher_query"
        return self.tool


def test_reconcile_graph_position_keys_gpo_by_name_via_literals():
    tool = _FakeTool()

    facts = asyncio.run(
        graph_reconciler.reconcile_graph_position(
            _FakeMCPManager(tool),
            ["samwell.tarly@north.sevenkingdoms.local"],
            "gpo abuse",
            NOW,
            TTL_SECONDS,
        )
    )

    # GPO control is keyed by NAME (matches SharpGPOAbuse --gponame), plus the GPO->domain link fact
    # (parsed from the name's @suffix) that lets the planner chain gpo-abuse -> dcsync on that domain.
    preds = [fact.predicate for fact in facts]
    assert "generic-write:gpo:starkwallpaper" in preds
    assert "gpo-domain:starkwallpaper:north.sevenkingdoms.local" in preds
    # Real MCP call shape: info_type=run, principals inlined into the query (no parameters support).
    assert all(call.get("info_type") == "run" for call in tool.calls)
    assert any("samwell.tarly@north.sevenkingdoms.local" in call.get("query", "") for call in tool.calls)
    assert all("parameters" not in call for call in tool.calls)


def test_reconcile_graph_position_returns_empty_on_mcp_errors():
    facts = asyncio.run(
        graph_reconciler.reconcile_graph_position(
            _FakeMCPManager(raises=True),
            ["samwell.tarly@north.sevenkingdoms.local"],
            "gpo abuse",
            NOW,
            TTL_SECONDS,
        )
    )

    assert facts == []
