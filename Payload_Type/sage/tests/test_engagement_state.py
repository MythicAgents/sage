import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402


def _foothold(
    host="WINTERFELL",
    forest="north.local",
    integrity="medium",
    alive=True,
    callback_id="cb50",
    agent="apollo",
    identity="NORTH\\arya",
):
    return engagement_state.Foothold(
        callback_id=callback_id,
        agent=agent,
        host=host,
        forest=forest,
        identity=identity,
        integrity=integrity,
        alive=alive,
        source="mythic",
        timestamp="2026-06-06T12:00:00Z",
    )


def _state_with_effect(effect, technique="seed", target="seed", evidence=None):
    return engagement_state.EngagementState(
        objective="essos DA",
        hops=[
            engagement_state.Hop(
                id=f"{technique}:{target}",
                technique=technique,
                target=target,
                effect=effect,
                status="achieved",
                evidence=evidence or {"task_id": "seed"},
                preconditions=[],
                satisfied_effects=[effect],
                source="test",
                timestamp="2026-06-06T12:00:00Z",
            )
        ],
    )


def _hop(technique, target, effect, status="achieved"):
    return engagement_state.Hop(
        id=f"{technique}:{target}",
        technique=technique,
        target=target,
        effect=effect,
        status=status,
        evidence={"provenance": "run"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-09T12:00:00Z",
    )


def test_verify_effect_uses_structured_gpo_probe_only():
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {"scheduled_task_present": True}) == "partial"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {"scheduled_task_present": False}) == "failed"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {}) == "failed"


def test_render_suppresses_pending_gpo_abuse_after_capability_system_exec_proof():
    state = engagement_state.EngagementState(
        objective="obtain administrative control of essos.local",
        hops=[
            engagement_state.Hop(
                id="gpo-abuse:starkwallpaper",
                technique="gpo-abuse",
                target="starkwallpaper",
                effect="system:starkwallpaper",
                status="pending",
                evidence={"mythic_task_id": 17},
                preconditions=[],
                satisfied_effects=["system:starkwallpaper"],
                source="test",
                timestamp="2026-06-16T23:00:00Z",
            ),
            engagement_state.Hop(
                id="capability:gpo-controlled-system-exec:gpo=starkwallpaper",
                technique="capability:gpo-controlled-system-exec",
                target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
                effect="system-exec:gpo:starkwallpaper@north.sevenkingdoms.local",
                status="achieved",
                evidence={"mythic_task_id": 19, "source": "execute_capability"},
                preconditions=[],
                satisfied_effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
                source="test",
                timestamp="2026-06-16T23:06:00Z",
            ),
            engagement_state.Hop(
                id="domain-admin-membership-check:north.sevenkingdoms.local",
                technique="domain-admin-membership-check",
                target="north.sevenkingdoms.local",
                effect="da:north.sevenkingdoms.local",
                status="achieved",
                evidence={"mythic_task_id": 19, "source": "execute_capability"},
                preconditions=[],
                satisfied_effects=["da:north.sevenkingdoms.local"],
                source="test",
                timestamp="2026-06-16T23:06:00Z",
            ),
        ],
    )

    rendered = engagement_state.render_engagement_state(state)

    assert "Pending hops:" not in rendered
    assert "pending: gpo-abuse" not in rendered
    assert "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local" in rendered
    assert "da:north.sevenkingdoms.local" in rendered


def test_verify_effect_ignores_error_strings_for_verdicts():
    achieved_probe = {
        "scheduled_task_present": True,
        "error": "System.UnauthorizedAccessException: Access to ScheduledTasks.xml is denied",
    }
    same_probe_different_error = {
        "scheduled_task_present": True,
        "error": "completely different tool text",
    }
    error_only = {
        "error": "System.UnauthorizedAccessException: Access to ScheduledTasks.xml is denied",
    }

    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", achieved_probe) == "partial"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", same_probe_different_error) == "partial"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", error_only) == "failed"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", "UnauthorizedAccessException") == "failed"


def test_verify_effect_returns_partial_for_partial_structured_probe():
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {"gpo_modified": True}) == "partial"


