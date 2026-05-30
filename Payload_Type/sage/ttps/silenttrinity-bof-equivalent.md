---
name: Havoc / Sliver BOF Integration
category: command-and-control
subcategories: [bof-runner, alternative-c2, bof-ecosystem]
tradecraft_tags: [bof, havoc, sliver, bof-runner, c2, alternative-framework, reference]
mitre_attack: []
source:
  url: https://github.com/HavocFramework/Havoc
  license: GPL-3.0
  maintained: true
binary_type: bof
binary_filename: (any .x64.o file)
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Same as Cobalt Strike BOF execution — in-process, no child process. The C2 framework
  (Havoc, Sliver) generates its own detection signals for the agent itself.
usage_examples:
  - description: Execute any BOF via Havoc's Devil-BOF command
    args: "bof execute /path/to/BOF.x64.o [args]"
  - description: Execute any BOF via Sliver's bof command
    args: "bof -P BOF.x64.o [args]"
opsec_notes: |
  Havoc and Sliver are open-source C2 frameworks with first-class BOF support.
  This file documents them as BOF-execution alternatives to Apollo/Athena when
  Mythic is not available. Within the Mythic ecosystem, Athena is the BOF-capable agent.
gotchas: |
  This is a REFERENCE document about the BOF ecosystem — not a specific tool TTP.
  For Mythic-based operations, Athena is the canonical BOF runner. Havoc and Sliver
  are documented here for operator awareness of the broader BOF landscape.
related_ttps: [trustedsec-bofs, outflank-remote-ops-bofs, bofnet, nanodump]
alternatives: [athena-execute-bof]
common_args: {}
last_updated: 2026-05-29
---

# Havoc / Sliver BOF Integration

Reference document for BOF execution across the major C2 frameworks that support
the Cobalt Strike BOF API. All BOF files (.x64.o) are compiled to a common ABI
and are generally cross-compatible with any C2 framework that implements the BOF
runner interface.

## BOF Compatibility Matrix

| C2 Framework | BOF Support | BOF Command | Notes |
|-------------|-------------|-------------|-------|
| Cobalt Strike | Native | `inline-execute` | Reference implementation |
| Mythic (Athena) | Via execute-bof | `execute-bof` | Athena-specific; see athena.md |
| Havoc | Native | `bof execute` | Open-source; strong BOF support |
| Sliver | Via extension | `bof -P` | Extension-based BOF loading |
| Brute Ratel C4 | Native | (varies) | Commercial alternative |
| Apollo (Mythic) | NOT SUPPORTED | — | Primary gap; see apollo.md |

## BOF Argument Packing Compatibility

The BOF argument packing format varies slightly between frameworks:
- Cobalt Strike: uses `bof_pack` helper in COFF format
- Athena: same packing format as Cobalt Strike (designed for compatibility)
- Havoc: similar format, some BOFs require minor modifications
- Sliver: may need wrappers for complex argument types

## The BOF Ecosystem for Sage

Within Sage's Mythic deployment:
- **Apollo** → no BOFs → fall back to Seatbelt, SharpView, SharpUp (.NET assemblies)
- **Athena** → execute-bof → full access to all BOF collections

The BOF collections Sage should have access to (via Athena):
1. TrustedSec CS-Situational-Awareness-BOFs (discovery)
2. Outflank CS-Remote-OPs-BOFs (remote operations, token, ExecuteAssembly)
3. BOF.NET (persistent .NET CLR in-process)
4. nanodump (LSASS credential access)
5. Dumpert (LSASS credential access)
6. KerbDump (Kerberos ticket extraction without LSASS open)
7. BOF-RegHunter (credential registry hunting)
