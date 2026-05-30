---
name: Parent PID Spoofing
category: defense-evasion
subcategories: [ppid-spoof, parent-process, process-creation, evasion]
tradecraft_tags: [ppid, parent-process, spoof, defense-evasion, process-creation, bof, dotnet]
mitre_attack:
  - id: T1134.004
    name: Access Token Manipulation — Parent PID Spoofing
source:
  url: https://github.com/boku7/spawn
  license: Unknown
  maintained: false
binary_type: bof
binary_filename: spawn.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Parent PID spoofing creates a process whose listed parent PID doesn't match the
  actual process that spawned it. Sysmon Event 1 records both the parent PID and
  the actual calling process — cross-referencing these can detect spoofed parent
  relationships. Some behavioral EDRs specifically correlate reported parent vs actual
  parent and flag discrepancies. However, many legacy detection rules rely only on
  parent-child process tree without cross-validation.
usage_examples:
  - description: Spawn a process with spoofed parent PID (BOF variant)
    args: "execute-bof spawn.x64.o <ppid> <program_path> <args>"
  - description: Spawn cmd.exe appearing to be a child of explorer.exe
    args: "execute-bof spawn.x64.o <explorer.exe_pid> C:\\Windows\\System32\\cmd.exe"
  - description: Apollo's spawn command with PPID spoofing
    args: "Apollo: spawn <ppid> <process_path>"
opsec_notes: |
  Parent PID spoofing makes a spawned process appear to be a child of a different,
  more legitimate parent. The classic use: spawning cmd.exe or powershell.exe as if
  launched by explorer.exe (normal user shell launch) rather than by the C2 agent.
  This defeats detection rules that look for "cmd.exe spawned by [unusual_parent]".
  Apollo's native `spawn` command supports PPID spoofing directly. EDR that correlates
  reported parent with actual kernel-level parent (via EPROCESS parent links) will
  detect the spoof regardless.
gotchas: |
  PPID spoofing requires access to the spoofed parent process (OpenProcess with
  PROCESS_DUP_HANDLE) — you can't spoof arbitrary PIDs without access rights to that
  process. Processes running at higher integrity than the attacker's process can't be
  used as spoofed parents. Common mistake: spawning a high-integrity process (SYSTEM)
  as the apparent parent when the attacker is medium-integrity — this is a larger
  anomaly than just the PPID spoof. Use processes of equivalent or similar integrity.
related_ttps: [process-injection, amsi-bypass, uac-bypass]
alternatives: [apollo-spawn-with-ppid, create-process-as-system]
common_args:
  ppid:
    description: Target parent PID to spoof (should be a running, accessible process)
    typical_values: ["<explorer.exe-pid>", "<svchost.exe-pid>"]
    required: true
  program:
    description: Program to launch with the spoofed parent
    typical_values: ["C:\\\\Windows\\\\System32\\\\cmd.exe", "C:\\\\Windows\\\\System32\\\\powershell.exe"]
    required: true
last_updated: 2026-05-29
---

# Parent PID Spoofing

A process creation technique that makes a new process appear to be a child of a
specified parent process (the "spoofed parent") rather than the actual calling process.
This defeats behavioral detection rules that analyze parent-child process relationships
to flag anomalous chains (e.g. "Office spawning cmd.exe").

## The Technique

```
Without PPID spoof:
  MythicAgent.exe
    └─ cmd.exe   ← anomalous (C2 agent spawning cmd directly)

With PPID spoof (parent = explorer.exe):
  explorer.exe (4892)  ← appears as parent
    └─ cmd.exe          ← normal (explorer spawning cmd)
  MythicAgent.exe      ← actual caller (hidden from process tree view)
```

## Apollo Native Support

Apollo's `spawn` command supports PPID spoofing natively:
```
# Spawn cmd.exe appearing as child of explorer.exe:
spawn <explorer_pid> cmd.exe
```
No separate BOF or binary needed for Apollo operators.

## Good Parent PIDs to Use

| Target process | Good spoofed parent | Why |
|---------------|--------------------| ----|
| cmd.exe | explorer.exe (user) | Normal user shell spawn |
| powershell.exe | explorer.exe (user) | Normal user shell spawn |
| Any network tool | svchost.exe | Service host spawning tools is common |
| msiexec.exe | msiexec.exe chain | Update-style spawn |

## Limitations

- Parent must be accessible (same integrity level or lower than attacker)
- Kernel-level EDR can correlate EPROCESS parent chain (doesn't rely on reported PID)
- CrowdStrike Falcon detects PPID spoofing with high confidence
