---
name: PowerView
category: recon
subcategories: [ad-enumeration, acl-enumeration, trust-enumeration]
tradecraft_tags: [powershell, ad-enumeration, ldap, acl, trusts, harmj0y, powersploit]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
  - id: T1069.002
    name: Permission Groups Discovery — Domain Groups
  - id: T1482
    name: Domain Trust Discovery
source:
  url: https://github.com/PowerShellMafia/PowerSploit/blob/master/Recon/PowerView.ps1
  license: BSD-3-Clause
  maintained: false
binary_type: powershell-script
binary_filename: PowerView.ps1
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  PowerView generates high-volume LDAP queries from PowerShell. PowerShell script block
  logging (Event 4104) captures the invoked commands and AMSI submissions flag suspicious
  PowerShell patterns. Defender/AMSI has signatures for PowerView function names. EDRs
  detect `.ps1` import + rapid AD LDAP enumeration. LDAP server logs show heavy queries
  from non-DC workstations.
usage_examples:
  - description: Enumerate all domain users
    args: "Get-DomainUser -Domain north.sevenkingdoms.local"
  - description: Get all computers in domain
    args: "Get-DomainComputer -Properties dnshostname,operatingsystem,lastlogon"
  - description: Find all group memberships including nested
    args: "Get-DomainGroupMember -Identity 'Domain Admins' -Recurse"
  - description: Get ACL entries — find who has GenericAll/GenericWrite on what
    args: "Get-DomainObjectAcl -SearchBase 'DC=north,DC=sevenkingdoms,DC=local' -ResolveGUIDs | Where-Object {$_.ActiveDirectoryRights -match 'GenericAll|GenericWrite|WriteDACL'}"
  - description: Find all domain trusts
    args: "Get-DomainTrust"
  - description: Find users with constrained delegation configured
    args: "Get-DomainUser -TrustedToAuth"
  - description: Find computers with unconstrained delegation
    args: "Get-DomainComputer -Unconstrained"
opsec_notes: |
  PowerView is heavily AMSI-signatured — function names like `Get-DomainUser`,
  `Invoke-ACLScanner`, and `Find-LocalAdminAccess` are in AMSI blocklists. Either use
  a PowerSploit fork with obfuscated function names or run from an AMSI-patched
  PowerShell session. For Apollo, `powershell_import` with AMSI bypass first, or use
  SharpHound (which is quieter) for initial enumeration. PowerView's advantage over
  SharpHound is targeted query flexibility — SharpHound collects everything; PowerView
  lets you run specific queries without the overhead.
gotchas: |
  PowerView's maintenance status is ambiguous (PowerSploit repo archived ~2022). The
  standalone `dev` branch at https://github.com/ZeroDayLab/PowerSploit or community
  forks are more up-to-date. Some cmdlets require an LDAP filter that may time out on
  large domains; use `-ResultPageSize 100` for large result sets. The PowerShell import
  is persistent across the agent session via `powershell_import` — subsequent commands
  don't need to re-import. ACL enumeration with `Get-DomainObjectAcl` is slow on large
  domains; prefer SharpHound for bulk ACL collection.
related_ttps: [sharphound, seatbelt, sharpdir, adexplorer]
alternatives: [sharphound, adexplorer, sharpdir, ldapsearch]
common_args:
  Get-DomainUser:
    description: Enumerate domain users; filter with -Identity, -AdminCount, -TrustedToAuth
    typical_values: [flag-only, "-Domain X", "-TrustedToAuth", "-AdminCount"]
  Get-DomainComputer:
    description: Enumerate domain computers; filter with -Unconstrained, -TrustedToAuth
    typical_values: [flag-only, "-Unconstrained", "-TrustedToAuth"]
  Get-DomainGroup:
    description: Enumerate domain groups
    typical_values: [flag-only, "-Identity 'Domain Admins'"]
  Get-DomainGroupMember:
    description: Get group membership including recursive (nested group expansion)
    typical_values: ["-Identity 'Domain Admins' -Recurse"]
  Get-DomainObjectAcl:
    description: Get ACL entries on AD objects; use -ResolveGUIDs for readable output
    typical_values: ["-SearchBase 'DC=...' -ResolveGUIDs"]
  Get-DomainTrust:
    description: Enumerate domain and forest trusts
    typical_values: [flag-only]
  Find-LocalAdminAccess:
    description: Find machines where the current user has local admin (uses parallelized SMB)
    typical_values: [flag-only]
  Get-DomainController:
    description: List domain controllers
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# PowerView

