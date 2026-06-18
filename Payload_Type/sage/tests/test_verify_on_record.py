"""Verify-on-record: a credential hop is recorded `achieved` ONLY when the task output actually
contains a usable secret.

Regression for the 2026-06-08 false-achieved bug: a `dcsync-user` DCSync of cersei/lord.varys that
failed with 8439 DS_DRA_BAD_DN (Mythic task "succeeds", returns no key) was recorded
`creds:X achieved` because the record path gated only on the absence of a known failure signature.
The agent then forged with a placeholder key. The fix feeds a deterministic probe dict (from
`credential_artifacts.extract_credential_probe`) into the existing `engagement_state.verify_effect`
seam, scoped to credential techniques; non-credential techniques are unchanged.

No live Mythic required — MythicTools.__init__ does no network. Mirrors the repo's no-pytest-asyncio
convention.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import credential_artifacts as ca  # noqa: E402
import engagement_state as es  # noqa: E402
import mythic_tools  # noqa: E402

TS = "2026-06-08T00:00:00+00:00"
DOM = "sevenkingdoms.local"
USER = f"cersei.lannister@{DOM}"

NTLM = "2b576acbe6bcfda7294d6bd18041b8fe"            # 32 hex
AES256 = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"  # 64 hex
AES128 = "00112233445566778899aabbccddeeff"          # 32 hex

# Real mimikatz dcsync output fragment (NTLM + kerberos keys).
MIMIKATZ_OK = f"""
Object RDN           : cersei.lannister
** SAM ACCOUNT **
Credentials:
  Hash NTLM: {NTLM}
    ntlm- 0: {NTLM}
* Primary:Kerberos-Newer-Keys *
    Credentials
      aes256_hmac       (4096) : {AES256}
      aes128_hmac       (4096) : {AES128}
