import asyncio
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import access_reconciler  # noqa: E402
import engagement_state  # noqa: E402
import graph_reconciler  # noqa: E402
import proof_boundary  # noqa: E402


NOW = "2026-06-06T12:00:00Z"
TTL_SECONDS = 300


def _bh_proof():
    return proof_boundary.make_runtime_bloodhound_envelope(
        engagement_id="op-1",
        callback_id="10",
        task_id="99",
        terminal_status="completed",
        command="execute_assembly",
        ingest_job_id="job-1",
        ingest_status="complete",
        source_artifact_id="file-1",
        source_artifact_sha256="a" * 64,
        verifier_id="bloodhound_ingest:completed",
        transaction_id="fixture:99",
        verifier_input={"ingest_job_id": "job-1", "task_id": "99"},
        verifier_result={"ingest_status": "complete"},
        captured_at=NOW,
    ).to_dict()


def _north_foothold(alive=True):
    return engagement_state.Foothold(
        callback_id="10",
        agent="Apollo",
        host="CASTLEBLACK",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=alive,
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


def test_controlled_principals_projects_foothold_to_upn():
    state = engagement_state.EngagementState(objective="x", footholds=[_north_foothold()])
    assert graph_reconciler.controlled_principals_from_state(state) == [
        "samwell.tarly@north.sevenkingdoms.local"
    ]


def test_controlled_principals_skips_dead_foothold():
    state = engagement_state.EngagementState(objective="x", footholds=[_north_foothold(alive=False)])
    assert graph_reconciler.controlled_principals_from_state(state) == []


def test_controlled_principals_includes_dcsynced_creds():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_north_foothold()],
        hops=[
            engagement_state.Hop(
                id="dcsync-user:cersei.lannister@sevenkingdoms.local",
                technique="dcsync-user",
                target="cersei.lannister@sevenkingdoms.local",
                effect="creds:cersei.lannister@sevenkingdoms.local",
                status="achieved",
                evidence={"provenance": "run"},
                preconditions=[],
                satisfied_effects=["creds:cersei.lannister@sevenkingdoms.local"],
                source="test",
                timestamp=NOW,
            )
        ],
    )
    principals = graph_reconciler.controlled_principals_from_state(state)
    assert "samwell.tarly@north.sevenkingdoms.local" in principals
    assert "cersei.lannister@sevenkingdoms.local" in principals


class _FakeTool:
    """Query-aware fake mirroring real per-target-kind reconcile calls and data.literals shape."""

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


def test_reconcile_graph_position_projects_gpo_domain_facts_from_mcp_shape():
    foothold = _north_foothold()
    principals = graph_reconciler.controlled_principals_from_state(
        engagement_state.EngagementState(objective="x", footholds=[foothold])
    )
    assert principals == ["samwell.tarly@north.sevenkingdoms.local"]

    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCP(_FakeTool()), principals, "reach essos DA", NOW, TTL_SECONDS
    ))
    preds = [f.predicate for f in facts]
    assert "generic-write:gpo:starkwallpaper" in preds
    assert "gpo-domain:starkwallpaper:north.sevenkingdoms.local" in preds


def test_reconcile_graph_position_carries_ingest_proof_envelope():
    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCP(_FakeTool()),
        ["samwell.tarly@north.sevenkingdoms.local"],
        "reach essos DA",
        NOW,
        TTL_SECONDS,
        proof_envelope=_bh_proof(),
    ))

    assert facts
    assert all(fact.proof_envelope == _bh_proof() for fact in facts)


def test_runtime_state_ignores_graph_fact_without_completed_ingest_lineage():
    unbound = engagement_state.GraphFact(
        predicate="ds-replication-rights:lab.local",
        source="bloodhound:cypher",
        timestamp=NOW,
        ttl_seconds=TTL_SECONDS,
    )
    bound = engagement_state.GraphFact(
        predicate="ds-replication-rights:good.local",
        source="bloodhound:cypher",
        timestamp=NOW,
        ttl_seconds=TTL_SECONDS,
        proof_envelope=_bh_proof(),
    )
    state = engagement_state.EngagementState(
        objective="x",
        graph_facts=[unbound, bound],
        engagement_id="op-1",
        runtime_scope=True,
    )

    assert "ds-replication-rights:lab.local" not in state.satisfied_predicates()
    assert "ds-replication-rights:good.local" in state.satisfied_predicates()


def test_reconcile_graph_position_projects_trust_reachability_fact():
    class _TrustTool:
        async def ainvoke(self, args):
            if "MATCH (source:Domain)-[edge]->(target:Domain)" in args.get("query", ""):
                return _literals_response("ROOT.EXAMPLE.LOCAL|TARGET.EXAMPLE.LOCAL")
            return _literals_response()

    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCP(_TrustTool()),
        ["operator@root.example.local"],
        "obtain administrative control of target.example.local",
        NOW,
        TTL_SECONDS,
    ))
    assert "trust-reachable:root.example.local:target.example.local" in [fact.predicate for fact in facts]


