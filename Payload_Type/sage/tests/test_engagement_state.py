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


def test_gate_skips_gpo_abuse_when_effect_already_achieved_with_evidence():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="essos DA"),
        "gpo-abuse",
        "WINTERFELL",
        "achieved",
        {"task_id": "2285", "source": "mythic"},
        "2026-06-06T12:34:00Z",
    )

    decision, reason = engagement_state.gate_decision("gpo-abuse", "WINTERFELL", state)

    assert decision == engagement_state.GateDecision.SKIP
    assert "effect already achieved" in reason
    assert "2285" in reason


def test_gate_defers_dcsync_rights_grant_without_essos_access():
    state = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[_foothold(host="WINTERFELL", forest="north.local")],
    )

    decision, reason = engagement_state.gate_decision("dcsync-rights-grant", "essos.local", state)

    assert decision == engagement_state.GateDecision.DEFER
    assert "missing precondition" in reason
    # belief-aware: write-dacl is graph-derived; with no graph data reconciled it is UNKNOWN, not
    # false, so it must NOT block. The DEFER stands on the foothold-observable precondition only.
    assert "live-foothold:essos.local" in reason
    assert "write-dacl:domain:essos.local" not in reason


def test_gate_proceeds_when_preconditions_are_met():
    state = _state_with_effect(
        "write-dacl:domain:essos.local",
        technique="acl-discovery",
        target="essos.local",
    )
    state.footholds.append(_foothold(host="MEEREEN", forest="essos.local", integrity="high"))

    decision, reason = engagement_state.gate_decision("dcsync-rights-grant", "essos.local", state)

    assert decision == engagement_state.GateDecision.PROCEED
    assert "preconditions met" in reason


def test_gate_fail_open_for_unknown_technique():
    decision, reason = engagement_state.gate_decision(
        "totally-unknown-technique",
        "X",
        engagement_state.EngagementState(objective="essos DA"),
    )

    assert decision == engagement_state.GateDecision.PROCEED
    assert "fail-open" in reason


def test_gate_fail_soft_on_malformed_state():
    decision, reason = engagement_state.gate_decision("gpo-abuse", "WINTERFELL", object())

    assert decision == engagement_state.GateDecision.PROCEED
    assert "fail-open" in reason


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

    decision, reason = engagement_state.gate_decision("gpo-abuse", "WINTERFELL", updated)

    assert decision == engagement_state.GateDecision.SKIP
    assert "2285" in reason


def test_technique_model_chains_dcsync_to_golden_ticket():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="essos DA"),
        "dcsync",
        "essos.local",
        "achieved",
        {"task_id": "2400", "source": "mythic"},
        "2026-06-06T14:00:00Z",
    )

    decision, reason = engagement_state.gate_decision("golden-ticket", "essos.local", state)

    assert len(engagement_state.TECHNIQUE_MODEL) >= 6
    assert decision == engagement_state.GateDecision.PROCEED
    assert "preconditions met" in reason


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
    decision, reason = engagement_state.gate_decision("dcsync", "north.sevenkingdoms.local", state)
    assert decision == engagement_state.GateDecision.PROCEED, reason


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
    assert not state.satisfies_predicate("ds-replication-rights:north.sevenkingdoms.local")


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

    assert engagement_state.engagement_phase(state).startswith("COMPLETE-CANDIDATE")
    assert "Phase: COMPLETE-CANDIDATE" in rendered
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
    assert "OBJECTIVE SATISFIED CANDIDATES" in rendered
    assert "STOP and report this proof" in rendered
    assert "administrative-control:essos.local" in rendered
    assert "admin=da:essos.local task=655" in rendered
    assert "access=kerberos-context:essos.local@callback:14 task=670" in rendered
    assert "auth=certificate-auth:administrator@essos.local task=655" in rendered
    assert rendered.index("OBJECTIVE SATISFIED CANDIDATES") < rendered.index("Live footholds:")
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
    assert "OBJECTIVE SATISFIED CANDIDATES" in rendered
    assert "STOP and report this proof" in rendered
    assert "NEXT CAPABILITY ACTIONS" not in rendered


def test_domain_admin_without_live_context_does_not_imply_replication_rights():
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
    assert not state.satisfies_predicate("ds-replication-rights:north.sevenkingdoms.local")
    decision, reason = engagement_state.gate_decision("dcsync", "north.sevenkingdoms.local", state)
    assert decision == engagement_state.GateDecision.DEFER
    assert "ds-replication-rights:north.sevenkingdoms.local" in reason


# --- COMPLETE-CANDIDATE: don't halt on an intermediate domain; climb to the objective's target ---

def test_objective_target_domains_parses_goal_not_intermediate():
    es = engagement_state
    assert es._objective_target_domains("reach and VERIFY Domain Admin on the essos.local forest") == {"essos.local"}
    t = es._objective_target_domains(
        "reach Domain Admin on essos.local; first DCSync north.sevenkingdoms.local krbtgt then climb")
    assert "essos.local" in t and "north.sevenkingdoms.local" not in t  # intermediate is not a goal-target


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