"""

# 8439 DS_DRA_BAD_DN: task "succeeds" but no key is returned.
DCSYNC_8439 = (
    "[DC] 'sevenkingdoms.local' will be the domain\n"
    "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439)\n"
    "ERROR kuhl_m_lsadump_dcsync ; DS_DRA_BAD_DN\n"
)
TRUNCATED = "[DC] 'sevenkingdoms.local' will be the domain\n[rpc] Aut"
PLACEHOLDER_ONLY = "forging cersei ticket with /rc4:REPLACE_ME /aes256:PLACEHOLDER"
SECRETSDUMP = f"{DOM}\\cersei.lannister:1107:aad3b435b51404eeaad3b435b51404ee:{NTLM}:::"
# A 32-hex blob with NO credential label (objectGUID-like) must NOT be read as a credential.
BARE_HEX = f"objectGUID value is {NTLM} in the directory listing"


# ---------------------------------------------------------------------------
# Extractor truth table (ISC-1..15)
# ---------------------------------------------------------------------------

def test_credential_techniques_set():
    assert ca.CREDENTIAL_TECHNIQUES == {"dcsync", "dcsync-user", "lsass-dump"}


def test_probe_shape_has_verify_keys():
    p = ca.extract_credential_probe(MIMIKATZ_OK)
    for k in ("credentials_dumped", "krbtgt_hash_present", "user_hash_present",
              "domain_hashes_dumped", "secretsdump_connected"):
        assert k in p


def test_ntlm_label_detected():
    assert ca.extract_credential_probe(f"  Hash NTLM: {NTLM}")["credentials_dumped"] is True


def test_aes256_detected():
    assert ca.extract_credential_probe(f"aes256_hmac (4096) : {AES256}")["credentials_dumped"] is True


def test_aes128_detected():
    assert ca.extract_credential_probe(f"aes128_hmac (4096) : {AES128}")["credentials_dumped"] is True


def test_rc4_detected():
    assert ca.extract_credential_probe(f"rc4_hmac (4096) : {NTLM}")["credentials_dumped"] is True


def test_secretsdump_line_detected():
    assert ca.extract_credential_probe(SECRETSDUMP)["credentials_dumped"] is True


def test_8439_no_key():
    assert ca.extract_credential_probe(DCSYNC_8439)["credentials_dumped"] is False


def test_truncated_no_key():
    assert ca.extract_credential_probe(TRUNCATED)["credentials_dumped"] is False


def test_empty_and_none_no_key():
    assert ca.extract_credential_probe("")["credentials_dumped"] is False
    assert ca.extract_credential_probe(None)["credentials_dumped"] is False


def test_placeholder_only_no_key():
    assert ca.extract_credential_probe(PLACEHOLDER_ONLY)["credentials_dumped"] is False


def test_real_key_plus_placeholder_real_wins():
    mixed = f"  Hash NTLM: {NTLM}\nthen forging with /rc4:REPLACE_ME\n"
    assert ca.extract_credential_probe(mixed)["credentials_dumped"] is True


def test_bare_hex_no_label_is_not_a_credential():
    # Anti (ISC-14): a 32-hex token with no field label / secretsdump shape must not count.
    assert ca.extract_credential_probe(BARE_HEX)["credentials_dumped"] is False


def test_dump_started_partial_signal():
    p = ca.extract_credential_probe(DCSYNC_8439)
    assert p["secretsdump_connected"] is True       # tool ran...
    assert p["credentials_dumped"] is False          # ...but no usable key


# ---------------------------------------------------------------------------
# verify_effect integration (ISC-16..19)
# ---------------------------------------------------------------------------

def test_verify_dcsync_user_with_key_achieved():
    assert es.verify_effect("dcsync-user", USER, ca.extract_credential_probe(MIMIKATZ_OK)) == "achieved"


def test_verify_dcsync_user_no_signal_failed():
    # No key AND no dump-started signal (truncated) -> "failed".
    assert es.verify_effect("dcsync-user", USER, ca.extract_credential_probe(TRUNCATED)) == "failed"


def test_verify_dcsync_user_8439_partial_never_achieved():
    # 8439: the dump tool ran (partial signal) but returned no key -> "partial", NEVER "achieved".
    verdict = es.verify_effect("dcsync-user", USER, ca.extract_credential_probe(DCSYNC_8439))
    assert verdict == "partial"
    assert verdict != "achieved"


def test_verify_dcsync_krbtgt_with_key_achieved():
    assert es.verify_effect("dcsync", DOM, ca.extract_credential_probe(MIMIKATZ_OK)) == "achieved"


def test_verify_lsass_with_key_achieved():
    assert es.verify_effect("lsass-dump", "winterfell", ca.extract_credential_probe(MIMIKATZ_OK)) == "achieved"


# ---------------------------------------------------------------------------
# Record-path wiring (ISC-20..28, 34)
# ---------------------------------------------------------------------------

@pytest.fixture
def mt(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-vor")
    return mythic_tools.MythicTools(agent_task_id="vor")


def _last(mt):
    return mt._engagement_hops[-1]


def test_record_dcsync_user_with_key_achieved(mt):
    mt._pending_engagement_hop = ("dcsync-user", USER, TS)
    mt._record_engagement_success(MIMIKATZ_OK)
    hop = _last(mt)
    assert hop.technique == "dcsync-user" and hop.status == "achieved"
    assert hop.evidence.get("verify_verdict") == "achieved"
    assert hop.evidence.get("verified_on_record") is True
    assert mt._pending_engagement_hop is None          # cleared in finally (ISC-27)


def test_stage_b_dcsync_key_records_verified_credential_without_gate_flag(mt):
    assert not hasattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED")
    mt._pending_engagement_hop = ("dcsync-user", USER, TS)
    mt._record_engagement_success(MIMIKATZ_OK)

    hop = _last(mt)
    assert hop.technique == "dcsync-user"
    assert hop.status == "achieved"
    assert hop.effect == f"creds:{USER}"
    assert hop.evidence.get("verify_verdict") == "achieved"
    assert hop.evidence.get("verified_on_record") is True
    assert hop.evidence.get("artifact_present") is True


def test_record_dcsync_user_no_key_failed_not_achieved(mt):
    mt._pending_engagement_hop = ("dcsync-user", USER, TS)
    mt._record_engagement_success(DCSYNC_8439)
    hop = _last(mt)
    assert hop.status == "failed"                       # the false-achieved lie is dead (ISC-21)
    assert hop.evidence.get("verify_verdict") != "achieved"   # "partial" (ran, no key) or "failed"
    assert hop.evidence.get("artifact_present") is False


def test_failed_credential_hop_persists(mt):
    mt._pending_engagement_hop = ("dcsync-user", USER, TS)
    mt._record_engagement_success(DCSYNC_8439)
    # Durable trail so `state show` surfaces the honest failure (ISC-23).
    assert Path(mt._engagement_ledger_path()).exists()


def test_record_dcsync_krbtgt_no_key_failed(mt):
    mt._pending_engagement_hop = ("dcsync", DOM, TS)
    mt._record_engagement_success(TRUNCATED)
    hop = _last(mt)
    assert hop.status == "failed"


def test_gpo_abuse_system_proof_achieved(mt):
    # GPO abuse records achieved only when the output proves execution, not merely setup.
    mt._pending_engagement_hop = ("gpo-abuse", "winterfell.north.sevenkingdoms.local", TS)
    mt._record_engagement_success("whoami\r\nnt authority\\system\r\n")
    hop = _last(mt)
    assert hop.technique == "gpo-abuse" and hop.status == "achieved"
    assert hop.evidence.get("verify_verdict") == "achieved"
    assert hop.evidence.get("verified_on_record") is True


def test_non_credential_failure_signature_records_nothing(mt):
    # Legacy early-return on a known failure signature for non-credential techniques.
    mt._pending_engagement_hop = ("gpo-abuse", "winterfell.north.sevenkingdoms.local", TS)
    mt._record_engagement_success("Failed to create task")
    assert mt._engagement_hops == []
    assert mt._pending_engagement_hop is None


# ---------------------------------------------------------------------------
# Purity (ISC-26)
# ---------------------------------------------------------------------------

def test_engagement_state_does_not_import_mythic_tools():
    src = (Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "engagement_state.py").read_text()
    assert "import mythic_tools" not in src
    assert "from mythic_tools" not in src


# ---------------------------------------------------------------------------
# lsass-dump shape (Advisor Q3) — sekurlsa::logonpasswords uses `* NTLM : <hex>`
# ---------------------------------------------------------------------------

LSASS_OK = """
Authentication Id : 0 ; 123456 (00000000:0001e240)
User Name         : jon.snow
Domain            : NORTH
        msv :
         [00000003] Primary
         * Username : jon.snow
         * Domain   : NORTH
         * NTLM     : 5a8d7e1f2c3b4a5968778695a4b3c2d1
         * SHA1     : 0123456789abcdef0123456789abcdef01234567
