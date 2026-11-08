"""Phase 4 remaining sources, checkpoint 2: directory ACL reads and enrolled certificates.

A granted right is proven by a later read of the exact object naming the exact principal, and an enrolled
certificate by code that opens its PFX and binds it. The capability writers (executor and registered supervised
command) reach these verifiers; the probes here run through them with only Mythic transport stubbed.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import importlib
from datetime import datetime, timezone
import json

import pytest

from ai.langgraph import capabilities, effect_observations as eo, mythic_tools
from tests.certificate_fixtures import enrollment_output, issue_pfx
from tests.esl_fixtures import seed_ledger
from tests.test_engagement_wiring import _make_tools, _proof_hop, _split_issue

HEADER = (
    "[?] Using DC : winterfell.north.sevenkingdoms.local\n"
    "[?] Object   : DC=north\n"
    "    Path     : LDAP://DC=north,DC=sevenkingdoms,DC=local\n\n"
    "[+] Object properties\n    |_ Owner : BUILTIN\\Administrators\n    |_ Group : BUILTIN\\Administrators\n\n"
)
DOMAIN_DN = "DC=north,DC=sevenkingdoms,DC=local"
PRINCIPAL = "NORTH\\samwell.tarly"


def rule(trustee, kind="Allow", permission="ExtendedRight", obj=""):
    text = f"[+] Identity --> {trustee}\n    |_ Type       : {kind}\n    |_ Permission : {permission}\n"
    return text + (f"    |_ Object     : {obj}\n" if obj else "")


def verify(output, **kwargs):
    params = {"object_dn": DOMAIN_DN, "principal": PRINCIPAL, "rights": eo.REPLICATION_RIGHTS, **kwargs}
    return eo.verify_acl_rights(eo.parse_acl_read(output), **params)


BOTH = rule(PRINCIPAL, obj="DS-Replication-Get-Changes") + rule(PRINCIPAL, obj="DS-Replication-Get-Changes-All")


def test_standin_read_with_both_rights_for_the_exact_principal_holds():
    verdict = verify(HEADER + BOTH)
    assert verdict.achieved, verdict.reason
    assert verdict.values["object_dn"] == DOMAIN_DN.casefold()


def test_standin_guid_named_rights_grant():
    output = HEADER + rule(PRINCIPAL, obj="1131f6aa-9c07-11d1-f79f-00c04fc2dcd2") + rule(
        PRINCIPAL, obj="1131f6ad-9c07-11d1-f79f-00c04fc2dcd2")
    assert verify(output).achieved


@pytest.mark.parametrize("output", [
    # StandIn prints no inheritance flags: a broad rule may be an inherit-only delegation for descendant objects.
    HEADER + rule(PRINCIPAL, permission="GenericAll"),
    HEADER + rule(PRINCIPAL),
    HEADER + rule(PRINCIPAL, obj="ANY"),
])
def test_standin_broad_rules_do_not_prove_named_rights(output):
    verdict = verify(output)
    assert not verdict.achieved
    assert "no allow ACE for the principal grants" in verdict.reason


def test_standin_broad_deny_still_defeats_the_right():
    assert "deny ACE" in verify(HEADER + BOTH + rule(PRINCIPAL, kind="Deny", permission="GenericAll")).reason


def test_standin_read_naming_two_objects_is_not_proof():
    second = HEADER.replace("DC=north,DC=sevenkingdoms", "DC=essos")
    assert "more than one object" in verify(HEADER + BOTH + second).reason


def test_object_filter_form_of_the_dn_normalizes():
    assert eo.normalize_dn("distinguishedname=DC=north, DC=sevenkingdoms,DC=local") == DOMAIN_DN.casefold()
    assert verify(HEADER + BOTH, object_dn="distinguishedname=" + DOMAIN_DN).achieved


@pytest.mark.parametrize("output, kwargs, reason", [
    # near-match principal: same name, other domain; bare name; other user
    (HEADER + BOTH.replace("NORTH\\", "SEVENKINGDOMS\\"), {}, "no allow ACE"),
    (HEADER + BOTH.replace("NORTH\\", ""), {}, "no allow ACE"),
    (HEADER + BOTH.replace("samwell.tarly", "samwell.tarly2"), {}, "no allow ACE"),
    # near-match object/domain
    (HEADER.replace("DC=north,", "DC=south,"), {}, "different object"),
    (HEADER + BOTH, {"object_dn": "DC=sevenkingdoms,DC=local"}, "different object"),
    # only one right; the filtered-set right is not Get-Changes-All
    (HEADER + rule(PRINCIPAL, obj="DS-Replication-Get-Changes"), {}, "grants " + eo.DS_REPLICATION_GET_CHANGES_ALL),
    (HEADER + rule(PRINCIPAL, obj="DS-Replication-Get-Changes")
     + rule(PRINCIPAL, obj="DS-Replication-Get-Changes-In-Filtered-Set"), {}, "grants"),
    # deny for the principal or for everyone; a deny for another principal does not matter (asserted below)
    (HEADER + BOTH + rule(PRINCIPAL, kind="Deny", obj="DS-Replication-Get-Changes-All"), {}, "deny ACE"),
    (HEADER + BOTH + rule("NT AUTHORITY\\Authenticated Users", kind="Deny", permission="GenericAll"), {}, "deny ACE"),
    (HEADER + BOTH + rule("S-1-1-0", kind="Deny"), {}, "deny ACE"),
    # rights that are not control access
    (HEADER + rule(PRINCIPAL, permission="ReadProperty, WriteProperty", obj="DS-Replication-Get-Changes")
     + rule(PRINCIPAL, permission="ReadProperty, WriteProperty", obj="DS-Replication-Get-Changes-All"), {}, "grants"),
    # no bound principal at all
    (HEADER + BOTH, {"principal": ""}, "no bound principal"),
    # failed or non-read output
    ("", {}, "no read output"),
    (HEADER + "[!] Failed to resolve object\n", {}, "reported a failure"),
    (BOTH, {}, "does not name its object"),
])
def test_acl_negative_near_match_and_partial(output, kwargs, reason):
    verdict = verify(output, **kwargs)
    assert not verdict.achieved
    assert reason in verdict.reason


def test_deny_for_another_principal_does_not_defeat_the_right():
    assert verify(HEADER + BOTH + rule("NORTH\\jon.snow", kind="Deny", permission="GenericAll")).achieved


def _json(aces, *, complete=True, dn=DOMAIN_DN, sid="S-1-5-21-9", guid="0f1e2d3c-0000-1111-2222-333344445555"):
    return json.dumps({"object": {"dn": dn, "sid": sid, "guid": guid}, "dacl": {"complete": complete, "aces": aces}})


def _ace(right, *, sid="S-1-5-21-9-1104", kind="allow", rights=0x100, **extra):
    return {"trustee_sid": sid, "type": kind, "rights": rights, "object_type": right, **extra}


JSON_BOTH = [_ace(eo.DS_REPLICATION_GET_CHANGES), _ace(eo.DS_REPLICATION_GET_CHANGES_ALL)]


def test_json_read_binds_principal_and_object_by_sid_and_guid():
    output = _json(JSON_BOTH)
    assert verify(output, principal="", principal_sid="S-1-5-21-9-1104", object_sid="S-1-5-21-9").achieved
    assert not verify(output, principal="", principal_sid="S-1-5-21-9-1105").achieved
    assert "SID differs" in verify(output, principal="", principal_sid="S-1-5-21-9-1104", object_sid="S-1-5-21-8").reason
    assert "GUID differs" in verify(output, principal="", principal_sid="S-1-5-21-9-1104",
                                    object_guid="0f1e2d3c-0000-1111-2222-333344445556").reason


@pytest.mark.parametrize("aces, complete, reason", [
    (JSON_BOTH, False, "complete DACL"),
    ([_ace(eo.DS_REPLICATION_GET_CHANGES), _ace(eo.DS_REPLICATION_GET_CHANGES_ALL, inherit_only=True)], True, "grants"),
    ([_ace(eo.DS_REPLICATION_GET_CHANGES),
      _ace(eo.DS_REPLICATION_GET_CHANGES_ALL, inherited_object_type="bf967aba-0de6-11d0-a285-00aa003049e2")], True, "grants"),
    (JSON_BOTH + [_ace("", sid="S-1-5-11", kind="deny", rights=0xF01FF)], True, "deny ACE"),
    # a deny scoped to inheriting classes still applies to the object unless it is inherit-only
    (JSON_BOTH + [_ace(eo.DS_REPLICATION_GET_CHANGES_ALL, kind="deny",
                       inherited_object_type="bf967aba-0de6-11d0-a285-00aa003049e2")], True, "deny ACE"),
    (JSON_BOTH + [_ace(eo.DS_REPLICATION_GET_CHANGES_ALL, kind="deny", inherit_only="false")], True, "not a Boolean"),
    # full control scoped to an object type is not every right
    ([_ace("bf967a86-0de6-11d0-a285-00aa003049e2", rights=0xF01FF)], True, "grants"),
    ([{"trustee_sid": "S-1-5-21-9-1104", "rights": 0x100}], True, "allow/deny type"),
])
def test_json_read_partial_inherited_and_deny(aces, complete, reason):
    verdict = verify(_json(aces, complete=complete), principal="", principal_sid="S-1-5-21-9-1104")
    assert not verdict.achieved
    assert reason in verdict.reason


def test_json_unscoped_full_control_grants_and_inherit_only_deny_does_not_apply():
    aces = [_ace("", rights=0xF01FF), _ace(eo.DS_REPLICATION_GET_CHANGES_ALL, kind="deny", inherit_only=True)]
    assert verify(_json(aces), principal="", principal_sid="S-1-5-21-9-1104").achieved


def test_json_effect_boolean_is_not_a_read():
    output = json.dumps({"object": {"dn": DOMAIN_DN}, "ds_replication_rights": True, "verified": True})
    assert "no DACL ACE records" in verify(output).reason


# --- Registered supervised command: the ADCS enrollment card the consultant accepted --------------------------------


def _tools_capabilities(mt):
    """The capabilities module the tools object imported (tests may load it flat or as a package); its per-process
    artifact salt is the one the step's default password uses."""
    return importlib.import_module(type(mt).__module__.rsplit("mythic_tools", 1)[0] + "capabilities")