def test_record_hop_result_round_trips_with_provenance_and_updates():
    now = "2026-06-06T13:00:00Z"
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="essos DA"),
        "gpo-abuse",
        "WINTERFELL",
        "pending",
        {"task_id": "2284", "source": "mythic"},
        "2026-06-06T12:59:00Z",
    )
    updated = engagement_state.record_hop_result(
        state,
        "gpo-abuse",
        "WINTERFELL",
        "achieved",
        {"task_id": "2285", "source": "mythic"},
        now,
    )

    assert len(updated.hops) == 1
    hop = updated.hops[0]
    assert hop.status == "achieved"
    assert hop.source == "mythic"
    assert hop.timestamp == now
    assert hop.preconditions == ["generic-write:gpo:winterfell", "live-foothold:*"]
    assert hop.satisfied_effects == ["system:winterfell"]
    assert "system:winterfell" in updated.achieved_effects()


def test_explicit_kerberos_context_proof_unlocks_dcsync():
    foothold = engagement_state.Foothold(
        callback_id="9",
        agent="generic-agent",
        host="castelblack",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="high",
        alive=True,
        source="test",
        timestamp="2026-06-11T01:10:00Z",
    )
    state = engagement_state.record_effect_result(
        engagement_state.EngagementState(objective="trust path", footholds=[foothold]),
        "domain-admin-membership-check",
        "north.sevenkingdoms.local",
        "da:north.sevenkingdoms.local",
        "achieved",
        {"source": "issue_task", "provenance": "run", "verified_on_record": True},
        "2026-06-11T01:10:05Z",
        preconditions=["live-foothold:*"],
        satisfied_effects=[
            "da:north.sevenkingdoms.local",
            "kerberos-context:north.sevenkingdoms.local@callback:9",
        ],
    )

    assert "da:north.sevenkingdoms.local" in state.achieved_effects()
    assert "live-callback:9" in engagement_state.foothold_predicates(state)
    assert "kerberos-context:north.sevenkingdoms.local@callback:9" in state.achieved_effects()
    assert state.satisfies_predicate("ds-replication-rights:north.sevenkingdoms.local")


def test_sage_control_callback_is_not_rendered_as_target_foothold():
    state = engagement_state.EngagementState(
        objective="trust path",
        footholds=[
            _foothold(
                host="SAGE",
                forest="sage",
                callback_id="1",
                agent="sage",
                identity="Sage",
            ),
            _foothold(
                host="CASTELBLACK",
                forest="north.sevenkingdoms.local",
                callback_id="2",
                agent="apollo",
                identity="NORTH\\samwell.tarly",
            ),
        ],
    )

    predicates = engagement_state.foothold_predicates(state)
    rendered = engagement_state.render_engagement_state(state)

    assert "live-callback:1" not in predicates
    assert "live-foothold:sage" not in predicates
    assert "live-callback:2" in predicates
    assert "live-foothold:north.sevenkingdoms.local" in predicates
    assert "SAGE | forest=sage" not in rendered
    assert "CASTELBLACK | forest=north.sevenkingdoms.local" in rendered


def test_live_foothold_projects_current_account_context_predicate():
    state = engagement_state.EngagementState(
        objective="read laps",
        footholds=[
            _foothold(
                forest="essos.local",
                callback_id="2",
                identity="jorah.mormont",
            ),
        ],
    )

    assert (
        "kerberos-account-context:jorah.mormont@essos.local@callback:2"
        in engagement_state.foothold_predicates(state)
    )


def test_cross_domain_live_foothold_does_not_project_same_forest_account_context():
    state = engagement_state.EngagementState(
        objective="read laps",
        footholds=[
            _foothold(
                forest="essos.local",
                callback_id="2",
                identity="NORTH\\jorah.mormont",
            ),
        ],
    )

    assert (
        "kerberos-account-context:jorah.mormont@essos.local@callback:2"
        not in engagement_state.foothold_predicates(state)
    )


