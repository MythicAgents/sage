---
name: SharPersist
category: persistence
subcategories: [registry-persistence, scheduled-task, startup-folder, com-hijack]
tradecraft_tags: [persistence, registry, scheduled-task, startup, wmi, com, ghostpack-adjacent]
mitre_attack:
  - id: T1547.001
    name: Boot or Logon Autostart Execution — Registry Run Keys / Startup Folder
  - id: T1053.005
    name: Scheduled Task/Job — Scheduled Task
source:
  url: https://github.com/mandiant/SharPersist
  license: Apache-2.0
  maintained: true
binary_type: .net-assembly
binary_filename: SharPersist.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  Each persistence method generates its own telemetry: registry Run key writes generate
  Event 13 (registry value set) in Sysmon; scheduled task creation generates Event 4698;
  startup folder writes generate file creation events. EDR behavioral signatures flag
  persistence mechanism creation from non-privileged processes. SharPersist's specific
  registry keys and task names are not signatured by name, but the patterns are.
usage_examples:
  - description: Add registry Run key persistence
    args: "-t reg -c 'cmd.exe' -a '/c start payload.exe' -k 'hkcu' -v 'WindowsUpdate' -m add"
  - description: Add scheduled task persistence (user-level)
    args: "-t schtask -c 'cmd.exe' -a '/c C:\\Users\\Public\\payload.exe' -n 'SystemHealthCheck' -m add"
  - description: Add to user startup folder
    args: "-t startupfolder -c 'C:\\Users\\Public\\payload.exe' -f 'svchost' -m add"
  - description: WMI event subscription persistence (requires admin)
    args: "-t wmi -c 'C:\\Windows\\Temp\\payload.exe' -n 'WMIPersist' -m add"
  - description: Remove a persistence entry
    args: "-t reg -k 'hkcu' -v 'WindowsUpdate' -m remove"
opsec_notes: |
  Persistence mechanisms are long-lived artifacts — they survive reboots and user
  logoffs. Each technique leaves a different forensic footprint. Registry Run keys are
  commonly hunted by IR teams (Autoruns). Scheduled tasks (Event 4698) are audited.
  WMI event subscriptions are more stealthy but require admin. For temporary campaign
  persistence where cleanup is certain, prefer less-detectable mechanisms. SharPersist
  is not stealthy by default — rename and use realistic key/task names.
gotchas: |
  Cleanup is critical — persistence entries left after an engagement are forensic evidence.
  Use `-m remove` to clean up. Registry Run keys in HKCU work at user privilege; HKLM
  requires admin. Scheduled tasks running at SYSTEM require admin to create. WMI event
  subscriptions are very persistent but also very detectable by modern forensic tools
  (Autoruns, velociraptor).
related_ttps: [seatbelt, sharpup]
alternatives: [powersploit-persistence, manual-registry-writes]
common_args:
  -t:
    name: -t
    description: Persistence technique type
    typical_values: [reg, schtask, startupfolder, wmi, tortoisesvn, keepass, hotkey]
    required: true
  -m:
    name: -m
    description: Mode — add, remove, or check
    typical_values: [add, remove, check]
    required: true
  -c:
    name: -c
    description: Command / binary to persist
    typical_values: ["cmd.exe", "C:\\\\Users\\\\Public\\\\payload.exe"]
  -a:
    name: -a
    description: Arguments for the command
    typical_values: ["/c C:\\\\payload.exe"]
  -n:
    name: -n
    description: Task or subscription name (for schtask and wmi)
    typical_values: ["SystemHealthCheck", "WindowsUpdate"]
  -k:
    name: -k
    description: Registry hive key (for reg persistence)
    typical_values: [hkcu, hklm]
  -v:
    name: -v
    description: Registry value name
    typical_values: ["WindowsUpdate", "SvcHost"]
last_updated: 2026-05-29
---

# SharPersist

Mandiant's .NET toolkit for establishing persistence via multiple Windows mechanisms.
SharPersist provides a unified interface for registry Run keys, scheduled tasks, startup
folder, WMI event subscriptions, and COM hijacking — all as a single .NET assembly
runnable via Apollo's inline_assembly. Each mechanism is paired with a `-m remove` cleanup
operation.

## Typical use cases
- Establish registry Run key persistence for re-entry after reboot
- Add a scheduled task for periodic callback
- Startup folder persistence (low-privilege, user-only)
- WMI event subscription (high-stealth, admin-required)

## How Sage uses this
Persistence is typically established after achieving the target access level for a
campaign phase. Sage may prompt the operator to confirm persistence establishment
(it's a durable artifact). The preference is lightweight mechanisms (Run key, startup
folder) at user level, with WMI reserved for admin-level high-value persistence.

## Output
Text output confirming add/remove operation and the specific registry key, task name,
or artifact created.
