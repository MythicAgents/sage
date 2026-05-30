---
name: Inline-Execute-PE BOF
category: defense-evasion
subcategories: [pe-in-memory, bof-execution, reflective-loading]
tradecraft_tags: [bof, pe, in-memory, reflective, execution, evasion, native-exe, athena]
mitre_attack:
  - id: T1620
    name: Reflective Code Loading
source:
  url: https://github.com/kyleavery/inject-assembly
  license: MIT
  maintained: true
binary_type: bof
binary_filename: InlineExecute-Assembly.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Running a native EXE inside a BOF context involves reflective PE loading — the PE
  headers and entrypoint are resolved in memory without disk writes. EDR behavioral
  detection for unusual memory sections (PE with MZ header in non-module memory),
  API call patterns matching PE loader code, and VirtualAlloc + VirtualProtect
  sequences. This is more detectable than true BOF execution but less detectable
  than spawning the EXE as a child process.
usage_examples:
  - description: Execute a native EXE in-memory via BOF (avoid child process creation)
    args: "execute-bof InlineExecute-Assembly.x64.o <base64-pe> <args>"
  - description: Run mimikatz.exe in-memory without disk touch
    args: "execute-bof InlineExecute-Assembly.x64.o <mimikatz-b64> 'sekurlsa::logonpasswords exit'"
  - description: Run any .NET assembly that doesn't load via BOF.NET
    args: "execute-bof inject-assembly.x64.o <assembly-b64> <args>"
opsec_notes: |
  Inline PE execution avoids child process creation (no Event 4688 for the tool) but
  the reflective loading itself is detectable via memory analysis. EDR with memory
  scanning (CrowdStrike, SentinelOne) may detect the PE headers in memory and attribute
  them to the agent process. For .NET assemblies, BOF.NET or Athena's inline_assembly
  are preferred. Inline-Execute-PE is most valuable for native EXEs that have no
  .NET equivalent.
gotchas: |
  Apollo has no BOF runner — requires Athena. Native EXEs that call ExitProcess() will
  kill the C2 agent if not patched first (some implementations patch ExitProcess to a
  safe return). EXEs that use console I/O may behave unexpectedly. Large EXEs increase
  memory footprint and scan surface. The technique works best for tools where the
  child-process signal is the primary detection vector.
related_ttps: [bofnet, trustedsec-bofs, outflank-remote-ops-bofs, nanodump]
alternatives: [bofnet-execute-assembly, apollo-inline-assembly, shinject-shellcode-wrapper]
common_args:
  pe_base64:
    description: Base64-encoded PE binary bytes
    typical_values: ["<mimikatz-b64>", "<any-native-exe-b64>"]
    required: true
  args:
    description: Command-line arguments for the PE
    typical_values: ["sekurlsa::logonpasswords exit", "whoami"]
last_updated: 2026-05-29
---

# Inline-Execute-PE BOF

A BOF framework for running native Windows executables (PE files) entirely in memory
inside the C2 agent's process, without writing to disk or creating a child process.
Addresses the gap between BOF (no child process) and inline_assembly (.NET only) —
allowing native EXEs like Mimikatz to run with the same "no child process" property
as a BOF.

## The Execution Tiers

| Method | Process created? | Disk write? | Detectable by |
|--------|-----------------|-------------|--------------|
| Shell command (normal) | YES (child) | YES | Process creation + path |
| Spawn + inject shellcode | YES (child) | Optional | Process creation |
| inline_assembly (Apollo) | NO | NO | AppDomain creation, CLR load |
| BOF (native) | NO | NO | In-process API calls |
| Inline-Execute-PE BOF | NO | NO | Memory scan (PE headers in agent) |

## Typical use cases
- Run Mimikatz (native EXE) in-memory without spawning mimikatz.exe process
- Execute any native-EXE tool where child-process creation is the primary detection vector
- Fill the native-EXE gap in BOF-capable agents (Athena can run BOFs but not native EXEs directly)

## How Sage uses this
With Athena, Inline-Execute-PE handles the native EXE gap — tools like Mimikatz.exe
that are native PEs can be run in-process. For .NET tools, BOF.NET or execute-bof
via BOF_ExecuteAssembly is preferred.

## Apollo-specific note
Apollo has no BOF runner. For native EXE execution from Apollo, options are limited:
- Spawn to disk + execute (noisy)
- Shellcode wrapper (requires separate shellcode conversion)
- Switch to Athena for in-memory native EXE execution
