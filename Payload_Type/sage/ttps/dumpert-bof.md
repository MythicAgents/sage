---
name: Dumpert (Direct Syscall LSASS Dump)
category: credential-access
subcategories: [lsass-dump, direct-syscalls, bof, minidump]
tradecraft_tags: [lsass, dump, direct-syscall, bof, minidump, credentials, outflank, athena]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/outflanknl/Dumpert
  license: GPL-3.0
  maintained: true
binary_type: bof
binary_filename: Dumpert.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  Dumpert uses direct syscalls (bypassing ntdll hooks) to open LSASS and write a
  minidump. A process opening LSASS via direct NtOpenProcess syscall (no ntdll hook
  path) is detectable by kernel-level EDR callbacks (ObRegisterCallbacks) — the
  kernel can't be bypassed from user mode. Kernel-based EDR (CrowdStrike, SentinelOne)
  will still detect LSASS access regardless of syscall method.
usage_examples:
  - description: Dump LSASS to disk using direct syscalls (BOF mode)
    args: "execute-bof Dumpert.x64.o"
  - description: Specify custom output path
    args: "execute-bof Dumpert.x64.o C:\\Windows\\Temp\\out.dmp"
opsec_notes: |
  Dumpert (Outflank) was one of the first LSASS dumpers to use direct syscalls,
  preceding nanodump. nanodump has since incorporated and expanded on Dumpert's
  technique set. For modern engagements, nanodump is generally preferred:
  - nanodump adds PssCaptureSnapshot (no direct LSASS open in some modes)
  - nanodump adds invalid minidump header (signature evasion)
  - nanodump adds more PPL bypass options
  Dumpert's value is its simplicity and Outflank's trusted maintenance.
gotchas: |
  Apollo has no BOF runner — requires Athena. Direct syscalls bypass ntdll hooks but
  NOT kernel callbacks — modern kernel-level EDR still detects LSASS dumps. The BOF
  format is compatible with Cobalt Strike and Athena's execute-bof. Output is a standard
  valid minidump (no corrupted header like nanodump's default) — parseable by any tool
  but detectable by signature-based file scanning.
related_ttps: [nanodump, nanodump-bof-expanded, kerbdump-bof, sharphandler]
alternatives: [nanodump, kerbdump-bof, sharphandler]
common_args:
  output_path:
    description: Optional output dump file path (default is current directory)
    typical_values: ["C:\\\\Windows\\\\Temp\\\\out.dmp"]
last_updated: 2026-05-29
---

# Dumpert (Direct Syscall LSASS Dump)

Outflank's direct-syscall LSASS dumper — one of the early tools implementing the
direct syscall technique for LSASS access. Dumpert opens LSASS using NtOpenProcess
with a system-call-number-resolved direct syscall (bypassing ntdll user-mode hooks)
and writes a standard minidump. Historically significant; nanodump has expanded on
this approach with additional evasion layers.

## Evasion Technique Comparison

| Tool | Direct syscall | Invalid header | PssCaptureSnapshot | Handle dup |
|------|---------------|----------------|-------------------|------------|
| Dumpert | YES | NO | NO | NO |
| nanodump | YES (via HellsGate) | YES | YES | YES (various) |
| SharpHandler | NO (handle dup instead) | NO | NO | YES |

## Typical use cases
- LSASS dump in environments where ntdll hooks are the primary EDR mechanism
- When a known-good Outflank-maintained BOF is preferred over third-party tools
- Reference implementation for understanding the direct syscall LSASS technique

## Apollo-specific note
BOF — requires Athena (`execute-bof Dumpert.x64.o`). For Apollo, the fallback is
Apollo's native `mimikatz` command or SharpKatz.