def _register_enroll_card(mt, params):
    capabilities = _tools_capabilities(mt)
    action = capabilities._adcs_esc_certificate_enroll_action("lab.local", "ca01", "administrator", "13")
    runtime = {"ca_name": r"ca01.lab.local\LAB-CA", "template": "VulnerableUser"}
    mt._deterministic_capability_command_contexts[mythic_tools._capability_command_key("powerpick", params)] = {
        "capability": action.name,
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "runtime_inputs": runtime,
        "expected_probe": "extract_adcs_enrolled_certificate_probe",
        "produces": [],
        "consumes": [],
    }
    return capabilities.capability_step_parameters(action, runtime, "adcs-esc-certificate-enroll")["certificate_password"]


def _issue_card(output_for_password):
    mt = _make_tools()
    seed_ledger(mt, hops=[_proof_hop("adcs-ca-key-export-blocked:ca01@lab.local", 7401, callback_id="13")])
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    params = "Write-Output enroll"
    password = _register_enroll_card(mt, params)
    with _split_issue(output_for_password(password), display_id=7410):
        asyncio.run(mt.issue_task_and_waitfor_task_output("powerpick", params, 13, timeout=5))
    return {h.effect: h for h in mt._ledger_view().hops if h.status == "achieved"}


def test_registered_enrollment_card_records_bound_certificate_material():
    achieved = _issue_card(lambda pw: enrollment_output(issue_pfx(pw, issued_at=datetime.now(timezone.utc))))
    hop = achieved["adcs-enrolled-certificate:administrator@lab.local"]
    assert hop.technique == "capability:adcs-esc-certificate-enroll"
    assert str(hop.evidence["mythic_task_id"]) == "7410"


