---
name: AD-Level Persistence Techniques
category: persistence
subcategories: [ad-persistence, golden-ticket-persistence, skeleton-key, sdprop-abuse]
tradecraft_tags: [persistence, ad, golden-ticket, skeleton-key, sdprop, adminsdHolder, dcshadow, technique]
mitre_attack:
  - id: T1098.001
    name: Account Manipulation — Additional Cloud Credentials
  - id: T1484
    name: Domain Policy Modification
source:
  url: https://attack.mitre.org/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-admin
network_required: true
detection_signal: |
  Each AD-level persistence technique generates different signals:
  - AdminSDHolder modification: Event 5136 on DC
  - Skeleton Key: Mimikatz patches LSASS (in-memory, no disk write, disappears on DC reboot)
  - DCShadow: fake DC registration, replication events
  - Golden Ticket: used when authenticating (unusual etype or group memberships)
usage_examples:
  - description: AdminSDHolder ACL modification (grants DA-equivalent to a user permanently)
    args: "Add-DomainObjectAcl -TargetIdentity 'CN=AdminSDHolder,CN=System,DC=...' -PrincipalIdentity attacker -Rights DCSync"
  - description: Skeleton Key (in-memory backdoor password on DC - disappears on reboot)
    args: "mimikatz misc::skeleton"
  - description: Golden Ticket (see golden-ticket.md for full chain)
    args: "Mimikatz kerberos::golden /krbtgt:<hash> /domain:X /sid:X /user:Administrator /ptt"
opsec_notes: |
  AD-level persistence is the highest-impact, hardest-to-remove persistence class.
  These techniques survive local remediations (password changes, agent removal) because
  they live in Active Directory itself. However, they are also the most carefully
  hunted by IR teams. Prefer these for high-value long-term access; avoid in
  short-term or scope-limited engagements.
gotchas: |
  This is a TECHNIQUE REFERENCE. Some of these techniques (Skeleton Key, DCShadow)
  are very high-risk from both detection and stability perspectives. DCShadow can
  cause AD replication issues if used incorrectly. Skeleton Key disappears on DC
  reboot — not persistent across reboots. AdminSDHolder changes ARE persistent and
  require explicit cleanup.
related_ttps: [mimikatz, golden-ticket, diamond-ticket, impacket-dacledit, acl-abuse-chain]
alternatives: [golden-ticket, forgecert-persistent-cert]
common_args: {}
last_updated: 2026-05-29
---

# AD-Level Persistence Techniques

Reference for domain-wide persistence mechanisms that survive local remediation.
These techniques modify Active Directory itself — they persist even after the C2
agent is removed, agent credentials change, or individual hosts are rebuilt.

## Persistence Options Matrix

| Technique | Persistence | Detected by | Requires | Survives |
|-----------|-----------|-------------|---------|---------|
| Golden Ticket | ~10h (until 2x krbtgt rotation) | MDI etype 23 | krbtgt hash | Password changes |
| Diamond Ticket | ~10h (like golden but stealthier) | MDI (harder) | krbtgt AES | Password changes |
| Certificate (ForgeCert) | Until cert expiry | Hard to detect | CA private key | krbtgt rotation |
| AdminSDHolder ACL | Permanent | Event 5136, Autoruns | DA | Agent removal |
| Skeleton Key | Until DC reboot | Memory scan, MDI | DA + LSASS access to DC | Most remediations |
| DCSync rights grant | Permanent | Event 4662 | WriteDACL on domain | Agent removal |
| Kerberoast with DCSync | Permanent | As used | DA | Password change (must update hash) |

## AdminSDHolder Abuse (Most Durable)

AdminSDHolder is a system container that replicates its ACL to all protected users/groups
every 60 minutes (SDProp process). Adding an ACE to AdminSDHolder grants persistent
rights to all protected AD principals:

```powershell
# Grant GenericAll or DCSync to attacker account via AdminSDHolder:
Add-DomainObjectAcl -TargetIdentity 'CN=AdminSDHolder,CN=System,DC=DOMAIN,DC=COM' \
    -PrincipalIdentity attacker -Rights All -Verbose

# The SDProp process runs every 60 minutes and will propagate this ACE
# to all protected users/groups (Domain Admins, krbtgt, etc.)
```

Detection: Event 5136 on DC, Bloodhound shows AdminSDHolder ACL edges.
Cleanup: Remove the ACE AND manually revert all protected objects' ACLs.

## Skeleton Key (In-Memory, Non-Persistent)

```
# From DA context on a DC:
mimikatz misc::skeleton
# Sets a universal master password ("mimikatz") for ALL domain accounts
# Use: authenticate as any user with "mimikatz" as the password
# Caution: disappears on DC reboot, only affects that DC
# Not persistent — only documents here for completeness
```

## Recommended Persistence Approach

For Sage engagements prioritizing stealth over dwell time:
1. **Certificate** (ForgeCert + CA key) — stealthiest, long-lived
2. **DCSync rights via AdminSDHolder** — persistent, moderate detection
3. **Golden/Diamond Ticket** — operational convenience, survives password changes

For short-term operational persistence (< 24 hours):
- Golden Ticket with krbtgt hash is sufficient and leaves minimal artifacts
