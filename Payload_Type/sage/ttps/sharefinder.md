---
name: ShareFinder
category: discovery
subcategories: [share-enumeration, network-discovery]
tradecraft_tags: [smb-shares, network-shares, enumeration, powershell, powersploit]
mitre_attack:
  - id: T1135
    name: Network Share Discovery
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
  SMB connections to multiple hosts for share enumeration generate network events.
  Scanning many hosts rapidly is anomalous. PowerShell script block logging (Event 4104)
  captures ShareFinder invocations. NetShareEnum API calls are logged by NetLogon on
  targets.
usage_examples:
  - description: Find all accessible shares in the domain
    args: "Find-DomainShare -CheckShareAccess"
  - description: Find shares containing interesting files
    args: "Find-InterestingDomainShareFile -Include *.config,*.ini,*.xml,*.txt"
  - description: Find shares readable by the current user
    args: "Find-DomainShare -CheckShareAccess -Verbose"
opsec_notes: |
  ShareFinder is part of PowerView — AMSI must be bypassed first. SMB enumeration
  across many hosts is noisy. Use targeted enumeration (specific hosts) rather than
  domain-wide sweeps in sensitive environments. Snaffler provides a more comprehensive
  file-hunting capability with better OPSEC characteristics for large-scale share enumeration.
gotchas: |
  PowerView's `Find-DomainShare` is the ShareFinder function. Same PowerView AMSI
  caveats apply. For large domains, the domain-wide sweep can take a very long time
  and generate significant network traffic. `-CheckShareAccess` verifies actual
  read access (slower but more accurate than just listing shares).
related_ttps: [snaffler, powerview, seatbelt, sharphound]
alternatives: [snaffler, netcat-smb, crackmapexec-shares]
common_args:
  Find-DomainShare:
    description: Enumerate all reachable shares across the domain
    typical_values: [flag-only, "-CheckShareAccess"]
  Find-InterestingDomainShareFile:
    description: Search for interesting files in accessible shares
    typical_values: ["-Include *.config,*.ini,*.xml"]
  -CheckShareAccess:
    description: Verify read access before reporting shares
    typical_values: [flag-only]
  -Include:
    description: File extension filter for Find-InterestingDomainShareFile
    typical_values: ["*.config,*.ini,*.xml,*.txt,*.ps1"]
last_updated: 2026-05-29
---

# ShareFinder

The share enumeration functions from PowerView (`Find-DomainShare`, `Find-InterestingDomainShareFile`).
Enumerates accessible SMB shares across domain computers and optionally hunts for
interesting files by extension. Part of PowerView/PowerSploit — loaded via `powershell_import`
in Apollo alongside other PowerView functions.

## Typical use cases
- Discover accessible SMB shares across the domain
- Find potentially sensitive files (configs, scripts, key files) in shares
- Pre-Snaffler lightweight share survey

## How Sage uses this
ShareFinder is invoked as part of PowerView's post-foothold enumeration phase, the
same session where Sage has PowerView loaded. For comprehensive file hunting, Snaffler
is preferred. ShareFinder is useful for quick share inventory.

## Output
List of discovered shares (hostname, share name, share description, access status).
`Find-InterestingDomainShareFile` additionally lists matching file paths.