@pytest.mark.parametrize("make_output", [
    lambda pw: enrollment_output(issue_pfx("not-the-step-password", issued_at=datetime.now(timezone.utc))),
    lambda pw: enrollment_output(issue_pfx(pw, upn="eddard.stark@lab.local", issued_at=datetime.now(timezone.utc))),
    lambda pw: enrollment_output(issue_pfx(pw, issued_at=datetime.now(timezone.utc)), status="FAILED"),
])
def test_registered_enrollment_card_unbound_output_records_nothing(make_output):
    assert "adcs-enrolled-certificate:administrator@lab.local" not in _issue_card(make_output)


# --- Checkpoint 2, part 2: RBCD, group membership and GPO-link verifiers --------------------------------------------

COMPUTER_DN = "CN=CASTELBLACK,OU=Servers,DC=north,DC=sevenkingdoms,DC=local"
TRUSTEE = "S-1-5-21-9-1105"


def _rbcd(aces, *, complete=True, dn=COMPUTER_DN):
    return eo.parse_acl_read(json.dumps({"object": {"dn": dn}, "rbcd": {"complete": complete, "aces": aces}}))


def test_rbcd_positive_negative_near_match_and_partial():
    allow = {"trustee_sid": TRUSTEE, "type": "allow", "rights": 0xF01FF}
    assert eo.verify_rbcd(_rbcd([allow]), computer="castelblack", trustee_sid=TRUSTEE).achieved
    assert eo.verify_rbcd(_rbcd([allow]), computer="CASTELBLACK$", trustee_sid=TRUSTEE).achieved
    cases = [
        (_rbcd([allow]), "winterfell", TRUSTEE, "different object"),
        (_rbcd([allow]), "castelblack", "S-1-5-21-9-1106", "does not allow"),
        (_rbcd([allow], complete=False), "castelblack", TRUSTEE, "complete RBCD"),
        (_rbcd([allow, {**allow, "type": "deny"}]), "castelblack", TRUSTEE, "deny ACE"),
        (_rbcd([]), "castelblack", TRUSTEE, "does not allow"),
        (eo.parse_acl_read("=== CASTELBLACK$ msDS-AllowedToActOnBehalfOfOtherIdentity ===\n  RBCD: not set\n"),
         "castelblack", TRUSTEE, "no Path"),  # the retained custom read (Mythic archive 2026-10-30): unsupported
    ]
    for read, computer, sid, reason in cases:
        verdict = eo.verify_rbcd(read, computer=computer, trustee_sid=sid)
        assert not verdict.achieved and reason in verdict.reason, (computer, sid, verdict.reason)