HarmJ0y's PowerShell Active Directory reconnaissance library, part of PowerSploit.
PowerView wraps raw .NET LDAP and ADSI calls in convenient PowerShell cmdlets for
enumerating users, computers, groups, ACLs, trusts, GPOs, and delegation settings.
Unlike SharpHound (which does a full collection pass for BloodHound), PowerView is
surgical — operators run targeted queries to answer specific questions ("who has
GenericWrite on this computer?", "which users are trusted for Kerberos delegation?").

## Typical use cases
- Targeted ACL enumeration to find privilege escalation paths (GenericAll/Write chains)
- Trust enumeration for cross-forest attack planning
- Find accounts with unconstrained / constrained delegation configured
- Identify SPNs (kerberoastable accounts) without a full SharpHound sweep
- Discover domain group memberships interactively
- Enumerate OU/container structure for GPO attack scope

## How Sage uses this
PowerView is used for targeted follow-up queries after SharpHound's bulk collection.
When SharpHound output lacks detail on a specific ACL or when Sage needs fresh data
on a specific account, PowerView provides surgical LDAP queries. The typical pattern:
- SharpHound for initial broad collection → BloodHound for attack path analysis
- PowerView for specific targeted queries that BloodHound paths need clarified

Apollo runs PowerView via `powershell_import` — the script is loaded once and then
cmdlets are invoked as needed.

## Output
PowerShell object output (formatted as property tables in console). Pipe to
`Select-Object`, `Where-Object`, and `Format-List` for filtering. Key cmdlets
produce structured objects that can be serialized to JSON for Sage to parse.

## OPSEC considerations
PowerView function names are AMSI-signatured. Must be loaded in an AMSI-patched
session or via an obfuscated/renamed variant. The LDAP queries are functionally
identical to SharpHound's but manual — defenders watching for unusual LDAP volume
from workstations will see both. PowerView is also visible in PowerShell script block
logging (Event 4104) if that's enabled.

## Full Reference

> Captured against PowerView v3.0 (dev branch), 2026-05-29.
> Source: https://github.com/PowerShellMafia/PowerSploit/blob/master/Recon/PowerView.ps1
> and HarmJ0y blog posts.

### Core enumeration cmdlets

| Cmdlet | Description |
|--------|-------------|
| `Get-DomainUser` | User enumeration with LDAP filtering |
| `Get-DomainComputer` | Computer object enumeration |
| `Get-DomainGroup` | Group enumeration |
| `Get-DomainGroupMember` | Group membership (recursive) |
| `Get-DomainController` | Domain controller list |
| `Get-DomainOU` | Organizational unit enumeration |
| `Get-DomainGPO` | Group Policy Object enumeration |
| `Get-DomainTrust` | Domain/forest trust enumeration |
| `Get-ForestTrust` | Forest-level trust enumeration |
| `Get-DomainForeignUser` | Users who are members of groups in other domains |
| `Get-DomainForeignGroupMember` | Foreign group members |

### ACL and access cmdlets

| Cmdlet | Description |
|--------|-------------|
| `Get-DomainObjectAcl` | Get raw ACL; use -ResolveGUIDs to translate GUID names |
| `Add-DomainObjectAcl` | Add ACE to a domain object (requires appropriate rights) |
| `Remove-DomainObjectAcl` | Remove ACE |
| `Invoke-ACLScanner` | Find potentially-abusable ACL entries for a given principal |

### Delegation cmdlets

| Cmdlet | Description |
|--------|-------------|
| `Get-DomainComputer -Unconstrained` | Computers with unconstrained delegation |
| `Get-DomainUser -TrustedToAuth` | Users trusted for constrained delegation (protocol transition) |
| `Get-DomainComputer -TrustedToAuth` | Computers trusted for constrained delegation |

### Search / reachability cmdlets

| Cmdlet | Description |
|--------|-------------|
| `Find-LocalAdminAccess` | Find machines where current user has local admin |
| `Find-DomainUserLocation` | Find machines where target user has an active session |
| `Test-AdminAccess` | Check admin access to a specific computer |
| `Find-InterestingDomainAcl` | Find ACLs with interesting permissions |
| `Find-DomainShare` | Find accessible shares |

### Common filter arguments (apply to most Get-Domain* cmdlets)

| Arg | Description |
|-----|-------------|
| `-Identity X` | Filter by LDAP attribute (name, SID, SamAccountName) |
| `-Domain X` | Target domain FQDN |
| `-DomainController X` | Specific DC to query |
| `-LDAPFilter X` | Raw LDAP filter string |
| `-Properties X` | Return only specified properties (comma-separated) |
| `-ResultPageSize X` | LDAP paging size (default 200; reduce for rate limiting) |
| `-SearchBase X` | Custom LDAP search base |
| `-SearchScope X` | Base, OneLevel, or Subtree |
| `-AdminCount` | Filter for AdminCount=1 (protected users) |

### Source for this reference

- https://github.com/PowerShellMafia/PowerSploit (README + script comments)
- HarmJ0y blog: https://harmj0y.medium.com/ (multiple PowerView posts)
- Version: v3.0-dev branch as of 2026-05-29 (PowerSploit archived ~2022)