def test_resolve_principal_aliases_expands_unambiguous_netbios_domain():
    class _AliasTool:
        async def ainvoke(self, args):
            assert "samwell.tarly@north." in args.get("query", "")
            return _literals_response("SAMWELL.TARLY@NORTH.SEVENKINGDOMS.LOCAL")

    aliases = asyncio.run(
        graph_reconciler._resolve_principal_aliases(
            _AliasTool(),
            ["samwell.tarly@north"],
        )
    )

    assert aliases == ["samwell.tarly@north.sevenkingdoms.local"]


def test_resolve_principal_aliases_fails_closed_on_ambiguous_netbios_domain():
    class _AmbiguousAliasTool:
        async def ainvoke(self, args):
            assert "alice@corp." in args.get("query", "")
            return _literals_response(
                "ALICE@CORP.EXAMPLE.LOCAL",
                "ALICE@CORP.OTHER.LOCAL",
            )

    aliases = asyncio.run(
        graph_reconciler._resolve_principal_aliases(
            _AmbiguousAliasTool(),
            ["alice@corp"],
        )
    )

    assert aliases == ["alice@corp"]


def test_reconcile_graph_position_uses_resolved_netbios_principal():
    class _AliasReconcileTool:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            query = args.get("query", "")
            if "MATCH (u:User)" in query:
                return _literals_response("SAMWELL.TARLY@NORTH.SEVENKINGDOMS.LOCAL")
            if "(t:GPO)" in query:
                return _literals_response("STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL")
            return _literals_response()

    tool = _AliasReconcileTool()
    facts = asyncio.run(
        graph_reconciler.reconcile_graph_position(
            _FakeMCP(tool),
            ["samwell.tarly@north"],
            "reach target",
            NOW,
            TTL_SECONDS,
        )
    )

    assert "generic-write:gpo:starkwallpaper" in [fact.predicate for fact in facts]
    acl_queries = [
        call["query"]
        for call in tool.calls
        if "MATCH (p)-[e]->" in call.get("query", "")
    ]
    assert acl_queries
    assert all("samwell.tarly@north.sevenkingdoms.local" in query for query in acl_queries)


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

    pruned = graph_reconciler.prune_stale_graph_facts(state, NOW)

    assert pruned.graph_facts == [fresh_fact]


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


def test_project_graph_predicates_maps_laps_read_edge_to_managed_secret_fact():
    records = [
        {
            "principal": "alice@LAB.LOCAL",
            "type": "ReadLAPSPassword",
            "target_kind": "computer",
            "computer": "WS01.CHILD.LAB.LOCAL",
        },
    ]

    facts = graph_reconciler.project_graph_predicates(records, NOW, TTL_SECONDS)

    assert [fact.predicate for fact in facts] == [
        "can-read-managed-local-admin-secret:"
        "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
    ]


def test_project_graph_predicates_maps_direct_dcsync_edge_to_replication_fact():
    records = [
        {
            "principal": "alice@LAB.LOCAL",
            "type": "DCSync",
            "target_kind": "domain",
            "domain": "LAB.LOCAL",
        },
    ]

    facts = graph_reconciler.project_graph_predicates(records, NOW, TTL_SECONDS)

    assert [fact.predicate for fact in facts] == ["ds-replication-rights:lab.local"]


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
            return _literals_response(
                "STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL|"
                "CN={0A93E998-2599-4DA8-9717-6744993DED3A},CN=POLICIES,CN=SYSTEM,"
                "DC=NORTH,DC=SEVENKINGDOMS,DC=LOCAL|"
                "\\\\NORTH.SEVENKINGDOMS.LOCAL\\SYSVOL\\NORTH.SEVENKINGDOMS.LOCAL\\POLICIES\\"
                "{0A93E998-2599-4DA8-9717-6744993DED3A}|"
                "89A762BB-09B9-45CF-AB37-E517C964B4A5"
            )
        if "GPLink" in query and "Contains" in query and "(g:GPO)" in query:
            return _literals_response(
                "STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL|"
                "WINTERFELL.NORTH.SEVENKINGDOMS.LOCAL|NORTH.SEVENKINGDOMS.LOCAL|1",
                "STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL|"
                "CASTELBLACK.NORTH.SEVENKINGDOMS.LOCAL|NORTH.SEVENKINGDOMS.LOCAL|0",
            )
        if "ReadLAPSPassword" in query:
            return _literals_response("alice@LAB.LOCAL|WS01.CHILD.LAB.LOCAL|")
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
    assert "gpo-guid:starkwallpaper:0a93e998-2599-4da8-9717-6744993ded3a" in preds
    assert "gpo-affects-computer:starkwallpaper:winterfell:north.sevenkingdoms.local" in preds
    assert "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local" in preds
    assert "gpo-affects-computer:starkwallpaper:castelblack:north.sevenkingdoms.local" in preds
    assert (
        "can-read-managed-local-admin-secret:"
        "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
    ) in preds
    # Real MCP call shape: info_type=run, principals inlined into the query (no parameters support).
    assert all(call.get("info_type") == "run" for call in tool.calls)
    assert any("samwell.tarly@north.sevenkingdoms.local" in call.get("query", "") for call in tool.calls)
    assert all("parameters" not in call for call in tool.calls)