GROUP_DN = "CN=Domain Admins,CN=Users,DC=north,DC=sevenkingdoms,DC=local"


def _members(sids, *, complete=True, dn=GROUP_DN, sid="S-1-5-21-9-512"):
    return eo.parse_acl_read(json.dumps({"object": {"dn": dn, "sid": sid}, "member": {"complete": complete, "sids": sids}}))


def test_group_membership_positive_negative_near_match_and_partial():
    assert eo.verify_group_member(_members([TRUSTEE]), group_dn=GROUP_DN, principal_sid=TRUSTEE).achieved
    cases = [
        (_members([TRUSTEE], dn=GROUP_DN.replace("DC=north,", "")), {}, "different object"),
        (_members([TRUSTEE]), {"group_sid": "S-1-5-21-8-512"}, "SID differs"),
        (_members([TRUSTEE], complete=False), {}, "complete member list"),
        (_members(["S-1-5-21-9-1106"]), {}, "not a direct member"),
    ]
    for read, extra, reason in cases:
        verdict = eo.verify_group_member(read, group_dn=GROUP_DN, principal_sid=TRUSTEE, **extra)
        assert not verdict.achieved and reason in verdict.reason


OU_DN = "OU=Servers,DC=north,DC=sevenkingdoms,DC=local"
GPO = "31b2f340-016d-11d2-945f-00c04fb984f9"