def test_render_no_longer_emits_planning_lines():
    state = engagement_state.EngagementState(
        objective="reach essos DA",
        footholds=[_foothold(forest="north.sevenkingdoms.local")],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="generic-write:gpo:winterfell",
                source="bloodhound:cypher",
                timestamp="2026-06-09T12:00:00Z",
                ttl_seconds=600,
            )
        ],
    )
    out = engagement_state.render_engagement_state(state)
    assert "NEXT GROUNDED ACTIONS" not in out
    assert "Phase:" not in out


def test_render_empty_state_unchanged():
    out = engagement_state.render_engagement_state(engagement_state.EngagementState(objective="x"))
    assert "(no observed state yet)" in out
    assert "NEXT GROUNDED ACTIONS" not in out


def test_access_context_key_changes_with_privilege():
    foothold = _foothold(forest="north.sevenkingdoms.local")
    base = engagement_state.EngagementState(objective="x", footholds=[foothold])
    escalated = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[_hop("membership", "north.sevenkingdoms.local", "da:north.sevenkingdoms.local")],
    )
    k0 = engagement_state.access_context_key(base, foothold)
    k1 = engagement_state.access_context_key(escalated, foothold)
    assert k0 and k1 and k0 != k1


def test_access_context_key_ignores_hash_and_unbound_credential_artifacts():
    foothold = _foothold(forest="north.sevenkingdoms.local")
    base = engagement_state.EngagementState(objective="x", footholds=[foothold])
    artifacts_only = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[
            _hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local"),
            _hop("dcsync-user", "arya@north.sevenkingdoms.local", "creds:arya@north.sevenkingdoms.local"),
        ],
    )
    assert engagement_state.access_context_key(base, foothold) == engagement_state.access_context_key(artifacts_only, foothold)


def test_access_context_key_ignores_same_domain_kerberos_proof_after_da():
    foothold = _foothold(forest="north.sevenkingdoms.local", callback_id="2")
    da_only = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[_hop("membership", "north.sevenkingdoms.local", "da:north.sevenkingdoms.local")],
    )
    da_with_context = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[
            _hop("membership", "north.sevenkingdoms.local", "da:north.sevenkingdoms.local"),
            _hop("ensure-kerberos-context", "north.sevenkingdoms.local",
                 "kerberos-context:north.sevenkingdoms.local@callback:2"),
        ],
    )

    assert engagement_state.access_context_key(da_only, foothold) == engagement_state.access_context_key(
        da_with_context,
        foothold,
    )


def test_access_context_key_changes_for_active_cross_domain_da_authority():
    foothold = _foothold(forest="north.sevenkingdoms.local", callback_id="2")
    child_da = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[_hop("membership", "north.sevenkingdoms.local", "da:north.sevenkingdoms.local")],
    )
    parent_da_active = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[
            _hop("membership", "north.sevenkingdoms.local", "da:north.sevenkingdoms.local"),
            _hop("forge-golden-ticket", "sevenkingdoms.local", "da:sevenkingdoms.local"),
            _hop("ensure-kerberos-context", "sevenkingdoms.local",
                 "kerberos-context:sevenkingdoms.local@callback:2"),
        ],
    )

    assert engagement_state.access_context_key(child_da, foothold) != engagement_state.access_context_key(
        parent_da_active,
        foothold,
    )


def test_graph_collection_coverage_requires_matching_foothold_domain():
    foothold = _foothold(forest="north.example.local")
    base = engagement_state.EngagementState(objective="x", footholds=[foothold])
    key = engagement_state.access_context_key(base, foothold)
    hop = engagement_state.Hop(
        id="collect",
        technique="collect-graph",
        target=key,
        effect=f"graph-built:{key}",
        status="achieved",
        evidence={
            "graph_verified": True,
            "covered_domains": ["target.example.local"],
        },
        preconditions=[],
        satisfied_effects=[f"graph-built:{key}"],
        source="test",
        timestamp="2026-06-24T12:00:00Z",
    )
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[hop],
    )

    assert engagement_state.graph_collection_covers_foothold(state, foothold) is False
    assert engagement_state.current_access_collection_missing(state) is True