def test_reconcile_graph_position_projects_direct_dcsync_authority():
    class _DirectDCSyncTool:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            query = args.get("query", "")
            if "type(e) IN ['DCSync']" in query:
                return _literals_response("LAB.LOCAL")
            return _literals_response()

    tool = _DirectDCSyncTool()

    facts = asyncio.run(
        graph_reconciler.reconcile_graph_position(
            _FakeMCPManager(tool),
            ["alice@lab.local"],
            "reach lab DA",
            NOW,
            TTL_SECONDS,
        )
    )

    assert "ds-replication-rights:lab.local" in [fact.predicate for fact in facts]
    assert any("type(e) IN ['DCSync']" in call.get("query", "") for call in tool.calls)


def test_credential_target_domains_from_state_uses_live_kerberos_context():
    state = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[
            engagement_state.Foothold(
                callback_id="3",
                agent="Apollo",
                host="CASTELBLACK",
                forest="north.sevenkingdoms.local",
                identity="NORTH\\samwell.tarly",
                integrity="medium",
                alive=True,
                source="test",
                timestamp=NOW,
            )
        ],
        hops=[
            engagement_state.Hop(
                id="da:sevenkingdoms.local",
                technique="forge-golden-ticket",
                target="sevenkingdoms.local",
                effect="da:sevenkingdoms.local",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["da:sevenkingdoms.local"],
                source="test",
                timestamp=NOW,
            ),
            engagement_state.Hop(
                id="ctx:sevenkingdoms.local",
                technique="forge-golden-ticket",
                target="sevenkingdoms.local",
                effect="kerberos-context:sevenkingdoms.local@callback:3",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["kerberos-context:sevenkingdoms.local@callback:3"],
                source="test",
                timestamp=NOW,
            ),
        ],
    )

    assert graph_reconciler.credential_target_domains_from_state(state) == ["sevenkingdoms.local"]


def test_controlled_principals_include_live_da_groups_for_graph_projection():
    state = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[_north_foothold()],
        hops=[
            engagement_state.Hop(
                id="da:sevenkingdoms.local",
                technique="forge-golden-ticket",
                target="sevenkingdoms.local",
                effect="da:sevenkingdoms.local",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=[
                    "da:sevenkingdoms.local",
                    "kerberos-context:sevenkingdoms.local@callback:10",
                ],
                source="test",
                timestamp=NOW,
            )
        ],
    )

    principals = graph_reconciler.controlled_principals_from_state(state)

    assert "domain admins@sevenkingdoms.local" in principals
    assert "administrators@sevenkingdoms.local" in principals


def test_reconcile_graph_position_projects_targets_from_controlled_group_members():
    class _SmallCouncilTool:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            query = args.get("query", "")
            if "(p)-[e]->(g:Group)" in query and "MemberOf*1..4" in query:
                return _literals_response("CERSEI.LANNISTER@SEVENKINGDOMS.LOCAL")
            return _literals_response()

    tool = _SmallCouncilTool()

    facts = asyncio.run(
        graph_reconciler.reconcile_graph_position(
            _FakeMCPManager(tool),
            ["domain admins@sevenkingdoms.local"],
            "essos DA",
            NOW,
            TTL_SECONDS,
            credential_domains=["sevenkingdoms.local"],
        )
    )

    preds = [fact.predicate for fact in facts]
    assert "credential-target:cersei.lannister@sevenkingdoms.local" in preds
    assert any("MATCH (p)-[e]->(g:Group)" in call.get("query", "") for call in tool.calls)


def test_reconcile_graph_position_projects_dcsync_target_from_laps_reader_path():
    class _LapsPathTool:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            query = args.get("query", "")
            if "MemberOf*0..4" in query:
                return _literals_response(
                    "CERSEI.LANNISTER@SEVENKINGDOMS.LOCAL|BRAAVOS.ESSOS.LOCAL|ESSOS.LOCAL"
                )
            return _literals_response()

    tool = _LapsPathTool()

    facts = asyncio.run(
        graph_reconciler.reconcile_graph_position(
            _FakeMCPManager(tool),
            [],
            "essos DA",
            NOW,
            TTL_SECONDS,
            credential_domains=["sevenkingdoms.local"],
        )
    )

    preds = [fact.predicate for fact in facts]
    assert "credential-target:cersei.lannister@sevenkingdoms.local" in preds
    assert (
        "can-read-managed-local-admin-secret:"
        "account=cersei.lannister;account_domain=sevenkingdoms.local;target=braavos;target_domain=essos.local"
    ) in preds
    assert any("ENDS WITH '@sevenkingdoms.local'" in call.get("query", "") for call in tool.calls)


def test_gpo_scalar_parts_does_not_treat_objectid_as_policy_guid():
    name, guid = graph_reconciler._gpo_scalar_parts(
        "STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL|||89A762BB-09B9-45CF-AB37-E517C964B4A5"
    )

    assert name == "STARKWALLPAPER@NORTH.SEVENKINGDOMS.LOCAL"
    assert guid == ""


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