def _gplink(value, dn=OU_DN):
    return eo.parse_acl_read(json.dumps({"object": {"dn": dn}, "gplink": value}))


def test_gpo_link_enabled_disabled_other_container_and_missing():
    link = f"[LDAP://cn={{{GPO}}},cn=policies,cn=system,DC=north,DC=sevenkingdoms,DC=local;0]"
    assert eo.verify_gpo_link(_gplink(link), container_dn=OU_DN, gpo_guid=GPO).achieved
    assert eo.verify_gpo_link(_gplink(link.replace(";0]", ";2]")), container_dn=OU_DN, gpo_guid=GPO).achieved  # enforced
    cases = [
        (_gplink(link.replace(";0]", ";1]")), OU_DN, GPO, "disabled"),
        (_gplink(link), "OU=Workstations,DC=north,DC=sevenkingdoms,DC=local", GPO, "different object"),
        (_gplink(link), OU_DN, "6ac1786c-016f-11d2-945f-00c04fb984f9", "not linked"),
        (_gplink(""), OU_DN, GPO, "not linked"),
        (eo.parse_acl_read(json.dumps({"object": {"dn": OU_DN}, "member": {"complete": True, "sids": []}})), OU_DN, GPO,
         "no gPLink"),
    ]
    for read, container, guid, reason in cases:
        verdict = eo.verify_gpo_link(read, container_dn=container, gpo_guid=guid)
        assert not verdict.achieved and reason in verdict.reason


# --- Joins: a write is a candidate; only a later read of the exact object proves it ---------------------------------

GRANT_PARAMS = (f'StandIn.exe --object "distinguishedname={DOMAIN_DN}" --grant "{PRINCIPAL}" '
                "--guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2")
GRANT_OK = f"[+] Set object access rules\n    |_ Success, added dcsync privileges to object for {PRINCIPAL}\n"
# Captured StandIn grant failure from the range (Mythic archive 2026-11-04).
GRANT_DENIED = HEADER + "[+] Set object access rules\n[!] Failed to set object access properties..\n    |_ Access is denied.\n"
READ_PARAMS = f'StandIn.exe --object "distinguishedname={DOMAIN_DN}" --access --ntaccount "{PRINCIPAL}"'
NETBIOS = {"NORTH": "north.sevenkingdoms.local"}


def _task(task_id, params, *, status="success"):
    from tests.test_engagement_ledger_phase2_unified_proof import CALLBACK, SUCCESS
    from tests.test_engagement_ledger_phase4_existing_rows import _record_fields

    record = dict(SUCCESS) if status == "success" else {**SUCCESS, "status": "error"}
    return {"display_id": task_id, "id": task_id, "operation_id": 1, "command_name": "execute_assembly",
            "original_params": params, "operator": {"username": "operator"},
            "callback": {"display_id": CALLBACK, "host": "WINTERFELL", "user": PRINCIPAL, "agent_callback_id": "agent-2"},
            **_record_fields(record)}


def _row(observation):
    return {"observation_id": observation.observation_id, "observed_at": observation.observed_at,
            "task_id": observation.task_id, "payload": observation.payload()}


def test_grant_success_opens_a_join_and_its_own_output_proves_nothing():
    from ai.langgraph import task_reconciler

    observations = task_reconciler.directory_observations(_task(20, GRANT_PARAMS), GRANT_OK, "2026-11-07T12:00:00+00:00")
    [join] = observations
    values = join.payload()["values"]
    assert (join.kind, join.join_state) == (eo.DIRECTORY_CHANGE_JOIN, eo.JOIN_AWAITING)
    assert (values["object"], values["principal"], values["property"]) == (
        DOMAIN_DN.casefold(), PRINCIPAL.casefold(), eo.PROPERTY_REPLICATION)
    assert task_reconciler.reconcile_task(_task(20, GRANT_PARAMS), GRANT_OK, "2026-11-07T12:00:00+00:00") is None


