import asyncio
import json
import re
import sys
from pathlib import Path

import pytest


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


class _PathProbeTool:
    """Fake cypher tool for probe_path_to_objective: resolves aliases to themselves, holds the nodes in
    `nodes`, and answers the shortestPath existence query per `found`. `errors=True` returns an error
    envelope (no `data`). Like BloodHound, a pattern whose endpoint is absent returns no rows."""

    def __init__(self, found: bool, errors: bool = False,
                 nodes: tuple[str, ...] = ("samwell.tarly@north.sevenkingdoms.local", "essos.local")):
        self.found = found
        self.errors = errors
        self.nodes = {n.casefold() for n in nodes}
        self.calls = []

    @staticmethod
    def _rows(*values):
        return json.dumps({"info_type": "run", "success": True,
                           "data": {"literals": [{"value": v, "key": "name"} for v in values]}})

    async def ainvoke(self, args):
        query = args.get("query", "")
        self.calls.append(query)
        if "u:User" in query:  # _resolve_principal_aliases probe
            return json.dumps({"data": {"literals": []}})
        if self.errors:
            return json.dumps({"errors": ["query failed"]})
        named = set(re.findall(r"'([^']+)'", query))
        present = sorted(n for n in named if n in self.nodes)
        if "shortestPath" not in query:  # endpoint existence checks
            return self._rows(*present)
        if len(present) < 2:  # a missing endpoint yields no rows, exactly like "no path"
            return self._rows()
        return self._rows("essos.local") if self.found else self._rows()


def test_probe_path_to_objective_emits_found_when_a_path_exists():
    facts = asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(_PathProbeTool(found=True)),
        ["samwell.tarly@north.sevenkingdoms.local"],
        ["essos.local"],
        NOW,
        TTL_SECONDS,
        proof_envelope=_bh_proof(),
    ))
    assert [f.predicate for f in facts] == ["objective-path-found:essos.local"]
    assert facts[0].proof_envelope == _bh_proof()


def test_probe_path_to_objective_emits_no_path_when_graph_has_data_but_no_path():
    tool = _PathProbeTool(found=False)
    facts = asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(tool), ["samwell.tarly@north.sevenkingdoms.local"], ["essos.local"], NOW, TTL_SECONDS,
    ))
    assert [f.predicate for f in facts] == ["objective-no-path:essos.local"]
    # the shortestPath query is bounded and existence-only (LIMIT 1), never an unbounded enumeration
    path_query = next(q for q in tool.calls if "shortestPath" in q)
    assert "shortestPath" in path_query and "LIMIT 1" in path_query and "[*1..12]" in path_query


def test_probe_path_to_objective_stays_silent_when_the_probe_cannot_run():
    # An error envelope (no `data` object) must NOT be reported as "no path" — fail-open to nothing.
    facts = asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(_PathProbeTool(found=False, errors=True)),
        ["samwell.tarly@north.sevenkingdoms.local"], ["essos.local"], NOW, TTL_SECONDS,
    ))
    assert facts == []


def test_probe_path_to_objective_requires_both_a_principal_and_a_domain():
    tool = _PathProbeTool(found=True)
    assert asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(tool), [], ["essos.local"], NOW, TTL_SECONDS)) == []
    assert asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(tool), ["samwell.tarly@north.sevenkingdoms.local"], [], NOW, TTL_SECONDS)) == []


@pytest.mark.parametrize("nodes", [
    ("essos.local",),                                   # the controlled principal is not in the graph
    ("samwell.tarly@north.sevenkingdoms.local",),       # the objective domain is not in the graph
])
def test_probe_path_to_objective_says_nothing_when_an_endpoint_is_missing_from_the_graph(nodes):
    # BloodHound returns no rows both for "no path" and for a pattern whose endpoint does not exist, so a
    # missing principal or domain must not be reported as "no path".
    tool = _PathProbeTool(found=False, nodes=nodes)
    facts = asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(tool), ["samwell.tarly@north.sevenkingdoms.local"], ["essos.local"], NOW, TTL_SECONDS,
    ))
    assert facts == []


