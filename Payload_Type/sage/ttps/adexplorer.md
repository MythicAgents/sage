---
name: ADExplorer
category: recon
subcategories: [ad-enumeration, lolbin, sysinternals]
tradecraft_tags: [sysinternals, lolbin, ad-enumeration, ldap, snapshot, trusted-binary]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://learn.microsoft.com/en-us/sysinternals/downloads/adexplorer
  license: Unknown
  maintained: true
binary_type: native-exe
binary_filename: ADExplorer.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  ADExplorer is a signed Sysinternals binary — most AV/EDR allowlists it. Detection
  comes from behavioral analysis: LDAP queries from ADExplorer are functionally identical
  to SharpHound but from a signed, legitimate binary. Snapshot files (.dat) written to
  disk are anomalous for normal ADExplorer use and can be detected by file monitoring.
  LOLBin abuse: signed binary doing recon is harder to detect than custom tools.
usage_examples:
  - description: Take a snapshot of AD for offline analysis
    args: "ADExplorer.exe -snapshot '' output.dat"
  - description: Interactive browsing (GUI; no command line for interactive mode)
    args: "(connect to domain via GUI, navigate LDAP tree)"
  - description: Open existing snapshot offline
    args: "ADExplorer.exe output.dat"
opsec_notes: |
  ADExplorer's LOLBIN status (signed Sysinternals binary) makes it appealing for
  enumeration in environments where SharpHound is blocked. The snapshot file is a
  portable offline copy of AD — exfiltrate it and analyze offline. Snap mode is
  non-interactive and can be automated. The snapshot file format can be parsed by
  bloodhound-python and other tools to generate BloodHound-compatible output.
gotchas: |
  ADExplorer is a NATIVE EXE — Apollo cannot use inline_assembly. It must be uploaded
  and executed differently. The snapshot includes all accessible AD objects and attributes
  but NOT ACL information (no DACL in the snapshot format) — SharpHound is still needed
  for ACL-based attack path analysis. Snapshot files can be large (GB for large domains).
  BloodHound-import of ADExplorer snapshots requires additional tooling (AdExplorer2BloodHound).
related_ttps: [sharphound, powerview, sharpdir, bloodhound-ingest]
alternatives: [sharphound, powerview, ldap-search]
common_args:
  -snapshot:
    name: -snapshot
    description: Take a snapshot to file (non-interactive)
    typical_values: ["'' output.dat"]
  output.dat:
    description: Snapshot output file path
    typical_values: ["C:\\\\Windows\\\\Temp\\\\snap.dat"]
last_updated: 2026-05-29
---

# ADExplorer

Microsoft Sysinternals' Active Directory Explorer. A signed, legitimate AD administration
tool that serves as an LOLBIN for stealthy AD enumeration. ADExplorer's snapshot feature
creates an offline portable copy of the AD database (all objects, attributes) that can
be exfiltrated and analyzed without further network access. Particularly valuable in
environments where custom enumeration tools are blocked but Sysinternals binaries are trusted.

## Typical use cases
- Take an AD snapshot for offline analysis (entire object tree minus DACLs)
- Enumerate AD attributes without using custom enumeration tooling
- LOLBin enumeration path when SharpHound/PowerView are blocked
- Analyze AD structure offline from exfiltrated snapshot

## How Sage uses this
ADExplorer is a fallback enumeration option when SharpHound is blocked. The snapshot
approach — collect everything offline, then analyze — is operationally useful even without
BloodHound integration. Native EXE means it requires non-assembly execution in Apollo.

## Apollo-specific note
Native EXE — inline_assembly won't run this. Requires upload to disk and execution
via a different primitive. For .NET-based enumeration, prefer SharpHound.

## Output
- Interactive mode: GUI LDAP tree browser
- Snapshot mode: Binary `.dat` file containing all accessible AD objects and attributes
- Snapshot can be parsed offline with ADExplorer2BloodHound for BloodHound import