def test_a_grant_with_no_success_marker_opens_no_join():
    from ai.langgraph import task_reconciler

    out = task_reconciler.directory_observations(_task(20, GRANT_PARAMS), HEADER + "[*] enumerating...\n",
                                                 "2026-11-07T12:00:00+00:00")
    assert [o for o in out if o.kind == eo.DIRECTORY_CHANGE_JOIN] == []


def test_reconciler_grant_route_never_credits_a_grant_from_its_own_output():
    from ai.langgraph import task_reconciler

    assert task_reconciler._probe_for_technique("dcsync-rights-grant", GRANT_OK) is None
    assert task_reconciler.reconcile_task(_task(20, GRANT_PARAMS), GRANT_OK, "2026-11-07T12:00:00+00:00") is None


def test_reconciler_gpo_route_stays_partial_on_a_modification_without_execution():
    from ai.langgraph import task_reconciler

    modified = "[+] GPO was modified to include a new immediate task. Wait for the GPO to refresh."
    probe = task_reconciler._probe_for_technique("gpo-abuse", modified)
    assert probe["system_command_succeeded"] is False and probe["system_callback_observed"] is False
    assert task_reconciler.reconcile_task(
        _task(30, 'SharpGPOAbuse.exe --AddComputerTask --GPOName "srv02-policy"'), modified,
        "2026-11-07T12:00:00+00:00") is None


@pytest.mark.parametrize("task, output", [
    (_task(20, GRANT_PARAMS), GRANT_DENIED),                         # the range's real denial: no candidate
    (_task(20, GRANT_PARAMS), "[+] Modified the DACL\n[!] Access is denied"),  # a success line beside a failure
    (_task(20, GRANT_PARAMS), HEADER + "[*] enumerating only\n"),   # no success marker at all
    (_task(20, GRANT_PARAMS), ""),                                   # never ran
    (_task(20, GRANT_PARAMS, status="error"), GRANT_OK),             # not terminal success
    (_task(20, "StandIn.exe --rbcd --target castelblack"), "[+] done"),  # RBCD write naming no trustee SID
])
def test_writes_that_cannot_be_candidates_open_no_join(task, output):
    from ai.langgraph import task_reconciler

    observations = task_reconciler.directory_observations(task, output, "2026-11-07T12:00:00+00:00")
    assert [o for o in observations if o.kind == eo.DIRECTORY_CHANGE_JOIN] == []


def _resolve(join_task, read_task, read_output, netbios=NETBIOS):
    from ai.langgraph import task_reconciler

    now = "2026-11-07T12:00:00+00:00"
    [join] = task_reconciler.directory_observations(_task(join_task, GRANT_PARAMS), GRANT_OK, now)
    reads = [o for o in task_reconciler.directory_observations(_task(read_task, READ_PARAMS), read_output, now)
             if o.kind == eo.DIRECTORY_READ]
    return eo.resolve_directory_change(_row(join), [_row(r) for r in reads], netbios)


def test_a_grant_on_an_ou_or_leaf_opens_no_replication_join():
    from ai.langgraph import task_reconciler

    ou = "OU=Staff,DC=north,DC=sevenkingdoms,DC=local"
    params = f'StandIn.exe --object "distinguishedname={ou}" --grant "{PRINCIPAL}" --guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2'
    out = task_reconciler.directory_observations(_task(20, params), GRANT_OK, "2026-11-07T12:00:00+00:00")
    assert [o for o in out if o.kind == eo.DIRECTORY_CHANGE_JOIN] == []


def test_a_later_exact_read_resolves_the_join():
    read, reason = _resolve(20, 21, HEADER + BOTH)
    assert read is not None, reason
    assert read["payload"]["values"]["task_id"] == "21"


