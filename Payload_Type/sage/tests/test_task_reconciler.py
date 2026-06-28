import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402
import task_reconciler  # noqa: E402


DOM = "sevenkingdoms.local"
NTLM = "2b576acbe6bcfda7294d6bd18041b8fe"
AES256 = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
TS = "2026-06-11T22:00:00+00:00"


def _task(user="krbtgt"):
    return {
        "display_id": 450,
        "command_name": "dcsync",
        "original_params": json.dumps({"Domain": DOM, "User": user, "DC": "kingslanding.sevenkingdoms.local"}),
        "status": "completed",
        "completed": True,
        "operator": {"username": "mythic_admin"},
        "callback": {"display_id": 13, "host": "CASTELBLACK", "user": "samwell.tarly"},
    }


def _assembly_wrapped_dcsync_task(user="krbtgt", command_name="invoke-assembly"):
    return {
        "display_id": 451,
        "command_name": command_name,
        "original_params": json.dumps({
            "assembly": "DirectoryTool.exe",
            "arguments": (
                f"--Command dcsync --User SEVENKINGDOMS\\{user} --Domain {DOM} "
                "--DomainController kingslanding.sevenkingdoms.local"
            ),
        }),
        "status": "completed",
        "completed": True,
        "operator": {"username": "mythic_admin"},
        "callback": {"display_id": 13, "host": "CASTELBLACK", "user": "samwell.tarly"},
    }


def _dcsync_output():
    return f"""
Object RDN           : krbtgt
** SAM ACCOUNT **
Credentials:
  Hash NTLM: {NTLM}
* Primary:Kerberos-Newer-Keys *
    Credentials
      aes256_hmac       (4096) : {AES256}
"""


def _domain_admin_task():
    return {
        "display_id": 74,
        "command_name": "run",
        "original_params": 'net group "Domain Admins" /domain',
        "status": "completed",
        "completed": True,
        "operator": {"username": "mythic_admin"},
        "callback": {
            "display_id": 3,
            "host": "CASTELBLACK",
            "user": r"NORTH\samwell.tarly",
            "forest": "north.sevenkingdoms.local",
        },
    }


def _domain_admin_output(member="samwell.tarly"):
    return f"""
Group name     Domain Admins
Members
-------------------------------------------------------------------------------
Administrator            eddard.stark             {member}
The command completed successfully.
"""


def test_reconcile_manual_krbtgt_dcsync_records_verified_effect():
    record = task_reconciler.reconcile_task(_task(), _dcsync_output(), TS)

    assert record is not None
    assert record.technique == "dcsync"
    assert record.target == DOM
    assert record.status == "achieved"
    assert record.evidence["source"] == "task_history_reconcile"
    assert record.evidence["provenance"] == "operator_task"
    assert record.evidence["mythic_task_id"] == 450
    assert record.evidence["callback_id"] == 13
    assert record.evidence["verified_on_record"] is True
    assert NTLM not in record.evidence["result_preview"]
    assert AES256 not in record.evidence["result_preview"]
    material = {(item["secret_type"], item["credential_type"], item["credential"]) for item in record.credential_material}
    assert ("aes256", "key", AES256.lower()) in material
    assert ("ntlm", "hash", NTLM.lower()) in material

    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        record.technique,
        record.target,
        record.status,
        record.evidence,
        TS,
    )
    assert state.achieved_effects() == {f"krbtgt-hash:{DOM}"}


def test_reconcile_manual_domain_admin_membership_records_callback_principal_da():
    record = task_reconciler.reconcile_task(_domain_admin_task(), _domain_admin_output(), TS)

    assert record is not None
    assert record.technique == "domain-admin-membership-check"
    assert record.target == "north.sevenkingdoms.local"
    assert record.status == "achieved"
    assert record.evidence["source"] == "task_history_reconcile"
    assert record.evidence["mythic_task_id"] == 74
    assert record.evidence["callback_id"] == 3
    assert record.evidence["verified_on_record"] is True

    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        record.technique,
        record.target,
        record.status,
        record.evidence,
        TS,
    )
    assert state.achieved_effects() == {"da:north.sevenkingdoms.local"}


def test_reconcile_domain_admin_membership_requires_callback_principal():
    assert task_reconciler.reconcile_task(
        _domain_admin_task(),
        _domain_admin_output(member="arya.stark"),
        TS,
    ) is None


def test_reconcile_dcsync_without_secret_imports_nothing():
    output = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439) DS_DRA_BAD_DN"

    assert task_reconciler.reconcile_task(_task(), output, TS) is None


def test_reconcile_manual_user_dcsync_keeps_user_target_distinct():
    record = task_reconciler.reconcile_task(_task("cersei.lannister"), _dcsync_output(), TS)

    assert record is not None
    assert record.technique == "dcsync-user"
    assert record.target == f"cersei.lannister@{DOM}"
    assert {item["account"] for item in record.credential_material} == {"cersei.lannister"}
    assert {item["realm"] for item in record.credential_material} == {DOM}


def test_reconcile_assembly_wrapped_dcsync_records_verified_effect():
    record = task_reconciler.reconcile_task(_assembly_wrapped_dcsync_task(), _dcsync_output(), TS)

    assert record is not None
    assert record.technique == "dcsync"
    assert record.target == DOM
    assert record.evidence["mythic_task_id"] == 451


def test_reconcile_assembly_wrapped_dcsync_requires_secret_material_and_effect_command():
    error_output = "ERROR GetNCChanges failed before any secret material was returned"

    assert task_reconciler.reconcile_task(_assembly_wrapped_dcsync_task(), error_output, TS) is None
    assert task_reconciler.reconcile_task(
        _assembly_wrapped_dcsync_task(command_name="load-assembly"),
        _dcsync_output(),
        TS,
    ) is None


def test_reconcile_extracts_wrapped_aes_material_without_ntlm_line():
    output = f"""
Object RDN           : krbtgt
** SAM ACCOUNT **
Credentials:
* Primary:Kerberos-Newer-Keys *
    Credentials
      aes256_hmac       (4096) : {AES256[:14]}
        {AES256[14:]}
"""

    record = task_reconciler.reconcile_task(_task(), output, TS)

    assert record is not None
    assert any(
        item["secret_type"] == "aes256" and item["credential"] == AES256.lower()
        for item in record.credential_material
    )


def test_reconcile_execute_pe_wrapper_classifies_from_observed_commandline():
    task = {
        "display_id": 450,
        "command_name": "execute_pe",
        "original_params": json.dumps({"pe_name": "mimikatz.exe", "arguments": ""}),
        "status": "success",
        "completed": True,
        "operator": {"username": "mythic_admin"},
        "callback": {"display_id": 13, "host": "CASTELBLACK", "user": "samwell.tarly"},
    }
    output = (
        'mimikatz(commandline) # lsadump::dcsync /domain:sevenkingdoms.local '
        '/dc:kingslanding.sevenkingdoms.local /user:CN=krbtgt,CN=Users,DC=sevenkingdoms,DC=local\n'
        + _dcsync_output()
    )

    record = task_reconciler.reconcile_task(task, output, TS)

    assert record is not None
    assert record.technique == "dcsync"
    assert record.target == DOM