def test_graph_collection_coverage_accepts_netbios_equivalent_domain():
    foothold = _foothold(forest="north")
    base = engagement_state.EngagementState(objective="x", footholds=[foothold])
    key = engagement_state.access_context_key(base, foothold)
    hop = engagement_state.Hop(
        id="collect",
        technique="collect-graph",
        target=key,
        effect=f"graph-built:{key}",
        status="achieved",
        evidence={
            "graph_verified": True,
            "covered_domains": ["north.example.local"],
        },
        preconditions=[],
        satisfied_effects=[f"graph-built:{key}"],
        source="test",
        timestamp="2026-06-24T12:00:00Z",
    )
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[foothold],
        hops=[hop],
    )

    assert engagement_state.graph_collection_covers_foothold(state, foothold) is True
    assert engagement_state.current_access_collection_missing(state) is False


def test_targeted_collection_scope_is_distinct_from_default_forest_scope():
    foothold = _foothold(forest="north.example.local")
    base = engagement_state.EngagementState(objective="x", footholds=[foothold])
    default_key = engagement_state.collection_target_key(base, foothold)
    targeted_key = engagement_state.collection_target_key(base, foothold, "target.example.local")
    assert default_key
    assert targeted_key == f"{default_key}|scope:target.example.local"
    assert targeted_key != default_key


def test_targeted_collection_scope_coverage_uses_target_domain():
    foothold = _foothold(forest="north.example.local")
    base = engagement_state.EngagementState(objective="x", footholds=[foothold])
    key = engagement_state.collection_target_key(base, foothold, "target.example.local")
    hop = engagement_state.Hop(
        id="collect-target",
        technique="collect-graph",
        target=key,
        effect=f"graph-built:{key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["target.example.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{key}"],
        source="test",
        timestamp="2026-06-24T12:00:00Z",
    )
    state = engagement_state.EngagementState(objective="x", footholds=[foothold], hops=[hop])
    assert engagement_state.graph_collection_covers_scope(state, foothold, "target.example.local") is True
    assert engagement_state.graph_collection_covers_foothold(state, foothold) is False


def test_trusted_uncollected_domains_prefers_objective_target():
    state = engagement_state.EngagementState(
        objective="obtain administrative control of target.example.local",
        graph_facts=[
            engagement_state.GraphFact("domain-collected:root.example.local", "test", "", 600),
            engagement_state.GraphFact("trust-reachable:root.example.local:other.example.local", "test", "", 600),
            engagement_state.GraphFact("trust-reachable:root.example.local:target.example.local", "test", "", 600),
        ],
    )
    assert engagement_state.trusted_uncollected_domains(state) == [
        "target.example.local",
        "other.example.local",
    ]


def test_trusted_uncollected_domains_accepts_netbios_collected_source():
    state = engagement_state.EngagementState(
        objective="obtain administrative control of target.example.local",
        graph_facts=[
            engagement_state.GraphFact("domain-collected:root", "test", "", 600),
            engagement_state.GraphFact("trust-reachable:root.example.local:target.example.local", "test", "", 600),
        ],
    )
    assert engagement_state.trusted_uncollected_domains(state) == ["target.example.local"]


def test_phase_is_exploitation_when_grounded_hop_exists_without_graph_facts():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_foothold(forest="north.sevenkingdoms.local")],
        hops=[_hop("dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local")],
    )

    assert engagement_state.engagement_phase(state).startswith("EXPLOITATION")


def test_phase_requests_collection_after_access_key_changes_and_no_next_action():
    foothold = _foothold(forest="north.sevenkingdoms.local")
    base = engagement_state.EngagementState(objective="reach essos.local forest", footholds=[foothold])
    old_key = engagement_state.access_context_key(base, foothold)
    state = engagement_state.EngagementState(
        objective="reach essos.local forest",
        footholds=[foothold],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="domain-collected:sevenkingdoms.local",
                source="bloodhound:test",
                timestamp="2026-06-09T12:00:00Z",
                ttl_seconds=600,
            )
        ],
        hops=[
            _hop("collect-graph", old_key, f"graph-built:{old_key}"),
            _hop("forge-golden-ticket", "sevenkingdoms.local", "da:sevenkingdoms.local"),
            _hop(
                "ensure-kerberos-context",
                "sevenkingdoms.local",
                "kerberos-context:sevenkingdoms.local@callback:cb50",
            ),
            _hop("dcsync", "sevenkingdoms.local", "krbtgt-hash:sevenkingdoms.local"),
        ],
    )

    assert engagement_state.current_access_collection_missing(state) is True
    phase = engagement_state.engagement_phase(state)
    assert phase.startswith("RECON")
    assert "current access" in phase
    assert "GRAPH COLLECTION NEEDED" not in engagement_state.render_engagement_state(state)