@pytest.mark.parametrize("join_task, read_task, output, netbios, reason", [
    (20, 19, HEADER + BOTH, NETBIOS, "no later read"),                          # read before the write
    (20, 21, HEADER + BOTH, {}, "not bound"),                                   # NetBIOS prefix not mapped
    (20, 21, HEADER + BOTH, {"SEVENKINGDOMS": "sevenkingdoms.local"}, "not bound"),
    (20, 21, HEADER + rule(PRINCIPAL, obj="DS-Replication-Get-Changes"), NETBIOS, "grants"),
    (20, 21, HEADER.replace("DC=north,", "DC=south,") + BOTH, NETBIOS, "different object"),
    (20, 21, HEADER + BOTH.replace("samwell.tarly", "jon.snow"), NETBIOS, "no allow ACE"),
])
def test_reads_that_do_not_prove_the_write_leave_the_join_awaiting(join_task, read_task, output, netbios, reason):
    read, why = _resolve(join_task, read_task, output, netbios)
    assert read is None and reason in why, why


def test_gpo_abuse_route_requires_system_execution_and_collect_graph_needs_its_ingest():
    from ai.langgraph import task_reconciler

    gpo = _task(30, 'SharpGPOAbuse.exe --AddComputerTask --TaskName "t" --GPOName "srv02-policy" --Command "cmd.exe"')
    modified = "[+] Domain Controller found\n[+] GPO was modified to include a new immediate task. Wait for the GPO refresh."
    assert task_reconciler.reconcile_task(gpo, modified, "2026-11-07T12:00:00+00:00") is None
    assert task_reconciler._probe_for_technique("gpo-abuse", modified)["system_command_succeeded"] is False
    collect = _task(31, "SharpHound.exe -c All --zipfilename loot.zip")
    assert task_reconciler.reconcile_task(collect, "Compressing data to loot.zip\nSharpHound Enumeration Completed",
                                          "2026-11-07T12:00:00+00:00") is None


def test_lsass_dump_route_needs_extracted_material():
    from ai.langgraph import task_reconciler

    probe = task_reconciler._probe_for_technique("lsass-dump", "Dump file written to C:\\Windows\\Temp\\out.dmp")
    assert eo is not None and probe.get("credentials_dumped") is False


# --- Assembled path: operation reconciliation, store, view ----------------------------------------------------------

from tests.esl_fixtures import stub_mythic_operation  # noqa: E402
from tests.test_engagement_ledger_phase2_unified_proof import SUCCESS, session  # noqa: E402,F401
from tests.test_engagement_ledger_phase4_observation_joins import (  # noqa: E402
    _events, _position, _restarted, _state,
)

EFFECT = "ds-replication-rights:north.sevenkingdoms.local"


@pytest.fixture
def directory_mythic(monkeypatch, mythic_task_records):
    records = stub_mythic_operation(monkeypatch)
    records["tasks"], records["outputs"] = [], {}
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", json.dumps(NETBIOS))

    def add(task_id, params, output):
        records["tasks"].append(_task(task_id, params))
        records["outputs"][task_id] = output
        mythic_task_records[task_id] = SUCCESS

    return add


def _reconcile(tools, *cited):
    return asyncio.run(tools._reconcile_operation(tuple(cited)))


def _directory_joins(tools):
    return tools._ledger_store().observations(tools._require_ledger_binding(), kind=eo.DIRECTORY_CHANGE_JOIN)


def _achieved(tools):
    return {h.effect: h for h in tools._ledger_view().hops if h.status == "achieved"}


