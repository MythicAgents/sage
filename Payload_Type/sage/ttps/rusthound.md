---
name: RustHound
category: recon
subcategories: [ad-enumeration, attack-path-mapping, bloodhound-collector]
tradecraft_tags: [bloodhound, rust, cross-platform, ad-enumeration, sharphound-alternative]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
  - id: T1069.002
    name: Permission Groups Discovery — Domain Groups
source:
  url: https://github.com/OPENCYBER-FR/RustHound
  license: AGPL-3.0
  maintained: true
binary_type: native-exe
binary_filename: rusthound.exe
supported_os: [windows, linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Same detection surface as SharpHound — heavy LDAP queries against the DC's naming
  context. LDAP query pattern analysis by MDI / Defender for Identity. RustHound is
  not string-signatured as heavily as SharpHound (newer tool), but the LDAP behavior
  is equivalent.
usage_examples:
  - description: Collect all domain data (Windows, authenticated as domain user)
    args: "-d north.sevenkingdoms.local -u jon.snow -p Password123 --zip"
  - description: Collect from Linux with explicit DC and output directory
    args: "-d essos.local -u daenerys.targaryen -p 'DragonMother1' --dc 192.168.56.12 -o /tmp/bh-data --zip"
  - description: LDAP-only stealth collection (DCOnly equivalent)
    args: "-d north.sevenkingdoms.local -u jon.snow -p Password123 --ldap-only --zip"
opsec_notes: |
  RustHound has better cross-platform support than SharpHound (runs natively on Linux).
  LDAP query pattern is similar to SharpHound — MDI and network monitoring will see
  the same enumeration behavior. Advantage: binary doesn't carry "SharpHound" string
  signatures. `--ldap-only` is the stealth equivalent of SharpHound's `--Stealth`.
gotchas: |
  RustHound is a native EXE (Rust-compiled) — Apollo cannot use inline_assembly for it.
  For Apollo engagements, prefer SharpHound (.net-assembly). RustHound is most useful
  from Linux attack infrastructure or for situations where .NET is unavailable on the
  target. Output is BloodHound CE-compatible JSON/ZIP.
related_ttps: [sharphound, bloodhound-ingest, powerview]
alternatives: [sharphound, adexplorer, ldapsearch]
common_args:
  -d:
    name: --domain
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -u:
    name: --username
    description: Domain username
    typical_values: ["jon.snow"]
    required: true
  -p:
    name: --password
    description: Domain password
    typical_values: ["Password123"]
  --dc:
    description: Specific DC IP or FQDN
    typical_values: ["192.168.56.10"]
  -o:
    name: --output
    description: Output directory
    typical_values: ["/tmp/bh", "C:\\\\Windows\\\\Temp"]
  --zip:
    description: Output as ZIP file (BloodHound-ready)
    typical_values: [flag-only]
  --ldap-only:
    description: LDAP-only collection (quieter, like SharpHound --Stealth)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# RustHound

A Rust-language cross-platform BloodHound data collector. RustHound replicates
SharpHound's collection capabilities from Linux or Windows without requiring a .NET
runtime. Produces BloodHound CE-compatible ZIP files with the same collection methods.
Primarily useful for engagements where collection is performed from Linux attack
infrastructure or where .NET assembly delivery is not available.

## Typical use cases
- Collect BloodHound data from a Linux machine (joined to or with creds in the domain)
- Alternative to SharpHound when .NET delivery is unavailable
- Cross-platform BloodHound data collection pipeline

## How Sage uses this
RustHound is an alternative collector for non-Windows-assembly scenarios. For Apollo
engagements, SharpHound is preferred (inline_assembly delivery). RustHound may be
used when Sage runs from infrastructure that can execute binaries directly.

## Apollo-specific note
Native EXE (Rust binary) — Apollo's inline_assembly won't run this. Prefer SharpHound
(.net-assembly) for Apollo-based delivery.
