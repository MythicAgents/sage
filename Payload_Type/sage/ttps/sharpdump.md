---
name: SharpDump
category: credential-access
subcategories: [lsass-dump, minidump, dotnet-lsass]
tradecraft_tags: [lsass, dump, minidump, dotnet, credentials, apollo-runnable, comsvcs]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/GhostPack/SharpDump
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: SharpDump.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: local-admin
network_required: false
detection_signal: |
  SharpDump calls MiniDumpWriteDump via a COM object (comsvcs.dll) invoked through
  reflection — the same technique as `rundll32 comsvcs.dll,MiniDump`. This generates
  a Sysmon Event 10 (ProcessAccess on lsass.exe) with comsvcs.dll in the call stack.
  CrowdStrike and Defender for Endpoint detect the comsvcs.dll LSASS dump pattern
  with high confidence. The dump file is a valid minidump parseable by mimikatz.
usage_examples:
  - description: Dump LSASS to the default location (encrypted)
    args: "SharpDump.exe"
  - description: Dump to a specific path
    args: "SharpDump.exe C:\\Windows\\Temp\\dump.bin"
  - description: Specify a custom process to dump
    args: "SharpDump.exe -pid <lsass_pid>"
opsec_notes: |
  SharpDump uses comsvcs.dll MiniDump — a well-known detection pattern. Modern EDR
  (CrowdStrike, Defender) blocks this with high confidence. Prefer nanodump (BOF,
  indirect syscalls, invalid header) for modern environments. SharpDump is documented
  for completeness and for lab/testing scenarios where EDR bypass isn't required.
  The GhostPack version encrypts the dump output to avoid signature detection on
  the output file.
gotchas: |
  comsvcs.dll LSASS dump is one of the most-detected patterns in red teaming. Use
  nanodump via Athena instead for OPSEC-sensitive operations. SharpDump's encryption
  of the output helps avoid signature-based detection of the dump file itself, but
  the dump ACT is still highly detectable. Requires local admin to access LSASS.
  Output is gzip-compressed and requires `SharpDump.exe --decompress` to parse.
related_ttps: [nanodump, mimikatz, sharpkatz, nanodump-bof-expanded]
alternatives: [nanodump, mimikatz-sekurlsa-minidump]
common_args:
  default:
    description: No args — dumps LSASS to %TEMP%\debug.out (gzip+encrypted)
    typical_values: [flag-only]
  -pid:
    description: Manually specify LSASS PID
    typical_values: ["<lsass-pid>"]
  path:
    description: Output file path (optional)
    typical_values: ["C:\\\\Windows\\\\Temp\\\\dump.bin"]
last_updated: 2026-05-29
---

# SharpDump

GhostPack's .NET LSASS dumper. Uses comsvcs.dll's MiniDump function (invoked via
COM reflection) to write an encrypted LSASS minidump. The encryption avoids AV/EDR
scanning of the dump file on disk; the dump operation itself uses the well-known
comsvcs.dll pattern which is highly signatured.

## Typical use cases
- Quick LSASS dump in lab environments where EDR bypass isn't needed
- Testing and development reference for LSASS dump techniques

## How Sage uses this
SharpDump is the least-preferred LSASS dump option in Sage's toolkit. Preference order:
1. **nanodump (BOF via Athena)** — indirect syscalls, invalid header, in-process
2. **Apollo native mimikatz** — embedded Mimikatz with better evasion than raw tool
3. **SharpKatz** — .NET Mimikatz port, less string-signatured than Mimikatz
4. **SharpDump** — comsvcs.dll pattern, highly detected; lab/testing only

## Output
Encrypted, gzip-compressed minidump at %TEMP%\debug.out. Use `SharpDump.exe --decompress`
on the output file, then parse with mimikatz or pypykatz.