def test_a_grant_then_a_later_read_records_the_row_with_the_read_as_its_proving_task(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    _reconcile(tools)

    hop = _achieved(tools)[EFFECT]
    assert (str(hop.evidence["mythic_task_id"]), str(hop.evidence["mutation_task_id"])) == ("21", "20")
    assert hop.evidence["proof_envelope"]["task_id"] in ("21", 21)
    [join] = _directory_joins(tools)
    assert (join["join_state"], join["resolved_by"]) == (eo.JOIN_RESOLVED, "task:21")
    assert EFFECT in _state(tools)
    events = len(_events(tools))
    _reconcile(tools)  # replay converges
    assert len(_events(tools)) == events


def test_without_a_bound_principal_the_write_stays_a_candidate(session, directory_mythic, monkeypatch):
    monkeypatch.delenv("SAGE_ENGAGEMENT_NETBIOS_MAP")
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    lines = _reconcile(tools)

    assert EFFECT not in _achieved(tools)
    assert "1 directory change(s) awaiting a later read" in lines[0]
    assert [j["join_state"] for j in _directory_joins(tools)] == [eo.JOIN_AWAITING]


def test_a_read_in_a_later_batch_and_after_a_restart_resolves_the_join(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    tools = session()
    _reconcile(tools)
    assert EFFECT not in _achieved(tools) and _position(tools).task_cursor == 20

    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    fresh = _restarted(tools)
    _reconcile(fresh)
    assert str(_achieved(fresh)[EFFECT].evidence["mythic_task_id"]) == "21"


def test_a_read_before_the_write_and_an_unrelated_task_prove_nothing(session, directory_mythic):
    directory_mythic(19, READ_PARAMS, HEADER + BOTH)
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, "whoami /all", "USER INFORMATION\nnorth\\samwell.tarly")
    tools = session()
    _reconcile(tools)
    assert EFFECT not in _achieved(tools)


def test_a_grant_and_read_on_an_ou_never_achieve_domain_dcsync(session, directory_mythic):
    ou = "OU=Staff,DC=north,DC=sevenkingdoms,DC=local"
    grant = f'StandIn.exe --object "distinguishedname={ou}" --grant "{PRINCIPAL}" --guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2'
    read = f'StandIn.exe --object "distinguishedname={ou}" --access --ntaccount "{PRINCIPAL}"'
    ou_read = HEADER.replace("LDAP://DC=north,DC=sevenkingdoms,DC=local", f"LDAP://{ou}") + BOTH
    directory_mythic(20, grant, GRANT_OK)
    directory_mythic(21, read, ou_read)
    tools = session()
    _reconcile(tools)

    assert EFFECT not in _achieved(tools)
    assert _directory_joins(tools) == []  # the OU grant never opened a replication join


def test_resolution_rejects_a_seeded_non_head_replication_join(session, directory_mythic):
    # Defense in depth: even a replication join whose object is not the domain head (bypassing the creation guard)
    # does not record domain DCSync when a read of that object is observed.
    from ai.langgraph import engagement_store

    ou = "OU=Staff,DC=north,DC=sevenkingdoms,DC=local"
    read = f'StandIn.exe --object "distinguishedname={ou}" --access --ntaccount "{PRINCIPAL}"'
    directory_mythic(21, read, HEADER.replace("LDAP://DC=north,DC=sevenkingdoms,DC=local", f"LDAP://{ou}") + BOTH)
    tools = session()
    join = eo.directory_change_join(_task(20, GRANT_PARAMS), prop=eo.PROPERTY_REPLICATION, technique="dcsync-rights-grant",
                                    target="north.sevenkingdoms.local", obj=ou, principal=PRINCIPAL,
                                    observed_at="2026-11-07T11:00:00+00:00")
    tools._ledger_store().apply(tools._require_ledger_binding(), [],
                                observations=[engagement_store.ObservationRecord(**join.record())])
    _reconcile(tools)

    assert EFFECT not in _achieved(tools)


def test_citations_report_the_candidate_then_the_proving_read_without_moving_the_cursor(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    [line] = _reconcile(tools, "20")
    assert line.startswith("- 20: not added, no proven gain yet; the write is a candidate")
    [line] = _reconcile(tools, "21")
    assert line.startswith("- 21: added row #")
    assert _position(tools).task_cursor == 0
    assert EFFECT in _achieved(tools)
