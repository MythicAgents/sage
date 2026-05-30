---
name: nanodump (expanded BOF reference)
category: credential-access
subcategories: [lsass-dump, bof, in-process-dump, pss-capture]
tradecraft_tags: [bof, lsass, dump, credentials, indirect-syscalls, pss-snapshot, fortra, athena]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/fortra/nanodump
  license: BSD-3-Clause
  maintained: true
binary_type: bof
binary_filename: nanodump.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  nanodump uses indirect syscalls (HellsGate/HalosGate technique) to call NtOpenProcess,
  NtAllocateVirtualMemory, and NtWriteVirtualMemory without going through ntdll.dll exports
  directly — evading user-mode API hooks. PssCaptureSnapshot mode avoids direct lsass.exe
  handle acquisition (the snapshot is taken via a process cloning mechanism, then the
  clone is read). EDR with kernel-level callbacks (PsSetCreateProcessNotifyRoutine
  watching for lsass clones) or hardware breakpoints on lsass handles may still detect.
usage_examples:
  - description: Default dump — PssCaptureSnapshot to disk (least noise)
    args: "--write C:\\Windows\\Temp\\out.dmp"
  - description: In-memory dump — exfil over C2 without disk write
    args: "--inmem"
  - description: Fork mode — creates a forked clone of lsass, dumps the clone
    args: "--fork --write C:\\Windows\\Temp\\out.dmp"
  - description: Seclogon duplicate — bypass PPL via service handle
    args: "--seclogon-duplicate --write C:\\Windows\\Temp\\out.dmp"
  - description: WerFault impersonation — appear as Windows Error Reporting
    args: "--werfault --write C:\\Windows\\Temp\\out.dmp"
  - description: Banzai pipe trick — handle via pipe dup
    args: "--banzai --write C:\\Windows\\Temp\\out.dmp"
  - description: Spoof call stack to hide nanodump in thread traces
    args: "--spoof-callstack --write C:\\Windows\\Temp\\out.dmp"
opsec_notes: |
  nanodump's defining evasion properties:
  1. **Invalid minidump signature**: output file has a corrupted MZ/minidump header —
     passes signature-based detection but still parsed by mimikatz/pypykatz
  2. **Indirect syscalls**: avoids ntdll user-mode hooks placed by EDR
  3. **PssCaptureSnapshot**: no direct LSASS OpenProcess; uses Windows process snapshot API
  4. **In-memory mode (--inmem)**: zero disk writes; dump bytes sent over C2 channel

  The combination of (1) + (2) + (3) bypasses the majority of LSASS protection stacks
  that rely on user-mode API hooks and signature detection. Kernel-level callbacks are
  the primary remaining detection path.
gotchas: |
  Apollo has no BOF runner — requires Athena's execute-bof. The --inmem path requires
  C2 framework support for receiving the dump bytes in-channel (Athena supports this).
  PPL (Protected Process Light) on lsass.exe blocks default modes — use
  --seclogon-duplicate or --banzai. Credential Guard encrypts credentials in memory
  even if lsass is dumped — nanodump doesn't help against Credential Guard (the LSA
  isolated process holds the encrypted material). Parse the dump with:
    mimikatz: `sekurlsa::minidump out.dmp` then `sekurlsa::logonpasswords`
    pypykatz: `pypykatz lsa minidump out.dmp`