"""


def test_lsass_logonpasswords_shape_detected():
    # The real sekurlsa column shape must extract a key (not just dcsync labels).
    assert ca.extract_credential_probe(LSASS_OK)["credentials_dumped"] is True
    assert es.verify_effect("lsass-dump", "winterfell", ca.extract_credential_probe(LSASS_OK)) == "achieved"


# ---------------------------------------------------------------------------
# Degenerate constants (Advisor Q3) — empty-LM / blank-NT / all-zero are NOT usable secrets
# ---------------------------------------------------------------------------

def test_blank_nt_constant_rejected():
    assert ca.extract_credential_probe("  Hash NTLM: 31d6cfe0d16ae931b73c59d7e0c089c0")["credentials_dumped"] is False


def test_empty_lm_with_real_nt_secretsdump_detected():
    # LM is the empty constant (normal in modern AD); the NT field is real -> usable.
    line = f"{DOM}\\user:1107:aad3b435b51404eeaad3b435b51404ee:{NTLM}:::"
    assert ca.extract_credential_probe(line)["credentials_dumped"] is True


def test_secretsdump_blank_nt_rejected():
    line = f"{DOM}\\guest:501:aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0:::"
    assert ca.extract_credential_probe(line)["credentials_dumped"] is False


def test_all_zero_aes_rejected():
    assert ca.extract_credential_probe(f"aes256_hmac (4096) : {'0' * 64}")["credentials_dumped"] is False


# ---------------------------------------------------------------------------
# Stickiness (Advisor Q1) — a no-key re-probe must NOT downgrade a verified achieved
# ---------------------------------------------------------------------------

def test_verified_achieved_not_downgraded_same_run(mt):
    mt._pending_engagement_hop = ("dcsync-user", USER, TS)
    mt._record_engagement_success(MIMIKATZ_OK)
    assert _last(mt).status == "achieved"
    # Transient 8439 re-probe in the same instance must keep the verified achieved.
    mt._pending_engagement_hop = ("dcsync-user", USER, "2026-06-08T01:00:00+00:00")
    mt._record_engagement_success(DCSYNC_8439)
    hops = [h for h in mt._engagement_hops if h.technique == "dcsync-user" and h.target == USER]
    assert len(hops) == 1 and hops[0].status == "achieved"


def test_verified_achieved_sticky_across_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-vor-reload")
    a = mythic_tools.MythicTools(agent_task_id="run1")
    a._pending_engagement_hop = ("dcsync-user", USER, TS)
    a._record_engagement_success(MIMIKATZ_OK)
    # Fresh instance loads the durable ledger; the no-key re-probe must keep the achieved hop.
    b = mythic_tools.MythicTools(agent_task_id="run2")
    assert any(h.technique == "dcsync-user" and h.status == "achieved" for h in b._engagement_hops)
    b._pending_engagement_hop = ("dcsync-user", USER, "2026-06-08T02:00:00+00:00")
    b._record_engagement_success(DCSYNC_8439)
    hops = [h for h in b._engagement_hops if h.technique == "dcsync-user" and h.target == USER]
    assert len(hops) == 1 and hops[0].status == "achieved"


def test_legacy_false_achieved_overwritten_by_verified_failed(mt):
    # A legacy achieved WITHOUT real-key evidence (artifact_present absent) is cleanup-eligible:
    # a verified no-key result SHOULD overwrite it (this is the false-achieved fix doing its job).
    mt._engagement_hops = es.record_hop_result(
        es.EngagementState(objective="t"), "dcsync-user", USER, "achieved",
        {"source": "legacy", "provenance": "run"}, TS,
    ).hops
    mt._pending_engagement_hop = ("dcsync-user", USER, "2026-06-08T03:00:00+00:00")
    mt._record_engagement_success(DCSYNC_8439)
    hops = [h for h in mt._engagement_hops if h.technique == "dcsync-user" and h.target == USER]
    assert len(hops) == 1 and hops[0].status == "failed"


# ---------------------------------------------------------------------------
# Schema invariant (Advisor Q2) — CREDENTIAL_TECHNIQUES == techniques declaring a credential key
# ---------------------------------------------------------------------------

def test_credential_techniques_matches_verify_schema():
    cred_keys = {"credentials_dumped", "krbtgt_hash_present"}
    derived = set()
    for name, model in es.TECHNIQUE_MODEL.items():
        verify = model.get("verify", {})
        allkeys = set(verify.get("achieved_all", [])) | set(verify.get("achieved_any", []))
        if allkeys & cred_keys:
            derived.add(name)
    assert ca.CREDENTIAL_TECHNIQUES == derived


# ---------------------------------------------------------------------------
# Verify-on-record for dcsync-rights-grant (2026-06-09 false-achieved-grant bug)
# ---------------------------------------------------------------------------

GRANT_DOM = "north.sevenkingdoms.local"

GRANT_OK = (
    "[+] Using DC   : winterfell.north.sevenkingdoms.local\n"
    "[+] Object     : DC=north,DC=sevenkingdoms,DC=local\n"
    "[+] Adding DS-Replication-Get-Changes and DS-Replication-Get-Changes-All\n"
    "[+] DACL modified successfully\n"
)
GRANT_OK_GUID = (
    "[+] Object : DC=north,DC=sevenkingdoms,DC=local\n"
    "[+] Applied ACE 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2\n"
    "[+] Applied ACE 1131f6ad-9c07-11d1-f79f-00c04fc2dcd2\n"
)
GRANT_DENIED = (
    "[!] Object    : DC=north,DC=sevenkingdoms,DC=local\n"
    "[!] Exception : Access is denied (0x80070005)\n"
)
GRANT_EMPTY = ""   # the GPO/SYSTEM scheduled task that vanished from SYSVOL before firing


def test_grant_techniques_set_separate_from_credential():
    assert ca.GRANT_TECHNIQUES == {"dcsync-rights-grant"}
    assert ca.GRANT_TECHNIQUES.isdisjoint(ca.CREDENTIAL_TECHNIQUES)


def test_grant_probe_applied_true_on_success():
    assert ca.extract_grant_probe(GRANT_OK)["ds_replication_rights"] is True
    assert ca.extract_grant_probe(GRANT_OK_GUID)["ds_replication_rights"] is True


def test_grant_probe_denied_is_not_applied():
    p = ca.extract_grant_probe(GRANT_DENIED)
    assert p["ds_replication_rights"] is False
    assert p["ace_present"] is False        # a denied attempt grants no partial credit


def test_grant_probe_empty_is_not_applied():
    assert ca.extract_grant_probe(GRANT_EMPTY)["ds_replication_rights"] is False
    assert ca.extract_grant_probe(None)["ds_replication_rights"] is False


def test_verify_grant_success_achieved():
    assert es.verify_effect("dcsync-rights-grant", GRANT_DOM, ca.extract_grant_probe(GRANT_OK)) == "achieved"


def test_verify_grant_denied_failed():
    assert es.verify_effect("dcsync-rights-grant", GRANT_DOM, ca.extract_grant_probe(GRANT_DENIED)) == "failed"


def test_record_grant_success_achieved(mt):
    mt._pending_engagement_hop = ("dcsync-rights-grant", GRANT_DOM, TS)
    mt._record_engagement_success(GRANT_OK)
    hop = _last(mt)
    assert hop.technique == "dcsync-rights-grant" and hop.status == "achieved"
    assert hop.evidence.get("verify_verdict") == "achieved"
    assert hop.evidence.get("artifact_present") is True


def test_record_grant_denied_failed_not_achieved(mt):
    # The false-achieved-grant lie is dead: Access-denied records FAILED, never achieved.
    mt._pending_engagement_hop = ("dcsync-rights-grant", GRANT_DOM, TS)
    mt._record_engagement_success(GRANT_DENIED)
    hop = _last(mt)
    assert hop.status == "failed"
    assert hop.evidence.get("artifact_present") is False


def test_record_grant_empty_failed(mt):
    mt._pending_engagement_hop = ("dcsync-rights-grant", GRANT_DOM, TS)
    mt._record_engagement_success(GRANT_EMPTY)
    assert _last(mt).status == "failed"


def test_verified_achieved_grant_not_downgraded(mt):
    mt._pending_engagement_hop = ("dcsync-rights-grant", GRANT_DOM, TS)
    mt._record_engagement_success(GRANT_OK)
    assert _last(mt).status == "achieved"
    # A later denied/empty re-probe in the same instance must keep the verified achieved grant.
    mt._pending_engagement_hop = ("dcsync-rights-grant", GRANT_DOM, "2026-06-08T01:00:00+00:00")
    mt._record_engagement_success(GRANT_DENIED)
    hops = [h for h in mt._engagement_hops if h.technique == "dcsync-rights-grant" and h.target == GRANT_DOM]
    assert len(hops) == 1 and hops[0].status == "achieved"


# ---------------------------------------------------------------------------
# Verify-on-record for golden-ticket / SID-history (2026-06-11 false-achieved ticket bug)
# ---------------------------------------------------------------------------

CHILD_DOM = "north.sevenkingdoms.local"
ROOT_DOM = "sevenkingdoms.local"

RUBEUS_SDDL_EXCEPTION = """
Created C:\\Windows\\System32\\WerFault.exe process with an ID of 7696
[*] Action: Build TGT
[*] Building PAC
[!] Unhandled Rubeus exception:
System.ArgumentException: Value was invalid.
Parameter name: sddlForm
   at System.Security.Principal.SecurityIdentifier..ctor(String sddlForm)