def test_gpo_domain_downstream_progress_detects_domain_proof():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_foothold(forest="north.sevenkingdoms.local")],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="gpo-domain:wallpaperpolicy:child.example.local",
                source="bloodhound:test",
                timestamp="2026-06-09T12:00:00Z",
                ttl_seconds=600,
            )
        ],
        hops=[_hop("dcsync", "child.example.local", "krbtgt-hash:child.example.local")],
    )

    assert engagement_state._gpo_domain_has_downstream_progress(state, "wallpaperpolicy") is True


def test_gpo_domain_link_expands_system_effect_to_replication_rights():
    state = engagement_state.EngagementState(
        objective="x",
        footholds=[_foothold(forest="north.sevenkingdoms.local")],
        hops=[
            engagement_state.Hop(
                id="gpo-abuse:starkwallpaper",
                technique="gpo-abuse",
                target="starkwallpaper",
                effect="system:starkwallpaper",
                status="achieved",
                evidence={"provenance": "run"},
                preconditions=[],
                satisfied_effects=["system:starkwallpaper"],
                source="t",
                timestamp="2026-06-09T12:00:00Z",
            )
        ],
        graph_facts=[
            engagement_state.GraphFact(
                "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
                "live-probe",
                "2026-06-09T12:00:00Z",
                600,
            )
        ],
    )

    assert "ds-replication-rights:north.sevenkingdoms.local" in state.satisfied_predicates()


def test_domain_admin_membership_check_alone_does_not_imply_kerberos_context():
    foothold = engagement_state.Foothold(
        callback_id="9",
        agent="generic-agent",
        host="castelblack",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="high",
        alive=True,
        source="test",
        timestamp="2026-06-11T01:10:00Z",
    )
    state = engagement_state.record_effect_result(
        engagement_state.EngagementState(objective="trust path", footholds=[foothold]),
        "domain-admin-membership-check",
        "north.sevenkingdoms.local",
        "da:north.sevenkingdoms.local",
        "achieved",
        {"source": "issue_task", "provenance": "run", "verified_on_record": True},
        "2026-06-11T01:10:05Z",
        preconditions=["live-foothold:*"],
        satisfied_effects=["da:north.sevenkingdoms.local"],
    )

    assert "da:north.sevenkingdoms.local" in state.achieved_effects()
    assert "kerberos-context:north.sevenkingdoms.local@callback:9" not in state.achieved_effects()
    # ds-replication-rights IS now implied (intentional 2026-06-20): the foothold is a LIVE IN-DOMAIN callback
    # and DA-group membership grants replication rights (in-domain arm of _expand_implications). It still does
    # NOT fabricate a kerberos-context effect (asserted above) — selection is unblocked; execution proves the
    # live token, and the stall detector recovers if it is not yet DA-privileged.
    assert state.satisfies_predicate("ds-replication-rights:north.sevenkingdoms.local")