related_ttps: [kerbdump-bof, mimikatz, sharpkatz, bofnet, trustedsec-bofs]
alternatives: [mimikatz-sekurlsa-minidump, dumpert, lsassy]
common_args:
  --write:
    description: Write dump to this path on disk
    typical_values: ['C:\\Windows\\Temp\\out.dmp', 'C:\\Windows\\Temp\\~tmp.dmp']
  --inmem:
    description: Keep dump in agent memory; exfil over C2 (no disk write)
    typical_values: [flag-only]
  --fork:
    description: Fork lsass and dump the fork (avoids direct handle to lsass)
    typical_values: [flag-only]
  --no-snapshot:
    description: Skip PssCaptureSnapshot; direct LSASS handle (louder)
    typical_values: [flag-only]
  --seclogon-duplicate:
    description: Duplicate LSASS handle via seclogon service (PPL bypass)
    typical_values: [flag-only]
  --werfault:
    description: Impersonate WerFault.exe when opening LSASS
    typical_values: [flag-only]
  --banzai:
    description: Banzai named pipe trick for handle duplication (PPL bypass)
    typical_values: [flag-only]
  --spoof-callstack:
    description: Spoof call stack to hide nanodump in thread traces
    typical_values: [flag-only]
  --valid:
    description: Produce a valid (unmodified) minidump header (loses signature evasion)
    typical_values: [flag-only]
  --pid:
    description: Manually specify LSASS PID instead of name resolution
    typical_values: ["<pid>"]
last_updated: 2026-05-29
---

# nanodump (expanded BOF reference)

This file expands on `nanodump.md` with full BOF-specific context: the indirect
syscall implementation, evasion layers, and how nanodump's approach compares to
other LSASS dump techniques in terms of detection surface.

> See `nanodump.md` for the canonical schema entry. This file is the technical depth
> supplement used by `get_ttp_full_reference("nanodump-bof-expanded")`.

## Indirect Syscall Implementation (HellsGate / HalosGate)

nanodump resolves Windows syscall numbers at runtime by parsing ntdll.dll's
export table, then executes the syscall directly via inline assembly — bypassing
any user-mode API hooks that EDR has installed in ntdll:

```
Standard EDR-hooked path:
  NtOpenProcess() → jmp [EDR hook] → EDR inspection → ntdll code → kernel

nanodump indirect syscall path:
  Parse ntdll → find syscall number → execute syscall directly from agent memory
  (EDR hook in ntdll is never called)
```

This defeats user-mode hook-based EDR detection. Kernel-mode callbacks on
process/thread operations (PsSetCreateProcessNotifyRoutine, ObRegisterCallbacks)
are NOT bypassed by this technique.

## PssCaptureSnapshot vs Direct Handle

| Mode | API | Handle source | EDR detection path |
|------|-----|--------------|------------------|
| Default (snapshot) | PssCaptureSnapshot | OS-mediated process snapshot | Lower — no direct LSASS OpenProcess |
| --no-snapshot | NtOpenProcess(lsass) | Direct handle to lsass | Higher — direct OpenProcess |
| --fork | ZwCreateProcessEx(lsass) | Fork of lsass | Medium — process clone event |
| --werfault | Token impersonation of WerFault | Indirect via WER | Low — WerFault normally accesses lsass |

## PPL (Protected Process Light) Bypass Modes

PPL protects lsass on Windows 10 1607+ (with Credential Guard or PPL policy set):

| Mode | PPL Bypass? | Mechanism |
|------|-------------|----------|
| Default (snapshot) | No | — |
| --seclogon-duplicate | Yes | Duplicate handle via seclogon service handle |
| --banzai | Yes | Named pipe handle inheritance trick |
| --fork | No | Can't fork a PPL process without PPL level |

To check PPL status:
```
Seatbelt.exe LSASettings   (shows LSA RunAsPPL registry value)
Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\Lsa -Name RunAsPPL
```

## Credential Guard Interaction

If Windows Defender Credential Guard is enabled, credentials in LSASS are stored in
an isolated VM (VSM/VTL1). nanodump CAN dump the lsass process but the credential
material within the dump will be opaque (Credential Guard virtualizes the credential
storage). There is no user-mode bypass for Credential Guard.

Check with Seatbelt: `Seatbelt.exe CredGuard`

## Post-Dump Parsing

```python
# pypykatz (Linux):
pypykatz lsa minidump out.dmp

# mimikatz (Windows — run from another agent or local):
sekurlsa::minidump out.dmp
sekurlsa::logonpasswords
```

If the dump has the corrupted header (default, without --valid), use mimikatz or
pypykatz — both handle the malformed header. `procdump -ma lsass.exe` output
(valid header) needs `--valid` mode equivalent for compatibility.
