---
name: Process Injection Techniques
category: defense-evasion
subcategories: [shellcode-injection, process-hollow, dll-injection, reflective-load]
tradecraft_tags: [injection, process-hollow, shellcode, dll, reflective, migration, parent-spoof]
mitre_attack:
  - id: T1055
    name: Process Injection
  - id: T1055.001
    name: Process Injection — Dynamic-link Library Injection
  - id: T1055.012
    name: Process Injection — Process Hollowing
source:
  url: https://attack.mitre.org/techniques/T1055/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  OpenProcess + VirtualAllocEx + WriteProcessMemory + CreateRemoteThread (classic
  injection) is the most-signatured pattern in Windows history. Modern EDR uses kernel
  callbacks (ObRegisterCallbacks, PsSetCreateThreadNotifyRoutine) to detect injection
  at the kernel level. Process hollowing creates a suspended process with mismatched
  disk/memory content (Sysmon Event 8 — CreateRemoteThread, Event 10 — ProcessAccess).
usage_examples:
  - description: Apollo shinject — inject shellcode into remote process
    args: "Apollo: shinject PID shellcode_uuid"
  - description: Apollo inject — inject assembly into remote process
    args: "Apollo: assembly_inject PID assembly_uuid"
  - description: Process migration — move the agent to a more stable process
    args: "Apollo: migrate PID"
  - description: Spawn and inject — create a sacrificial process and inject shellcode
    args: "Apollo: spawn sacrificial_process shellcode_uuid"
opsec_notes: |
  Apollo provides native injection commands (shinject, assembly_inject, migrate, spawn)
  that are integrated with Apollo's execution context and evasion. For Apollo operators,
  these native commands are always preferred over uploading a standalone injection utility.
  Key OPSEC rules:
  1. Inject into "boring" processes (svchost, explorer, spoolsv) not conspicuous ones
  2. Parent process spoofing reduces behavioral anomaly detection
  3. Injecting into a process that already does network activity hides C2 traffic
gotchas: |
  This is a TECHNIQUE REFERENCE — Apollo provides these capabilities natively. Key
  injection considerations for Apollo:
  - shinject: inject raw shellcode into a PID; PID must be accessible
  - assembly_inject: run a .NET assembly in a remote process's AppDomain
  - migrate: move the Apollo agent into a new host process
  - The target process must have compatible bitness (x64 agent → x64 target)
  - Cross-session injection (session 0 → session 1) requires specific privileges
related_ttps: [seatbelt, sharpup, nanodump]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Process Injection Techniques

Reference for process injection concepts as they apply to Apollo-based operations.
Apollo provides native injection primitives — operators do not need to upload standalone
injection utilities for basic injection tasks.

## Apollo Native Injection Commands

| Command | What it does |
|---------|-------------|
| `shinject <PID> <shellcode_uuid>` | Inject raw shellcode into a running process |
| `assembly_inject <PID> <assembly_uuid> [args]` | Load .NET assembly into a remote process |
| `migrate <PID>` | Move the Apollo agent into the specified process |
| `spawn <sacrificial_exe> <shellcode_uuid>` | Spawn a new process and inject shellcode |

## Injection Target Selection

Choose injection targets based on:
1. **Stability**: Long-lived processes (svchost.exe, explorer.exe) vs short-lived ones
2. **Legitimacy**: Processes that make network connections (for C2 concealment)
3. **Privileges**: Target must be accessible from current privilege level
4. **Bitness**: x64 agent must inject into x64 targets (generally)

## Parent Process Spoofing

Apollo supports parent process spoofing via spawn — setting the parent PID to a
legitimate process makes the spawned process look like it was launched by that parent:
```
Apollo: spawn <ppid_spoof> <child_exe> <shellcode>
```
This reduces behavioral anomaly (cmd.exe spawned by Word is suspicious; cmd.exe
spawned by svchost is normal).

## Injection Methods (Reference)

| Method | Apollo support | Detection signal |
|--------|---------------|-----------------|
| Remote thread injection | shinject | High (classic pattern) |
| AppDomain injection (.NET) | assembly_inject | Medium |
| Process hollowing | N/A (native in Apollo loader) | Medium |
| Module stomping | N/A | Low |
| Early bird injection | N/A (spawn variant) | Medium |
| Reflective DLL loading | N/A (used internally) | Medium |

## Process Migration OPSEC

Migrate into processes that:
- Already communicate over HTTP/HTTPS (browsers, update processes)
- Are long-lived and stable (not user-interactive)
- Match the privilege level needed (SYSTEM processes for SYSTEM ops)
- Are native to the system (not installed software that might be removed)

Good targets: `svchost.exe -k NetworkService`, `SearchIndexer.exe`, `spoolsv.exe`
Poor targets: user applications, short-lived helper processes, security software