def test_probe_path_to_objective_probes_only_principals_present_in_the_graph():
    tool = _PathProbeTool(found=True)
    facts = asyncio.run(graph_reconciler.probe_path_to_objective(
        _FakeMCP(tool),
        ["samwell.tarly@north.sevenkingdoms.local", "samwell.tarly@sevenkingdoms.local"],
        ["essos.local"], NOW, TTL_SECONDS,
    ))
    assert [f.predicate for f in facts] == ["objective-path-found:essos.local"]
    path_query = next(q for q in tool.calls if "shortestPath" in q)
    assert "samwell.tarly@north.sevenkingdoms.local" in path_query
    assert "samwell.tarly@sevenkingdoms.local" not in path_query


def _reported_foothold(callback_id, reported, identity, *, forest=None, alive=True):
    return engagement_state.Foothold(
        callback_id=callback_id, agent="apollo", host="WS01", forest=forest or reported, identity=identity,
        integrity="medium", alive=alive, source="test", timestamp=NOW, reported_domain=reported,
    )


def test_reported_domain_accounts_skip_shared_machine_dead_and_fqdn_reports():
    state = engagement_state.EngagementState(objective="x", footholds=[
        _reported_foothold("1", "corphq", "alice"),
        _reported_foothold("2", "corphq", "CORPHQ\\Bob"),
        _reported_foothold("3", "corphq", "Administrator"),
        _reported_foothold("4", "corphq", "WS01$"),
        _reported_foothold("5", "corphq", "carol", alive=False),
        _reported_foothold("6", "corp.example.com", "dave"),
        _reported_foothold("7", "nt authority", "system"),
    ])
    assert graph_reconciler.reported_domain_accounts_from_state(state) == [
        ("corphq", "alice", "ws01"), ("corphq", "bob", "ws01"),
    ]


class _NetbiosTool:
    """BloodHound with alice and WS01 in corp.example.com (NetBIOS CORPHQ, not its first label); alice controls a GPO."""

    def __init__(self, alice_domains=("CORP.EXAMPLE.COM",), host_domains=("CORP.EXAMPLE.COM",)):
        self.alice_domains = alice_domains
        self.host_domains = host_domains
        self.queries = []

    async def ainvoke(self, args):
        query = args.get("query", "")
        self.queries.append(query)
        if "u.samaccountname" in query and "'alice'" in query:
            return _literals_response(*[domain.lower() for domain in self.alice_domains])
        if "c.samaccountname" in query and "'ws01$'" in query:
            return _literals_response(*[domain.lower() for domain in self.host_domains])
        if "(t:GPO)" in query and "'alice@corp.example.com'" in query:
            return _literals_response("HQPOLICY@CORP.EXAMPLE.COM|CN={1},CN=POLICIES|\\\\X|OBJ-1")
        return _literals_response()


def test_bloodhound_teaches_a_netbios_name_from_the_callback_account_and_matches_under_it():
    state = engagement_state.EngagementState(objective="x", footholds=[_reported_foothold("1", "corphq", "alice")])
    principals = graph_reconciler.controlled_principals_from_state(state)
    assert principals == ["alice@corphq"]
    tool = _NetbiosTool()

    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCPManager(tool), principals, "x", NOW, 120,
        netbios_accounts=graph_reconciler.reported_domain_accounts_from_state(state),
    ))

    predicates = [fact.predicate for fact in facts]
    assert "domain-netbios:corphq:corp.example.com" in predicates
    assert "generic-write:gpo:hqpolicy" in predicates  # the edge query matched alice under the learned FQDN
    assert engagement_state.learned_netbios_map(
        engagement_state.EngagementState(objective="x", footholds=[], graph_facts=facts)
    ) == {"corphq": "corp.example.com"}