def _achieved_hop(effect, task_id, technique="capability:seed", target="seed"):
    return engagement_state.Hop(
        id=f"{technique}:{target}",
        technique=technique,
        target=target,
        effect=effect,
        status="achieved",
        evidence={"mythic_task_id": task_id, "source": "test"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-12T12:00:00Z",
    )


def test_objective_completion_candidate_renders_admin_control_proof_chain():
    foothold = engagement_state.Foothold(
        callback_id="14",
        agent="apollo",
        host="castelblack",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="2026-06-12T12:00:00Z",
    )
    state = engagement_state.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        graph_facts=[
            engagement_state.GraphFact(
                predicate="generic-write:gpo:winterfell",
                source="test",
                timestamp="2026-06-12T12:00:00Z",
                ttl_seconds=600,
            ),
        ],
        hops=[
            _achieved_hop("da:essos.local", "655", target="domain=essos.local"),
            _achieved_hop("certificate-auth:administrator@essos.local", "655", target="domain=essos.local"),
            _achieved_hop("krbtgt-hash:essos.local", "664", technique="dcsync", target="essos.local"),
            _achieved_hop(
                "kerberos-context:essos.local@callback:14",
                "670",
                technique="capability:ensure-kerberos-context",
                target="domain=essos.local;callback=14",
            ),
        ],
    )

    candidates = engagement_state.objective_completion_candidates(state)
    rendered = engagement_state.render_engagement_state(state)

    # The kept planner functions still derive phase + completion candidates (model.py's terminal/
    # blocked-report consumers depend on them) ...
    assert engagement_state.engagement_phase(state).startswith("COMPLETE-CANDIDATE")
    assert candidates == [{
        "kind": "administrative-control",
        "domain": "essos.local",
        "admin_effect": "da:essos.local",
        "admin_task_id": "655",
        "access_effect": "kerberos-context:essos.local@callback:14",
        "access_task_id": "670",
        "callback_id": "14",
        "auth_effect": "certificate-auth:administrator@essos.local",
        "auth_task_id": "655",
        "key_effect": "krbtgt-hash:essos.local",
        "key_task_id": "664",
    }]
    # ... but Stage B: the observed-state render no longer emits any planning/completion output.
    assert "Phase:" not in rendered
    assert "OBJECTIVE SATISFIED CANDIDATES" not in rendered
    assert "NEXT CAPABILITY ACTIONS" not in rendered
    assert "NEXT GROUNDED ACTIONS" not in rendered


def test_objective_completion_candidate_requires_live_callback_context():
    foothold = engagement_state.Foothold(
        callback_id="14",
        agent="apollo",
        host="castelblack",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="2026-06-12T12:00:00Z",
    )
    state = engagement_state.EngagementState(
        objective="domain admin",
        footholds=[foothold],
        hops=[
            _achieved_hop("da:essos.local", "655"),
            _achieved_hop("kerberos-context:essos.local@callback:13", "670"),
        ],
    )

    assert engagement_state.objective_completion_candidates(state) == []
    assert "OBJECTIVE SATISFIED CANDIDATES" not in engagement_state.render_engagement_state(state)


def test_objective_completion_candidate_accepts_live_certificate_auth_proof():
    foothold = engagement_state.Foothold(
        callback_id="3",
        agent="apollo",
        host="castelblack",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="2026-06-12T12:00:00Z",
    )
    auth_hop = _achieved_hop(
        "certificate-auth:administrator@essos.local",
        "150",
        technique="capability:adcs-certificate-auth",
        target="domain=essos.local;account=administrator",
    )
    auth_hop.evidence["callback_id"] = "3"
    state = engagement_state.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[
            _achieved_hop(
                "da:essos.local",
                "150",
                technique="capability:adcs-certificate-auth",
                target="domain=essos.local",
            ),
            auth_hop,
        ],
    )

    candidates = engagement_state.objective_completion_candidates(state)
    rendered = engagement_state.render_engagement_state(state)

    assert engagement_state.engagement_phase(state).startswith("COMPLETE-CANDIDATE")
    assert candidates == [{
        "kind": "administrative-control",
        "domain": "essos.local",
        "admin_effect": "da:essos.local",
        "admin_task_id": "150",
        "access_effect": "certificate-auth:administrator@essos.local",
        "access_task_id": "150",
        "callback_id": "3",
        "auth_effect": "certificate-auth:administrator@essos.local",
        "auth_task_id": "150",
    }]
    # Stage B: completion candidates are still derived by the kept function (above), but the
    # observed-state render no longer emits the OBJECTIVE SATISFIED CANDIDATES planning block.
    assert "OBJECTIVE SATISFIED CANDIDATES" not in rendered
    assert "NEXT CAPABILITY ACTIONS" not in rendered


