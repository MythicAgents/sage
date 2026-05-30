---
name: SharpShares
category: discovery
subcategories: [share-enumeration, network-shares, file-discovery]
tradecraft_tags: [shares, smb, enumeration, network-discovery, dotnet, apollo-runnable]
mitre_attack:
  - id: T1135
    name: Network Share Discovery
source:
  url: https://github.com/djhohnstein/SharpShares
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpShares.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  SMB connections to multiple hosts for share enumeration — same signal as
  ShareFinder (PowerView). Heavy enumeration from a single workstation is anomalous.
  Less detection signal than Snaffler (which reads files) since SharpShares only lists.
usage_examples:
  - description: Enumerate accessible shares across domain
    args: "SharpShares.exe"
  - description: Target a specific host
    args: "SharpShares.exe /Host:FILESERVER01"
  - description: Include domain controller shares
    args: "SharpShares.exe /IncludeDC"
opsec_notes: |
  SharpShares is a .NET assembly for share enumeration — Apollo inline_assembly compatible.
  Less comprehensive than Snaffler (no file content analysis) but faster and quieter.
  Produces a list of accessible share paths for the operator to review.
  Prefer Snaffler for deep file-hunting; use SharpShares for quick share inventory.
gotchas: |
  Not actively maintained. Snaffler is the more capable alternative for actual file
  hunting. SharpShares is useful for a quick inventory of accessible shares before
  deciding whether a more thorough Snaffler scan is warranted.
related_ttps: [snaffler, sharefinder, seatbelt]
alternatives: [snaffler, sharefinder-powerview, crackmapexec-shares]
common_args:
  /Host:
    description: Specific target host (instead of domain-wide scan)
    typical_values: ["FILESERVER01", "192.168.56.22"]
  /IncludeDC:
    description: Include domain controller shares in scan
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpShares

A .NET assembly for SMB share enumeration across domain computers. Identifies accessible
shares for further investigation (Snaffler for file hunting, manual browse for interesting content).

## Use Cases
- Quick share inventory before committing to a full Snaffler scan
- Identify file servers with potentially interesting data
- Map internal SMB infrastructure

## Output
List of accessible share paths (\\HOST\SHARENAME) with share descriptions.
