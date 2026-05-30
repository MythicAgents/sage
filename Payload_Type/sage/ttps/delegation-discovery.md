---
name: Delegation Configuration Discovery
category: recon
subcategories: [delegation, constrained, unconstrained, rbcd, discovery]
tradecraft_tags: [delegation, unconstrained, constrained, rbcd, discovery, technique, reference]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://attack.mitre.org/techniques/T1134/001/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries for delegation attributes are standard and not anomalous. SharpHound
  collects this data as part of normal collection. PowerView queries are slightly louder
  than SharpHound collection but still within normal LDAP usage.
usage_examples:
  - description: Find computers with unconstrained delegation (PowerView)
    args: "Get-DomainComputer -Unconstrained | Select name, dnshostname"
  - description: Find users trusted for constrained delegation (protocol transition)
    args: "Get-DomainUser -TrustedToAuth | Select samaccountname, msds-allowedtodelegateto"
  - description: Find computers trusted for constrained delegation
    args: "Get-DomainComputer -TrustedToAuth | Select name, msds-allowedtodelegateto"
  - description: Find RBCD configurations (existing delegations on computer objects)
    args: "Get-DomainComputer | Where {$_.msDS-AllowedToActOnBehalfOfOtherIdentity}"
  - description: BloodHound Cypher — all unconstrained delegation computers (non-DC)
    args: "MATCH (c:Computer {unconstraineddelegation:true}) WHERE NOT c.name STARTS WITH 'DC' RETURN c"
  - description: BloodHound Cypher — constrained delegation chains
    args: "MATCH p=(a)-[:AllowedToDelegate]->(b:Computer) RETURN p"
  - description: SharpHound collects all delegation data in DCOnly or All mode
    args: "-c All (or DCOnly) — delegation is in the collection"
opsec_notes: |
  Delegation discovery is entirely passive LDAP enumeration — very low noise. This is
  standard post-foothold reconnaissance. SharpHound collects all delegation data
  automatically; these queries are for targeted or fresh lookups.
gotchas: |
  This is a REFERENCE DOCUMENT for delegation discovery patterns, not a tool.
  Three delegation types require different exploitation approaches:
  1. Unconstrained: TGT capture via coercion + Rubeus monitor
  2. Constrained (with protocol transition): S4U chain via Rubeus s4u
  3. RBCD: Attacker writes delegation attribute + S4U chain
  BloodHound shows delegation in the graph; check for TrustedToAuth and Unconstrained
  computer/user nodes.
related_ttps: [sharphound, powerview, rubeus, unconstrained-delegation-abuse, constrained-delegation-abuse, rbcd-abuse]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Delegation Configuration Discovery

A reference for discovering all three types of Kerberos delegation in an Active Directory
environment. Delegation misconfigurations are among the most impactful privilege escalation
paths in enterprise AD environments.

## The Three Delegation Types

| Type | AD Attribute | UAC Flag | Discovery | Exploitation |
|------|-------------|---------|-----------|-------------|
| **Unconstrained** | TrustedForDelegation=True | TRUSTED_FOR_DELEGATION | Get-DomainComputer -Unconstrained | Rubeus monitor + coercion |
| **Constrained** | msDS-AllowedToDelegateTo | TRUSTED_TO_AUTH_FOR_DELEGATION | Get-DomainUser/Computer -TrustedToAuth | Rubeus s4u |
| **RBCD** | msDS-AllowedToActOnBehalfOfOtherIdentity | (none — attribute-based) | Get-DomainComputer where attr set | StandIn + Rubeus s4u |

## Quick Discovery Commands

### PowerView
```powershell
# Unconstrained delegation machines (most critical):
Get-DomainComputer -Unconstrained -Properties Name, dnshostname | Sort Name

# Constrained delegation with protocol transition (S4U-exploitable):
Get-DomainUser -TrustedToAuth -Properties SamAccountName, msDS-AllowedToDelegateTo
Get-DomainComputer -TrustedToAuth -Properties Name, msDS-AllowedToDelegateTo

# RBCD configured (existing):
Get-DomainComputer | Where-Object { $_.'msDS-AllowedToActOnBehalfOfOtherIdentity' -ne $null }
```

### BloodHound Cypher
```cypher
-- Unconstrained delegation (non-DC):
MATCH (c:Computer {unconstraineddelegation:true})
WHERE NOT c.name STARTS WITH 'DC'
RETURN c.name ORDER BY c.name

-- Constrained delegation attack paths:
MATCH p=(a)-[:AllowedToDelegate]->(b:Computer)
WHERE a.owned = true
RETURN p

-- RBCD writable attack paths:
MATCH p=(a)-[:GenericWrite|GenericAll|WriteDACL]->(b:Computer)
WHERE a.owned = true
RETURN p
```

## Priority Ordering

Highest value for privilege escalation:
1. **Unconstrained on non-DC server** (Exchange, SQL, app servers) → coerce DC → DA
2. **Constrained with protocol transition on machine account** → S4U → admin on target
3. **Constrained with protocol transition on user account** → S4U → lateral movement to SPN hosts
4. **GenericWrite on computer** → RBCD setup → admin on that computer