"""

MIMIKATZ_GOLDEN_FORGE_ONLY = """
mimikatz(commandline) # kerberos::golden /user:Administrator /domain:north.sevenkingdoms.local
 * PAC generated
 * PAC signed
 * EncTicketPart generated
 * EncTicketPart encrypted
Golden ticket for 'Administrator @ north.sevenkingdoms.local' successfully submitted for current session
"""

MIMIKATZ_SPLIT_ERROR = """
mimikatz(commandline) # /user:Administrator
ERROR mimikatz_doLocal ; "/user:Administrator" command of "standard" module not found !
"""

TICKET_GROUP_PROOF = """
GROUP INFORMATION
-----------------
SEVENKINGDOMS\\Enterprise Admins Group used for deny only
SEVENKINGDOMS\\Domain Admins       Enabled group
The command completed successfully.
"""


def test_ticket_techniques_set():
    assert ca.TICKET_TECHNIQUES == {"golden-ticket", "sid-history-escalation"}


def test_ticket_probe_rubeus_exception_failed_not_partial():
    probe = ca.extract_ticket_probe(RUBEUS_SDDL_EXCEPTION)
    assert probe["ticket_error"] is True
    assert probe["ticket_forged"] is False
    assert probe["domain_admin"] is False
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) == "failed"


def test_ticket_probe_forge_only_partial_never_achieved():
    probe = ca.extract_ticket_probe(MIMIKATZ_GOLDEN_FORGE_ONLY)
    assert probe["ticket_forged"] is True
    assert probe["ticket_valid"] is False
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) == "partial"


def test_ticket_probe_mimikatz_split_error_failed():
    probe = ca.extract_ticket_probe(MIMIKATZ_SPLIT_ERROR)
    assert probe["ticket_error"] is True
    assert es.verify_effect("golden-ticket", CHILD_DOM, probe) == "failed"


def test_ticket_probe_group_proof_achieved():
    probe = ca.extract_ticket_probe(TICKET_GROUP_PROOF)
    assert probe["domain_admin"] is True
    assert "Domain Admins" in probe["member_of"]
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) == "achieved"


def test_record_sid_history_rubeus_exception_failed_not_achieved(mt):
    mt._pending_engagement_hop = ("sid-history-escalation", CHILD_DOM, TS)
    mt._record_engagement_success(RUBEUS_SDDL_EXCEPTION)
    hop = _last(mt)
    assert hop.technique == "sid-history-escalation"
    assert hop.status == "failed"
    assert hop.effect == f"da:{ROOT_DOM}"
    assert hop.evidence["verified_on_record"] is True
    assert hop.evidence["verify_verdict"] == "failed"
    assert hop.evidence["artifact_present"] is False


def test_record_sid_history_forge_only_failed_with_partial_verdict(mt):
    mt._pending_engagement_hop = ("sid-history-escalation", CHILD_DOM, TS)
    mt._record_engagement_success(MIMIKATZ_GOLDEN_FORGE_ONLY)
    hop = _last(mt)
    assert hop.status == "failed"
    assert hop.evidence["verify_verdict"] == "partial"
    assert hop.evidence["ticket_forged"] is True
    assert hop.evidence["artifact_present"] is False


def test_record_sid_history_group_proof_achieved(mt):
    mt._last_issued_callback_id = 13
    mt._pending_engagement_hop = ("sid-history-escalation", CHILD_DOM, TS)
    mt._record_engagement_success(TICKET_GROUP_PROOF)
    hop = _last(mt)
    assert hop.status == "achieved"
    assert hop.effect == f"da:{ROOT_DOM}"
    assert f"kerberos-context:{ROOT_DOM}@callback:13" in hop.satisfied_effects
    assert hop.evidence["verify_verdict"] == "achieved"
    assert hop.evidence["artifact_present"] is True


# --- Negative tests (2026-06-12 audit): the forgeable-proof holes, now closed. Each of these would have
#     recorded ACHIEVED (false-achieved) under the pre-fix substring proof; they must now NOT be achieved. ---

CHILD_DA_LINE = """
GROUP INFORMATION
-----------------
NORTH\\Domain Admins       Enabled group
The command completed successfully.
"""

DENY_ONLY_EA = """
GROUP INFORMATION
-----------------
SEVENKINGDOMS\\Enterprise Admins   Group used for deny only
The command completed successfully.
"""

PARENT_EA_ENABLED = """
GROUP INFORMATION
-----------------
SEVENKINGDOMS\\Enterprise Admins   Enabled group
The command completed successfully.
"""

BENIGN_SUCCESS_ONLY = "The command completed successfully.\n"

NEGATED_MEMBERSHIP = "The user samwell is not a member of Domain Admins.\n"


def test_ticket_probe_benign_success_string_is_not_proof():
    # "The command completed successfully." alone is NOT proof of admin/Kerberos access (CRIT-A).
    probe = ca.extract_ticket_probe(BENIGN_SUCCESS_ONLY)
    assert probe["domain_admin"] is False
    assert probe["service_access_proven"] is False
    assert probe["member_of"] == []
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) != "achieved"


def test_ticket_probe_deny_only_enterprise_admins_not_membership():
    # A deny-only / disabled Enterprise Admins SID is NOT effective membership (CRIT-C).
    probe = ca.extract_ticket_probe(DENY_ONLY_EA, expected_domain=ROOT_DOM)
    assert "Enterprise Admins" not in probe["member_of"]
    assert probe["domain_admin"] is False
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) != "achieved"


def test_ticket_probe_cross_domain_child_da_does_not_prove_parent():
    # A child NORTH\\Domain Admins line must NOT prove da:sevenkingdoms.local (the parent effect) (CRIT-B).
    probe = ca.extract_ticket_probe(CHILD_DA_LINE, expected_domain=ROOT_DOM)
    assert probe["member_of"] == []
    assert probe["domain_admin"] is False
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) != "achieved"


def test_ticket_probe_cross_domain_child_fqdn_does_not_prove_parent():
    # FQDN-form child line: north.sevenkingdoms.local\Domain Admins must NOT correlate to parent
    # sevenkingdoms.local by substring (the qualifier must EQUAL the target, not merely contain it).
    child_fqdn_line = (
        "GROUP INFORMATION\n-----------------\n"
        "north.sevenkingdoms.local\\Domain Admins   Enabled group\n"
        "The command completed successfully.\n"
    )
    probe = ca.extract_ticket_probe(child_fqdn_line, expected_domain=ROOT_DOM)
    assert probe["member_of"] == []
    assert probe["domain_admin"] is False
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) != "achieved"


def test_ticket_probe_parent_fqdn_qualified_line_achieved():
    # Exact FQDN-qualified parent line DOES correlate (qualifier == target domain).
    parent_fqdn_line = (
        "GROUP INFORMATION\n-----------------\n"
        "sevenkingdoms.local\\Domain Admins   Enabled group\n"
        "The command completed successfully.\n"
    )
    probe = ca.extract_ticket_probe(parent_fqdn_line, expected_domain=ROOT_DOM)
    assert "Domain Admins" in probe["member_of"]
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) == "achieved"


def test_ticket_probe_parent_correlated_enterprise_admins_achieved():
    # The legitimate parent climb: an ENABLED, parent-correlated Enterprise Admins line IS proof.
    probe = ca.extract_ticket_probe(PARENT_EA_ENABLED, expected_domain=ROOT_DOM)
    assert "Enterprise Admins" in probe["member_of"]
    assert es.verify_effect("sid-history-escalation", CHILD_DOM, probe) == "achieved"


def test_ticket_probe_apollo_bytes_repr_directory_listing_proves_service_access():
    output = (
        r"b' Volume in drive \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$ is Windows 10\r\n"
        r" Volume Serial Number is 78B2-6C28\r\n\r\n"
        r" Directory of \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$\r\n\r\n"
        r"06/04/2026  01:22 PM    <DIR>          inetpub\r\n'"
    )

    probe = ca.extract_ticket_probe(output, expected_domain=ROOT_DOM)

    assert probe["service_access_proven"] is True
    assert probe["ticket_valid"] is True


def test_ticket_probe_negated_membership_not_achieved():
    probe = ca.extract_ticket_probe(NEGATED_MEMBERSHIP, expected_domain=DOM)
    assert probe["domain_admin"] is False
    assert es.verify_effect("golden-ticket", DOM, probe) != "achieved"


def test_redact_secretsdump_line_redacts_nt_not_lm_when_equal():
    # user:rid:LM:NT::: where LM == NT — the NT (rightmost captured) must be redacted, not the first-occurring
    # LM. The pre-fix value-based replace(NT, ..., 1) would redact the LM and leak the real NT secret.
    h = "31d6cfe0d16ae931b73c59d7e0c089c0"
    line = f"krbtgt:502:{h}:{h}:::"
    out = ca.redact_credential_material(line)
    assert out == f"krbtgt:502:{h}:<redacted>:::"


# --- 2026-06-12 live false-achieved: a NO-PROBE technique (gpo-abuse) recorded achieved off a Mythic task
#     that errored AT CREATION ("no assembly by that name" — SharpGPOAbuse never registered, never ran). The
#     narrow breaker list missed the creation-error traceback, so it fell through to the achieved default. ---

GPO_ABUSE_CREATION_ERROR = (
    "Traceback (most recent call last):\n"
    "  File \".../apollo/mythic/agent_functions/execute_assembly.py\", line 185, in create_go_tasking\n"
    "    raise Exception(f\"no assembly by that name that's not deleted\")\n"
    "Exception: no assembly by that name that's not deleted\n"
)

GPO_ABUSE_SUCCESS = (
    "[+] Domain Admins group was found.\n"
    "[+] The GPO was modified to include an immediate scheduled task. Wait for the GPO refresh cycle.\n"
)

GPO_ABUSE_GUID_ONLY = (
    "[+] Domain = north.sevenkingdoms.local\r\n"
    "[+] Domain Controller = winterfell.north.sevenkingdoms.local\r\n"
    "[+] Distinguished Name = CN=Policies,CN=System,DC=north,DC=sevenkingdoms,DC=local\r\n"
    "[+] GUID of \"STARKWALLPAPER\" is: {0A93E998-2599-4DA8-9717-6744993DED3A}\r\n"
)


def test_record_gpo_abuse_creation_error_not_achieved(mt):
    mt._pending_engagement_hop = ("gpo-abuse", "starkwallpaper", TS)
    before = len(mt._engagement_hops)
    mt._record_engagement_success(GPO_ABUSE_CREATION_ERROR)
    assert len(mt._engagement_hops) == before  # nothing recorded — the task never ran
    assert not any(h.technique == "gpo-abuse" for h in mt._engagement_hops)


def test_record_gpo_abuse_empty_output_not_achieved(mt):
    mt._pending_engagement_hop = ("gpo-abuse", "starkwallpaper", TS)
    before = len(mt._engagement_hops)
    mt._record_engagement_success("   ")
    assert len(mt._engagement_hops) == before


def test_record_gpo_abuse_setup_only_records_pending_not_achieved(mt):
    # A clean SharpGPOAbuse setup output is not enough: the GPO still needs refresh and effect proof.
    mt._pending_engagement_hop = ("gpo-abuse", "starkwallpaper", TS)
    mt._record_engagement_success(GPO_ABUSE_SUCCESS)
    hop = _last(mt)
    assert hop is not None and hop.technique == "gpo-abuse" and hop.status == "pending"
    assert hop.evidence.get("verify_verdict") == "partial"
    assert hop.effect == "system:starkwallpaper"


def test_record_gpo_abuse_guid_only_records_failed_not_waitable(mt):
    mt._pending_engagement_hop = ("gpo-abuse", "starkwallpaper", TS)
    mt._record_engagement_success(GPO_ABUSE_GUID_ONLY)
    hop = _last(mt)
    assert hop is not None and hop.technique == "gpo-abuse" and hop.status == "failed"
    assert hop.evidence.get("verify_verdict") == "failed"
    assert hop.evidence.get("artifact_present") is False