@pytest.mark.parametrize("alice_domains, host_domains", [
    (("CORP.EXAMPLE.COM", "LAB.EXAMPLE.COM"), ("CORP.EXAMPLE.COM",)),  # the account name is in two domains
    (("CORP.EXAMPLE.COM",), ()),  # the callback's host is not in the graph
    (("CORP.EXAMPLE.COM",), ("LAB.EXAMPLE.COM",)),  # logged on to a computer in another domain
])
def test_bloodhound_learns_nothing_without_one_domain_for_account_and_host(alice_domains, host_domains):
    state = engagement_state.EngagementState(objective="x", footholds=[_reported_foothold("1", "corphq", "alice")])
    tool = _NetbiosTool(alice_domains=alice_domains, host_domains=host_domains)

    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCPManager(tool), graph_reconciler.controlled_principals_from_state(state), "x", NOW, 120,
        netbios_accounts=graph_reconciler.reported_domain_accounts_from_state(state),
    ))

    assert not [fact for fact in facts if fact.predicate.startswith("domain-netbios:")]


class _NorthOnlyBH:
    """BloodHound holding only north.sevenkingdoms.local: north has its own sql_svc and samwell.tarly, CASTELBLACK, and
    a GPO north\\sql_svc controls; essos.local (BRAAVOS, essos\\sql_svc) is not collected yet."""

    async def ainvoke(self, args):
        query = args.get("query", "")
        if "u.samaccountname" in query and ("'sql_svc'" in query or "'samwell.tarly'" in query):
            return _literals_response("north.sevenkingdoms.local")
        if "c.samaccountname" in query and "'castelblack$'" in query:
            return _literals_response("north.sevenkingdoms.local")
        if "(t:GPO)" in query and "'sql_svc@north.sevenkingdoms.local'" in query:
            return _literals_response("NORTHPOLICY@NORTH.SEVENKINGDOMS.LOCAL|CN={1},CN=POLICIES|\\\\X|OBJ-1")
        return _literals_response()


def _north_only_refresh(footholds):
    state = engagement_state.EngagementState(objective="x", footholds=footholds)
    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _FakeMCPManager(_NorthOnlyBH()), graph_reconciler.controlled_principals_from_state(state), "x", NOW, 120,
        netbios_accounts=graph_reconciler.reported_domain_accounts_from_state(state),
    ))
    learned = engagement_state.learned_netbios_map(
        engagement_state.EngagementState(objective="x", footholds=[], graph_facts=facts))
    return [fact.predicate for fact in facts], learned


def _host_foothold(callback_id, host, reported, identity):
    return engagement_state.Foothold(
        callback_id=callback_id, agent="apollo", host=host, forest=reported, identity=identity, integrity="medium",
        alive=True, source="test", timestamp=NOW, reported_domain=reported,
    )


def test_a_same_named_account_in_another_collected_domain_teaches_nothing():
    """essos\\sql_svc on BRAAVOS while only north (which has its own sql_svc) is collected."""
    predicates, learned = _north_only_refresh([_host_foothold("20", "BRAAVOS", "essos", "sql_svc")])
    assert learned == {}
    assert not [p for p in predicates if p.startswith("domain-netbios:")]
    assert "generic-write:gpo:northpolicy" not in predicates  # north\\sql_svc's edge is not ours


def test_a_same_named_account_does_not_unlearn_or_misattribute_beside_a_corroborated_callback():
    predicates, learned = _north_only_refresh([
        _host_foothold("10", "CASTELBLACK", "north", "samwell.tarly"),
        _host_foothold("20", "BRAAVOS", "essos", "sql_svc"),
    ])
    assert learned == {"north": "north.sevenkingdoms.local"}
    assert "generic-write:gpo:northpolicy" not in predicates


def test_bloodhound_drops_an_fqdn_claimed_by_two_netbios_names():
    class _Everything:
        async def ainvoke(self, args):
            return _literals_response("corp.example.com") if "samaccountname" in args.get("query", "") else (
                _literals_response())

    learned = asyncio.run(graph_reconciler._learn_netbios_domains(
        _Everything(), [("corphq", "alice", "ws01"), ("corp", "bob", "ws02")]))
    assert learned == {}
