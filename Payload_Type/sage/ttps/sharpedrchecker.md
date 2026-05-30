---
name: SharpEDRChecker
category: discovery
subcategories: [edr-detection, av-detection, security-product-fingerprint]
tradecraft_tags: [edr, av, detection, fingerprint, endpoint-security, dotnet, apollo-runnable]
mitre_attack:
  - id: T1518.001
    name: Software Discovery — Security Software Discovery
source:
  url: https://github.com/PwnDexter/SharpEDRChecker
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: SharpEDRChecker.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  SharpEDRChecker reads process names, DLL paths, file metadata, and services to
  fingerprint security products. The individual read operations are benign — WMI
  process enumeration, file metadata reads — but the combination (rapid enumeration
  of security-related process names) may be detectable by behavioral analytics.
  Paradoxically, EDRs themselves may flag the "EDR discovery" behavior.
usage_examples:
  - description: Enumerate all running security products
    args: "SharpEDRChecker.exe"
  - description: Check specific process list against known EDR signatures
    args: "SharpEDRChecker.exe -p"
  - description: Check loaded DLLs in running processes
    args: "SharpEDRChecker.exe -d"
opsec_notes: |
  Running SharpEDRChecker early in the engagement tells you which EDR vendor is
  deployed — critical for choosing the right evasion path. The check itself is
  mostly benign reads, but some EDRs detect "EDR fingerprinting" behaviors. Best
  practice: run quickly early, use results to tune evasion approach for the session.
gotchas: |
  Seatbelt's `AntiVirus` check covers some of the same ground via WMI SecurityCenter2
  queries. SharpEDRChecker goes deeper (process DLLs, service names, file paths).
  The two are complementary. EDR fingerprint is important to know BEFORE running noisy
  tools — if CrowdStrike Falcon is deployed, certain bypass paths won't work at all.
related_ttps: [seatbelt, watson, sharpup]
alternatives: [seatbelt-antivirus, inline-process-enum]
common_args:
  -p:
    description: Check processes only
    typical_values: [flag-only]
  -d:
    description: Check loaded DLLs in processes
    typical_values: [flag-only]
  -f:
    description: Check files/services
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpEDRChecker

A .NET assembly for fingerprinting endpoint security products (EDR, AV, HIPS) by
checking process names, loaded DLL paths, service names, and file metadata against
a database of known security vendor patterns. The output tells an operator which
security products are active, informing bypass strategy selection.

## Typical use cases
- Identify the active EDR vendor before attempting evasion (CrowdStrike vs SentinelOne vs Defender)
- Discover AV products that may block tool uploads
- Confirm security stack before running high-risk operations

## How Sage uses this
SharpEDRChecker is an early-phase reconnaissance tool. Knowing the EDR vendor allows
Sage to:
- Select appropriate evasion paths (some work better against specific vendors)
- Warn the operator before running tools that are likely to be caught
- Adjust tool naming and delivery for the specific security stack

## Output
Text listing of detected security products with vendor name, process path, and
signature match type (process name, DLL, service, or file).
