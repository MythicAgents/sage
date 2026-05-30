---
name: SharpView
category: recon
subcategories: [ad-enumeration, acl-enumeration, dotnet-powerview]
tradecraft_tags: [dotnet, ad-enumeration, powerview-port, acl, ldap, apollo-runnable]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
  - id: T1069.002
    name: Permission Groups Discovery — Domain Groups
source:
  url: https://github.com/tevora-threat/SharpView
  license: BSD-3-Clause
  maintained: false
binary_type: .net-assembly
binary_filename: SharpView.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries against DC — same pattern as PowerView but from a .NET process instead
  of PowerShell. String-based detection for SharpView's binary name. Behavioral
  analytics for large-volume LDAP enumeration from non-DC workstations.
usage_examples:
  - description: Get all domain users
    args: "Get-DomainUser"
  - description: Get domain computers with unconstrained delegation
    args: "Get-DomainComputer -Unconstrained"
  - description: Get domain object ACLs (translated GUIDs)
    args: "Get-DomainObjectAcl -ResolveGUIDs"
  - description: Find users trusted for delegation
    args: "Get-DomainUser -TrustedToAuth"
  - description: Find domain group memberships recursively
    args: "Get-DomainGroupMember -Identity 'Domain Admins' -Recurse"
opsec_notes: |
  SharpView is a .NET assembly port of PowerView — same LDAP queries, but runs via
  inline_assembly without needing PowerShell. Avoids PowerShell script block logging
  (Event 4104) and AMSI. This is its primary advantage over PowerView in Apollo-based
  engagements. Binary string "SharpView" may be in some EDR rules; rename before upload.
gotchas: |
  Not actively maintained (last commit ~2019). Some newer PowerView functions may not
  be ported. For full ACL enumeration, verify the specific cmdlet is present in this
  version. Output format mirrors PowerView's PowerShell objects (text). For comprehensive
  attack-path analysis, SharpHound + BloodHound is still required — SharpView provides
  surgical targeted queries.
related_ttps: [powerview, sharphound, sharpdir, bloodhound-ingest]
alternatives: [powerview, sharphound, sharpdir]
common_args:
  Get-DomainUser:
    description: Enumerate domain users (same as PowerView)
    typical_values: [flag-only, "-TrustedToAuth", "-AdminCount"]
  Get-DomainComputer:
    description: Enumerate domain computers
    typical_values: [flag-only, "-Unconstrained", "-TrustedToAuth"]
  Get-DomainGroupMember:
    description: Get group membership with recursion
    typical_values: ["-Identity 'Domain Admins' -Recurse"]
  Get-DomainObjectAcl:
    description: Get AD object ACLs with GUID resolution
    typical_values: ["-ResolveGUIDs"]
  Find-DomainShare:
    description: Enumerate domain shares
    typical_values: [flag-only]
  Get-DomainTrust:
    description: Enumerate domain trusts
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpView

A C#/.NET port of PowerView — provides the same Active Directory enumeration cmdlets
(Get-DomainUser, Get-DomainObjectAcl, etc.) as a .NET assembly runnable via Apollo's
inline_assembly. The key advantage over PowerView: no PowerShell involved, so no
script block logging (Event 4104) and no AMSI exposure.

## Typical use cases
- Same as PowerView — targeted AD enumeration, ACL inspection, delegation discovery
- When PowerShell logging or AMSI makes PowerView risky, use SharpView instead
- ACL-based attack path queries from within an Apollo agent

## How Sage uses this
SharpView is Sage's preferred targeted AD enumeration tool when PowerShell is risky
(script block logging enabled, AMSI not bypassed). For comprehensive collection,
SharpHound is still the go-to. SharpView fills the targeted-query gap.

## Output
Text output mimicking PowerView's PowerShell object formatting. One object per line
for user enumeration; ACL entries as structured blocks.