def test_in_domain_da_implies_replication_rights_without_kerberos_context():
    # Intentional change (2026-06-20): DA from a LIVE IN-DOMAIN foothold implies ds-replication-rights even
    # without a recorded kerberos-context — DA-group membership grants replication, usable from your own
    # in-domain foothold. This unblocks the DA->DCSync selection loop. (Cross-forest keeps the kerberos-context
    # gate — covered by test_cross_forest_da_without_context_or_in_domain_foothold_keeps_gate.) If the live
    # token is not actually DA-privileged yet, dcsync fails at execution and the stall detector recovers.
    foothold = _foothold(host="castelblack", forest="north.sevenkingdoms.local")
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="trust path", footholds=[foothold]),
        "domain-admin-membership-check",
        "north.sevenkingdoms.local",
        "achieved",
        {"source": "issue_task", "provenance": "run", "verified_on_record": True},
        "2026-06-11T01:10:05Z",
    )

    assert "da:north.sevenkingdoms.local" in state.achieved_effects()
    assert state.satisfies_predicate("ds-replication-rights:north.sevenkingdoms.local")


def test_netbios_da_with_fqdn_kerberos_context_implies_fqdn_replication_rights():
    state = engagement_state.EngagementState(
        objective="trust path",
        footholds=[_foothold(host="castelblack", forest="north", callback_id="2")],
        hops=[
            _hop("seed", "north", "da:north"),
            _hop(
                "ensure-kerberos-context",
                "north.example.local",
                "kerberos-context:north.example.local@callback:2",
            ),
        ],
    )

    assert state.satisfies_predicate("ds-replication-rights:north.example.local")


# --- COMPLETE-CANDIDATE: don't halt on an intermediate domain; climb to the objective's target ---

def test_objective_target_domains_parses_goal_not_intermediate():
    es = engagement_state
    assert es._objective_target_domains("reach and VERIFY Domain Admin on the essos.local forest") == {"essos.local"}
    t = es._objective_target_domains(
        "reach Domain Admin on essos.local; first DCSync north.sevenkingdoms.local krbtgt then climb")
    assert "essos.local" in t and "north.sevenkingdoms.local" not in t  # intermediate is not a goal-target


def test_credential_material_objective_completes_only_for_matching_effect():
    objective = "Engagement objective: obtain verified credential material for alice@lab.local."
    state = engagement_state.EngagementState(
        objective=objective,
        hops=[_hop("dcsync-account", "alice", "creds:alice@lab.local")],
    )
    wrong = engagement_state.EngagementState(
        objective=objective,
        hops=[_hop("dcsync-account", "bob", "creds:bob@lab.local")],
    )

    assert engagement_state._objective_credential_targets(objective) == {("alice", "lab.local")}
    assert engagement_state.objective_effects_complete(state) is True
    assert engagement_state._objective_is_complete(state, has_next=True) is True
    assert engagement_state.objective_effects_complete(wrong) is False


def test_objective_is_complete_intermediate_domain_is_milestone(monkeypatch):
    es = engagement_state
    st = es.EngagementState(objective="reach Domain Admin on essos.local")
    # Intermediate domain (north) proven, objective targets essos -> NOT complete, even with no further hop.
    monkeypatch.setattr(es, "objective_completion_candidates", lambda s: [{"domain": "north.sevenkingdoms.local"}])
    assert es._objective_is_complete(st, has_next=False) is False
    assert es._objective_is_complete(st, has_next=True) is False
    # The objective's target domain proven -> complete (terminal), regardless of other available hops.
    monkeypatch.setattr(es, "objective_completion_candidates", lambda s: [{"domain": "essos.local"}])
    assert es._objective_is_complete(st, has_next=True) is True


def test_objective_is_complete_no_target_falls_back_to_no_further_hop(monkeypatch):
    es = engagement_state
    st = es.EngagementState(objective="get domain admin")  # no parseable target domain
    monkeypatch.setattr(es, "objective_completion_candidates", lambda s: [{"domain": "north.sevenkingdoms.local"}])
    assert es._objective_is_complete(st, has_next=True) is False   # further hop -> keep going
    assert es._objective_is_complete(st, has_next=False) is True   # nothing left -> complete


