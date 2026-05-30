---
name: KeeThief
category: credential-access
subcategories: [keepass, password-manager, master-key-theft, memory-extraction]
tradecraft_tags: [keepass, password-manager, master-password, memory, dotnet, ghostpack, apollo-runnable]
mitre_attack:
  - id: T1555
    name: Credentials from Password Stores
source:
  url: https://github.com/GhostPack/KeeThief
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: KeeThief.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  KeeThief reads KeePass 2.X process memory to extract the master key material —
  this is an OpenProcess + ReadProcessMemory operation targeting KeePass.exe.
  Sysmon Event 10 (ProcessAccess with KeePass.exe as target) is the primary signal.
  Behavioral EDR detects cross-process memory reads targeting password manager processes
  specifically — high-priority detection for most AV/EDR vendors.
usage_examples:
  - description: Extract KeePass master key from running KeePass process
    args: "KeeThief.exe"
  - description: Target a specific KeePass process ID
    args: "KeeThief.exe -pid <keepass_pid>"
  - description: Get keylog module for KeePass master password capture
    args: "KeeThief.exe -keylog"
opsec_notes: |
  KeeThief reads KeePass.exe process memory to extract the CompositeKey (the master
  key material) after it has been unsealed from the Windows Data Protection API (DPAPI).
  This avoids needing the master password itself — KeePass has already decrypted it
  when the database is unlocked. Detection is via cross-process memory read on
  KeePass.exe, which most EDR vendors specifically watch for password manager protection.
  Time the operation: KeePass must be RUNNING and the database must be UNLOCKED.
gotchas: |
  KeePass must be running with the database unlocked for KeeThief to work. If the database
  is locked (screensaver, timeout), the key is not in memory. KeeThief requires that the
  current user has access to the target KeePass process (same user context or SYSTEM).
  After extracting the master key, use it with KeePass's command-line interface or a KDB4
  parser (kpcli, keepassxc-cli) to access the password database. KeeThief works against
  KeePass 2.X specifically; KeePass 1.X and KeePassXC have different memory layouts.
related_ttps: [sharpdpapi, credential-hunting-checklist, sharpdpapi, seatbelt]
alternatives: [keepass-memory-dump, manual-memory-scan]
common_args:
  -pid:
    description: Target KeePass process PID (defaults to auto-discover)
    typical_values: ["<keepass_pid>"]
  -keylog:
    description: Install keylogger hook for master password capture on next database open
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# KeeThief

GhostPack's KeePass 2.X master key extractor. When KeePass is running with an unlocked
database, the master key material (CompositeKey) exists in process memory in a decrypted
state. KeeThief reads this memory to extract the key, allowing the operator to decrypt
the KeePass database file offline — gaining access to all stored credentials without
knowing the master password.

## Why KeePass is High-Value

Organizations frequently use KeePass to store:
- Domain administrator passwords
- Service account credentials
- VPN and network device passwords
- Cloud console credentials (AWS, Azure, GCP)
- Database connection strings

Compromising a KeePass database with elevated passwords is often more valuable than
LSASS dumping — it may contain credentials that haven't been entered recently and
therefore aren't in LSASS cache.

## KeeThief Workflow

```
1. Find KeePass processes:
   Apollo shell: tasklist | findstr KeePass

2. Verify database is unlocked (not at master password prompt):
   Apollo screenshot (Athena) or ask via user context

3. Extract master key:
   KeeThief.exe
   → Output: CompositeKey bytes + optional master password if keylogging is set

4. Locate the .kdbx database file:
   Apollo shell: dir C:\Users\*\.kdbx /s 2>/dev/null
   Seatbelt PowerShellHistory (may reveal path from previous keepass-cli usage)

5. Download the .kdbx file via Apollo download command

6. Open offline with extracted key:
   keepassxc-cli open database.kdbx (with extracted composite key)
```

## Keylog Mode

If the database is locked and you need to wait for the user to re-unlock:
```
KeeThief.exe -keylog
# Installs a hook in KeePass for next password entry
# Next time the user types their master password, it's captured
# Clean up: the hook is removed after next unlock or process restart
```

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible. The ProcessAccess on KeePass.exe
is detectable; run quickly and remove the tool after use.
