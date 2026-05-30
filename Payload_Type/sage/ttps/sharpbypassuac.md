---
name: SharpBypassUAC
category: privilege-escalation
subcategories: [uac-bypass, auto-elevate, cm-exec]
tradecraft_tags: [uac, bypass, auto-elevate, cm-execution, cmstp, sdclt, dotnet, apollo-runnable]
mitre_attack:
  - id: T1548.002
    name: Abuse Elevation Control Mechanism — Bypass User Account Control
source:
  url: https://github.com/FatRodzianko/SharpBypassUAC
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpBypassUAC.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Depends on bypass technique used. Registry-modification-based bypasses
  (fodhelper, sdclt) generate Sysmon Event 13 (registry value set in HKCU).
  The elevated process has a high-integrity token parent relationship that is
  anomalous. CMSTP-based bypass has specific Event 4697 or 7045 variations.
  EDR behavioral analytics flag high-integrity spawns from medium-integrity parents
  that didn't show a UAC consent dialog.
usage_examples:
  - description: UAC bypass via CMSTP INF file execution
    args: "SharpBypassUAC.exe -b cmstp -e 'cmd.exe /c start powershell.exe'"
  - description: UAC bypass via fodhelper.exe registry technique
    args: "SharpBypassUAC.exe -b fodhelper -e 'cmd.exe /c net localgroup administrators attacker /add'"
  - description: UAC bypass via sdclt.exe
    args: "SharpBypassUAC.exe -b sdclt -e 'C:\\Windows\\System32\\cmd.exe'"
  - description: List available bypass techniques
    args: "SharpBypassUAC.exe -list"
opsec_notes: |
  SharpBypassUAC implements several UAC bypass techniques as a .NET assembly —
  Apollo-runnable via inline_assembly. The key advantage over manual registry
  manipulation: the bypass is self-cleaning (removes the HKCU registry key it
  creates). CMSTP bypass is less commonly seen than fodhelper and may have less
  detection coverage, but it's patched on Windows 11. Always test the specific
  bypass against the target Windows version before use.
gotchas: |
  Many classic UAC bypasses are patched on Windows 11 22H2+. Use SharpUp's UAC check
  to determine the current UAC level before attempting. The CMSTP technique requires
  cmstp.exe to be present (Windows feature, usually available). Self-cleaning registry
  writes means artifacts are transient, but the elevated process spawn is still
  detectable behaviorally.
related_ttps: [sharpup, uac-bypass, godpotato, runascs]
alternatives: [uac-bypass-manual, akagi, runascs-bypass-uac]
common_args:
  -b:
    description: Bypass technique to use
    typical_values: [cmstp, fodhelper, sdclt, dikeyboard]
    required: true
  -e:
    description: Command to execute with elevated privileges
    typical_values: ["cmd.exe /c net localgroup administrators attacker /add", "C:\\\\Windows\\\\System32\\\\cmd.exe"]
    required: true
  -list:
    description: List available bypass techniques
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpBypassUAC

A .NET assembly that implements multiple UAC bypass techniques with self-cleanup of
registry artifacts. Runnable via Apollo's inline_assembly. Provides a menu of bypass
techniques (CMSTP, fodhelper, sdclt, others) with automatic HKCU key cleanup after
the elevated process is spawned.

## Typical use cases
- Elevate from medium-integrity admin token to high-integrity without UAC prompt
- Self-cleaning bypass (less forensic artifact than manual techniques)
- Multi-technique fallback when one bypass is patched

## How Sage uses this
When SharpUp identifies an admin account running at medium-integrity and UAC bypass
is needed before privilege escalation or domain operations, SharpBypassUAC provides
the elevation step. Sage checks UAC level via Seatbelt first, then selects an appropriate
bypass technique based on the Windows version.

## Technique compatibility

| Technique | Windows 10 | Windows 11 21H2 | Windows 11 22H2+ |
|-----------|-----------|-----------------|-----------------|
| CMSTP | Works | Works | Patched |
| fodhelper | Works | Works (most builds) | Some builds patched |
| sdclt | Works | Works (most builds) | Some builds patched |
| dikeyboard | Works (some) | Less reliable | — |

Always test in lab against target OS build before operational use.
