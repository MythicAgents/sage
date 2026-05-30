---
name: SharpHandler
category: credential-access
subcategories: [handle-hijacking, lsass-alternative, duplicate-handle, bof-technique]
tradecraft_tags: [handle, duplicate, lsass, credential-access, no-openprocess, evasion, dotnet, bof]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/jfmaes/SharpHandler
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpHandler.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  SharpHandler finds processes that already have an open handle to LSASS (e.g.
  antivirus, EDR, or other system processes) and duplicates that handle — avoiding a
  direct NtOpenProcess(lsass.exe) call from the attacking process. The DuplicateHandle
  operation is visible to EDR via ObRegisterCallbacks watching handle duplication,
  but has a different detection footprint than direct LSASS opens.
usage_examples:
  - description: Find processes with existing LSASS handles and duplicate one
    args: "SharpHandler.exe find"
  - description: Duplicate an existing handle from a specific process to dump LSASS
    args: "SharpHandler.exe dump --pid <process_with_handle>"
  - description: Use the handle to dump to disk
    args: "SharpHandler.exe dump --write C:\\Windows\\Temp\\out.dmp"
opsec_notes: |
  Handle duplication is the LSASS access technique that has the best chance of
  bypassing EDR rules specifically watching for NtOpenProcess(lsass.exe) from the
  agent process. By reusing handles that legitimate processes (AV engines, backup tools)
  already hold, the agent never directly opens LSASS. However:
  - EDR that monitors ALL handle duplication (ObRegisterCallbacks) will catch this
  - CrowdStrike Falcon specifically monitors handle duplication to LSASS
  Prefer nanodump (indirect syscalls + PssCaptureSnapshot) over SharpHandler for
  modern EDR environments. SharpHandler is most effective against legacy EDR.
gotchas: |
  Not actively maintained. The tool relies on finding a process that ALREADY has an
  open LSASS handle — this depends on what's running on the target (typically AV/EDR
  or Windows processes like MsMpEng.exe, antimalware). If no such process exists, the
  technique fails. This is a dependent technique, not a standalone one.
related_ttps: [nanodump, nanodump-bof-expanded, kerbdump-bof, mimikatz]
alternatives: [nanodump, dumpert]
common_args:
  find:
    description: Find all processes with existing open LSASS handles
    typical_values: [flag-only]
  dump:
    description: Duplicate a handle and dump LSASS
    typical_values: [flag-only]
  --pid:
    description: Specific process PID to duplicate handle from
    typical_values: ["<MsMpEng-PID>", "<AV-process-PID>"]
  --write:
    description: Output dump file path
    typical_values: ["C:\\\\Windows\\\\Temp\\\\out.dmp"]
last_updated: 2026-05-29
---

# SharpHandler

A .NET assembly that finds existing open handles to LSASS.exe in other processes and
duplicates them — avoiding the direct NtOpenProcess(lsass.exe) call that most EDR rules
watch for. By reusing handles held by legitimate processes (antivirus engines, Windows
system processes), the attacking process never appears as a direct LSASS accessor.

## The Handle Duplication Approach

```
Standard LSASS dump:
  AttackingProcess → NtOpenProcess(lsass.exe) → read memory → dump
  EDR: sees direct handle acquisition to lsass.exe by attacking process → ALERT

Handle duplication approach:
  1. Find processes that already have lsass handles (e.g. MsMpEng.exe, svchost.exe)
  2. Duplicate handle from MsMpEng → attacking process
  3. Read LSASS memory using the duplicated handle
  EDR: sees handle dup from MsMpEng → less suspicious path
```

## Process Types That Often Have LSASS Handles

- `MsMpEng.exe` (Windows Defender)
- `svchost.exe` (LSASS Monitor service)
- EDR agent process (ironically has an LSASS handle)
- Backup software agents
- Password manager agents

## How Sage uses this

SharpHandler is a fallback LSASS dump technique when nanodump's indirect syscall approach
is detected. It's most effective against endpoint security that doesn't monitor
DuplicateHandle operations (older EDR). For modern CrowdStrike/SentinelOne, nanodump
with PssCaptureSnapshot and indirect syscalls is preferred.
