---
name: SharpKatz
category: credential-access
subcategories: [lsass-dump, wdigest, logonpasswords]
tradecraft_tags: [credentials, ntlm, wdigest, lsass, dotnet-mimikatz]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/b4rtik/SharpKatz
  license: MIT
  maintained: false
binary_type: .net-assembly
binary_filename: SharpKatz.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  SharpKatz accesses LSASS memory similarly to Mimikatz; EDR kernel callbacks on
  LSASS handle creation will catch it. Sysmon event 10 (ProcessAccess on lsass.exe)
  is the primary telemetry. Behavioral EDRs (CrowdStrike, SentinelOne) detect LSASS
  memory reads from .NET processes. String-based detection is less common than for
  Mimikatz but some rules include b4rtik/SharpKatz filenames.
usage_examples:
  - description: Dump all logon credentials (NTLM hashes + cleartext if WDigest enabled)
    args: "--Command logonpasswords"
  - description: Dump WDigest cleartext passwords
    args: "--Command wdigest"
  - description: Triage logon sessions (lighter-weight list)
    args: "--Command triage"
  - description: DCSync for a specific account
    args: "--Command dcsync --User krbtgt --Domain north.sevenkingdoms.local --DomainController DC01.north.sevenkingdoms.local"
opsec_notes: |
  SharpKatz's primary advantage over raw Mimikatz is that it is a .NET assembly
  (runnable via inline_assembly in Apollo) with fewer string-based signatures.
  However, the underlying LSASS access pattern is identical. Prefer Apollo's native
  `mimikatz` command for LSASS operations; use SharpKatz only when the native command
  is insufficient or the caller needs .NET-assembly delivery specifically.
gotchas: |
  SharpKatz is not actively maintained (last commit ~2020). Compatibility with newer
  Windows versions (11 23H2+, Server 2022) may be limited — test in target environment.
  DCSync functionality requires the same AD replication rights as Mimikatz (GetChangesAll).
  WDigest cleartext is only available when UseLogonCredential=1 (disabled by default on
  Windows 8.1+ and Server 2012 R2+). If only NTLM hashes are needed, `logonpasswords` works
  regardless of WDigest state.
related_ttps: [mimikatz, nanodump, rubeus, sharpdpapi]
alternatives: [mimikatz, nanodump, lsassy]
common_args:
  --Command:
    name: --Command
    description: Sub-command to execute
    typical_values: [logonpasswords, wdigest, triage, dcsync]
    required: true
  --User:
    description: Target user for dcsync
    typical_values: [krbtgt, administrator]
  --Domain:
    description: Target domain FQDN for dcsync
    typical_values: [north.sevenkingdoms.local, essos.local]
  --DomainController:
    description: Specific DC to replicate from (dcsync)
    typical_values: ["DC01.north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# SharpKatz

A C#/.NET reimplementation of selected Mimikatz credential-extraction modules,
designed for inline-assembly delivery in frameworks like Apollo. SharpKatz implements
`logonpasswords`, `wdigest`, `triage`, and `dcsync` from Mimikatz, providing the most
common credential-harvesting operations in a .NET assembly that doesn't carry the full
Mimikatz signature load.

## Typical use cases
- Extract NTLM hashes from LSASS when Apollo's native `mimikatz` is flagged
- Dump WDigest cleartext credentials on older/misconfigured Windows versions
- DCSync a specific domain account's hash without uploading full Mimikatz binary
- Triage logon sessions to identify which users are active on a host

## How Sage uses this
SharpKatz is the fallback LSASS credential harvester when Apollo's embedded Mimikatz
is insufficient or unavailable. Sage prefers Apollo's native `mimikatz` command first;
SharpKatz is the secondary option when specifically requesting .NET-assembly delivery.
For the Trust Walker chain, SharpKatz's `dcsync` is an alternative to Apollo's `dcsync`
native command when the native is unavailable or the operator wants assembly-based delivery.

## Output
Text output formatted similarly to Mimikatz — per-session blocks with authentication
package, domain, username, NTLM hash, SHA1, and cleartext (if available). DCSync
output includes RC4 (NTLM), AES128, and AES256 keys.

## OPSEC considerations
SharpKatz has fewer string-based signatures than the full Mimikatz binary, but the
behavioral pattern (LSASS handle + memory read from a .NET process) is identical to
Mimikatz and caught by the same EDR kernel callbacks. For truly stealthy LSASS access,
prefer nanodump (BOF, indirect syscalls, handle duplication) over SharpKatz.

## Full Reference

> Captured against SharpKatz v1.0.0, 2026-05-29. Source: https://github.com/b4rtik/SharpKatz README.
> Note: project is not actively maintained; behavior reflects last commit.

### Available commands

| Command | Description |
|---------|-------------|
| `logonpasswords` | Dump all logon session credential material (NTLM + cleartext if WDigest) |
| `wdigest` | Dump WDigest cleartext credentials |
| `triage` | List logon sessions without dumping full credentials |
| `dcsync` | Simulate DC replication to extract credential material |

### DCSync argument listing

| Arg | Description |
|-----|-------------|
| `--User X` | Specific user to sync (e.g. krbtgt, administrator) |
| `--Domain X` | Target domain FQDN |
| `--DomainController X` | Specific DC FQDN to replicate from |

### Source for this reference

- https://github.com/b4rtik/SharpKatz (README)
- Version: v1.0.0 (last commit ~2020; may have compatibility issues with newer Windows)
