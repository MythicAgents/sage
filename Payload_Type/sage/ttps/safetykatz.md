---
name: SafetyKatz
category: credential-access
subcategories: [lsass-dump, mimikatz-loader, pe-loader, static-av-bypass]
tradecraft_tags: [mimikatz, lsass, credentials, pe-loader, dotnet, ghostpack, apollo-runnable]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/GhostPack/SafetyKatz
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: SafetyKatz.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  SafetyKatz loads Mimikatz as a .NET PE (base64-encoded, slightly modified) into memory
  using a custom reflective PE loader. The LSASS access pattern is the same as Mimikatz —
  Sysmon Event 10 (ProcessAccess on lsass.exe), kernel-level EDR callbacks detect LSASS
  handle acquisition. The primary detection advantage over raw Mimikatz.exe is evading
  static signature scanning of the binary on disk or during upload.
usage_examples:
  - description: Run Mimikatz sekurlsa::logonpasswords in-memory via SafetyKatz
    args: "SafetyKatz.exe"
  - description: Run specific Mimikatz commands
    args: "SafetyKatz.exe 'sekurlsa::logonpasswords' 'exit'"
  - description: Run multiple Mimikatz commands
    args: "SafetyKatz.exe 'privilege::debug' 'sekurlsa::logonpasswords' 'lsadump::dcsync /user:krbtgt' 'exit'"
  - description: Via Apollo inline_assembly (preferred path)
    args: "(Apollo) inline_assembly SafetyKatz.exe args='sekurlsa::logonpasswords exit'"
opsec_notes: |
  SafetyKatz is harmj0y's refinement over just uploading mimikatz.exe. It loads a
  slightly-modified Mimikatz binary via a .NET PE loader, changing the static hash.
  Key OPSEC considerations:
  1. Static signature evasion: SafetyKatz's .exe binary has a different hash from
     standard Mimikatz — bypasses file-hash-based signature scanning
  2. The modified Mimikatz it loads has string changes to bypass string-based AMSI/YARA
  3. The actual LSASS operations (handle acquisition, memory read) still generate
     the same kernel-level events as any Mimikatz execution
  Apollo's native `mimikatz` command likely uses a similar or better embedded approach —
  prefer Apollo native. SafetyKatz is the standalone fallback.
gotchas: |
  SafetyKatz is a DELIVERY mechanism for Mimikatz, not a bypass for the underlying
  LSASS access detection. Behavioral EDR (CrowdStrike, SentinelOne) catches the LSASS
  access regardless of how Mimikatz was loaded. The advantage is specifically against:
  - AV that signature-scans the uploaded binary file
  - AMSI that string-matches known Mimikatz patterns
  Compare: nanodump BOF (Athena) is more effective against behavioral EDR because it
  uses indirect syscalls + PssCaptureSnapshot, avoiding the direct LSASS handle pattern.
  SafetyKatz vs nanodump: SafetyKatz gives Mimikatz's full feature set; nanodump is
  stealthier but only produces a dump file that still needs parsing.
related_ttps: [mimikatz, sharpkatz, nanodump, nanodump-bof-expanded, sharpdump]
alternatives: [apollo-native-mimikatz, nanodump, sharpkatz]
common_args:
  commands:
    description: Mimikatz commands to execute (space-separated, last must be 'exit')
    typical_values: ["'sekurlsa::logonpasswords' 'exit'",
                     "'privilege::debug' 'sekurlsa::logonpasswords' 'lsadump::dcsync /user:krbtgt' 'exit'"]
last_updated: 2026-05-29
---

# SafetyKatz

GhostPack's safer Mimikatz delivery mechanism. SafetyKatz encodes a slightly-modified
Mimikatz binary and loads it at runtime via a custom .NET PE Loader — the resulting
assembly has no direct Mimikatz byte signatures on disk, bypassing static AV scanning
while retaining Mimikatz's complete feature set.

## What SafetyKatz Does Differently

```
Raw mimikatz.exe:
  - Well-known hash → blocked by hash-based AV
  - Contains literal Mimikatz strings → AMSI/YARA matches
  - .exe PE directly inspectable on disk

SafetyKatz:
  - Custom .NET assembly hash (different from any known signature)
  - Modified Mimikatz (string substitutions break known YARA rules)
  - Mimikatz bytes loaded at runtime via PE loader (not stored on disk unmodified)
  - Still uses same LSASS access patterns (behavior identical)
```

## Comparison with Apollo Native Mimikatz

Apollo ships an embedded Mimikatz via its native `mimikatz` command. That embedded
variant is likely more up-to-date and better integrated than SafetyKatz. Recommendation:

1. **Apollo `mimikatz` native command** — first choice (no binary upload needed)
2. **SafetyKatz inline_assembly** — fallback when Apollo native is insufficient
3. **SharpKatz** — .NET Mimikatz port (fewer features but less signature)
4. **nanodump** (Athena BOF) — stealthiest for LSASS dump specifically

## Full Command Reference

SafetyKatz passes commands directly to Mimikatz's parser:

```
SafetyKatz.exe 'privilege::debug' 'sekurlsa::logonpasswords' 'exit'
SafetyKatz.exe 'lsadump::dcsync /domain:DOMAIN /user:krbtgt' 'exit'
SafetyKatz.exe 'sekurlsa::minidump out.dmp' 'sekurlsa::logonpasswords' 'exit'
SafetyKatz.exe 'kerberos::list /export' 'exit'
```

All Mimikatz commands (see mimikatz.md) are available through SafetyKatz.
