---
name: SharpDir
category: discovery
subcategories: [ad-enumeration, ldap-only, quiet-recon]
tradecraft_tags: [ldap, ad-enumeration, quiet, stealth, dotnet]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/FuzzySecurity/Sharp-Suite
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: SharpDir.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries against the DC — quieter than SharpHound's full collection because
  SharpDir only does LDAP (no SAMR enumeration, no computer-side queries). Detectable
  by LDAP audit logging on the DC and MDI anomalous query detection, but lower signal
  than a full SharpHound pass.
usage_examples:
  - description: Enumerate all users in domain
    args: "--Domain north.sevenkingdoms.local --Filter users"
  - description: Enumerate all computers
    args: "--Domain north.sevenkingdoms.local --Filter computers"
  - description: Enumerate all groups and memberships
    args: "--Domain north.sevenkingdoms.local --Filter groups"
  - description: Find all objects with specific SPN
    args: "--Domain north.sevenkingdoms.local --Filter users --SPN *MSSQLSvc*"
opsec_notes: |
  SharpDir is quieter than SharpHound because it uses LDAP-only (no SAMR, no NetSessionEnum,
  no computer-side enumeration). Use when a full SharpHound pass is too risky but targeted
  LDAP queries are needed. FuzzySecurity's Sharp-Suite contains multiple tools; SharpDir
  is the AD enumeration component.
gotchas: |
  SharpDir is part of FuzzySecurity's Sharp-Suite project — download SharpDir.exe specifically.
  Less feature-rich than PowerView for ACL enumeration but sufficient for basic user/computer/
  group inventory. For full attack-path analysis, SharpHound + BloodHound is required.
related_ttps: [sharphound, powerview, adexplorer, seatbelt]
alternatives: [powerview, sharphound-stealth, adexplorer]
common_args:
  --Domain:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  --Filter:
    description: Object type to enumerate
    typical_values: [users, computers, groups, ous, gpos]
  --SPN:
    description: SPN filter for user/computer queries
    typical_values: ["*MSSQLSvc*", "*HTTP*"]
last_updated: 2026-05-29
---

# SharpDir

FuzzySecurity's lightweight LDAP-only Active Directory enumeration tool. SharpDir performs
targeted LDAP queries for users, computers, groups, and OUs without the SAMR-based,
session-based, or computer-side enumeration that makes SharpHound loud. Useful for a
quiet recon pass when the cost of full SharpHound collection is too high.

## Typical use cases
- Quiet targeted LDAP enumeration without full SharpHound-level noise
- Enumerate specific object types (users with SPN, computers, groups)
- Supplement BloodHound data with targeted queries

## How Sage uses this
SharpDir is used in stealth-priority engagements where SharpHound's full collection
would be too loud. It provides enough data for targeted ACL and delegation decisions
without triggering full enumeration detection signatures.

## Output
Text-format enumeration results per object type.