def test_opaque_engagement_id_does_not_complete_from_no_next_hop(monkeypatch):
    es = engagement_state
    st = es.EngagementState(objective="sage-engagement:d499206c-e493-45cf-a422-54ffa33fcece")
    monkeypatch.setattr(es, "objective_completion_candidates", lambda s: [{"domain": "sevenkingdoms.local"}])

    assert es._objective_is_complete(st, has_next=False) is False


# --- rights-trace diagnostic: eid attribution (Fix #1) + test-pollution guard (Fix #2) ---

def test_rights_trace_attributes_frozen_engagement_id(monkeypatch, tmp_path):
    """Fix #1: when the live process publishes its frozen engagement key, the rights-trace record carries it
    in `eid` (NOT the empty harness-side SAGE_ENGAGEMENT_ID env)."""
    import json
    import os
    import engagement_ledger as el

    es = engagement_state
    # Simulate the live (non-pytest) Sage process so the diagnostic actually writes.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("SAGE_ENGAGEMENT_ID", raising=False)
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(el, "_ACTIVE_ENGAGEMENT_ID", "", raising=False)  # auto-restored after test
    el.set_active_engagement_id("gauge-c0-cross-forest-deadbeef")
    es._RIGHTS_TRACE_SEEN.clear()

    es._trace_rights_decision(
        {"da:north.sevenkingdoms.local", "live-foothold:north.sevenkingdoms.local"},
        {"ds-replication-rights:north.sevenkingdoms.local"},
    )

    trace = tmp_path / "rights_trace.jsonl"
    assert trace.exists()
    rec = json.loads(trace.read_text().strip().splitlines()[-1])
    assert rec["eid"] == "gauge-c0-cross-forest-deadbeef"
    assert rec["rights_granted"] is True


def test_rights_trace_skipped_under_pytest(monkeypatch, tmp_path):
    """Fix #2: under pytest the trace must not write — synthetic test predicates were polluting the real
    .sage_engagement/rights_trace.jsonl with empty-eid fixture records."""
    import os

    es = engagement_state
    assert os.environ.get("PYTEST_CURRENT_TEST")  # precondition: pytest sets this for the test's duration
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    es._RIGHTS_TRACE_SEEN.clear()

    es._trace_rights_decision(
        {"da:north.sevenkingdoms.local"},
        {"ds-replication-rights:north.sevenkingdoms.local"},
    )

    assert not (tmp_path / "rights_trace.jsonl").exists()


def test_runtime_trace_writer_honors_isolation_never_repo_root(monkeypatch, tmp_path):
    """REGRESSION for the bypass that caused real pollution: the live write path MUST honor
    SAGE_ENGAGEMENT_STATE_DIR (the conftest isolation) and never fall back to writing under os.getcwd().
    The original relative-only `from . import engagement_ledger` failed in flat-import runtimes and fell
    through to `os.getcwd()/.sage_engagement`, bypassing isolation — which is how 30 records landed in the
    repo working tree."""
    import os
    import engagement_ledger as el

    es = engagement_state
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)  # simulate the live (non-pytest) process
    monkeypatch.delenv("SAGE_ENGAGEMENT_ID", raising=False)
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "isolated"))
    monkeypatch.setattr(el, "_ACTIVE_ENGAGEMENT_ID", "", raising=False)
    el.set_active_engagement_id("Operation_Isolated")
    es._RIGHTS_TRACE_SEEN.clear()

    repo_root_pollution = os.path.join(os.getcwd(), ".sage_engagement", "rights_trace.jsonl")
    existed_before = os.path.exists(repo_root_pollution)

    es._trace_rights_decision(
        {"da:north.sevenkingdoms.local", "live-foothold:north.sevenkingdoms.local"},
        {"ds-replication-rights:north.sevenkingdoms.local"},
    )

    # The write honored the configured isolation dir...
    assert (tmp_path / "isolated" / "rights_trace.jsonl").exists()
    # ...and never created repo-root pollution.
    assert os.path.exists(repo_root_pollution) == existed_before
