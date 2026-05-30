---
name: Shadow Copy NTDS.dit Extraction
category: credential-access
subcategories: [ntds-extraction, shadow-copy, domain-credential-dump]
tradecraft_tags: [ntds, shadow-copy, vss, domain-hashes, dc-access, built-in]
mitre_attack:
  - id: T1003.003
    name: OS Credential Dumping — NTDS
source:
  url: https://docs.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/cc755318(v=ws.11)
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-admin
network_required: false
detection_signal: |
  Volume Shadow Copy Service (VSS) usage on DCs generates Event 7036 (VSS Service
  state changes), Event 98 (VSS Provider registration), and file access events in
  Windows Security log for ntds.dit file access. Shadow copy creation generates
  Event 8194 in System log. Sysmon Event 9 (RawAccessRead) for ntds.dit direct access.
  EDR with file system monitoring catches ntds.dit reads.
usage_examples:
  - description: Create a shadow copy and copy ntds.dit (using built-in tools)
    args: "vssadmin create shadow /for=C: && copy \\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\Windows\\NTDS\\ntds.dit C:\\Windows\\Temp\\ntds.dit"
  - description: Copy SYSTEM hive for NTDS decryption
    args: "reg save HKLM\\SYSTEM C:\\Windows\\Temp\\SYSTEM"
  - description: Using ntdsutil for authorized AD backup snapshot
    args: "ntdsutil 'ac in ntds' ifm 'create full C:\\Temp\\ifm' q q"
  - description: Using secretsdump.py to parse offline
    args: "secretsdump.py -ntds C:\\Temp\\ntds.dit -system C:\\Temp\\SYSTEM LOCAL"
opsec_notes: |
  Direct ntds.dit access on a DC is one of the highest-signal operations in Windows
  — virtually every EDR and SIEM has detection rules for this. VSS creation is logged.
  Prefer network-based DCSync (via Apollo's native `dcsync` command or Mimikatz
  `lsadump::dcsync`) which generates less on-host forensic evidence than copying ntds.dit.
  The Shadow Copy approach leaves forensic artifacts (the shadow copy itself, file copies)
  that persist after the engagement.
gotchas: |
  This is a TECHNIQUE using built-in Windows tools (vssadmin, copy, reg save).
  No binary upload required. However, it is VERY noisy and leaves artifacts.
  DCSync is strongly preferred over NTDS.dit extraction in most scenarios.
  ntds.dit extraction is most useful for offline/lab analysis or when DCSync is
  not available (no network access to DC for replication). The SYSTEM hive is
  required alongside ntds.dit for decryption — both must be captured.
related_ttps: [impacket-secretsdump, mimikatz, sharpdpapi]
alternatives: [dcsync-via-apollo, mimikatz-dcsync]
common_args: {}
last_updated: 2026-05-29
---

# Shadow Copy NTDS.dit Extraction

The technique of using Windows Volume Shadow Copy Service (VSS) to create a snapshot
of the DC's volume and copy `ntds.dit` (the AD database containing all domain hashes)
from the snapshot. Combined with the SYSTEM registry hive, the offline database can
be parsed with `secretsdump.py` to extract all domain credentials.

## Typical use cases
- Extract the complete domain hash database from a compromised DC
- Offline hash analysis when DCSync is unavailable
- Full domain compromise credential capture for forensic evidence / scope confirmation

## How Sage uses this
Shadow Copy NTDS extraction is a last-resort or high-impact operation. Sage strongly
prefers DCSync (Apollo native `dcsync` command) which is:
- Lower-noise (no VSS creation, no ntds.dit file copy)
- Just as effective (provides the same hashes)
- No on-host artifacts

This technique is documented for completeness. Use Apollo's `dcsync` instead.

## Apollo-specific note
Uses only built-in Windows tools (vssadmin, copy, reg save) — no binary upload.
Execute via Apollo's shell command. Parse the resulting files with secretsdump.py
on attacker infrastructure.

## DC-only technique
ntds.dit only exists on domain controllers. This technique requires SYSTEM or
DA-level access on a DC.
