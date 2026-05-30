---
name: SharpPrinter
category: persistence
subcategories: [printer-persistence, print-provider, dll-load]
tradecraft_tags: [persistence, printer, dll, print-provider, lolbin-adjacent]
mitre_attack:
  - id: T1547.012
    name: Boot or Logon Autostart Execution — Print Processors
source:
  url: https://github.com/0xthirteen/SharpPrinter
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpPrinter.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  Print provider / print processor installation generates registry writes under
  HKLM\SYSTEM\CurrentControlSet\Control\Print\Monitors (or Environments\...\Print Processors).
  These registry keys are monitored by Sysmon and some EDRs. DLL load from a non-standard
  path via the print spooler service (spoolsv.exe) is detectable by behavioral EDR.
usage_examples:
  - description: Install a DLL as a print processor for persistent execution
    args: "--dll payload.dll --name LegitMonitor"
  - description: List installed print processors
    args: "--list"
  - description: Remove an installed print processor
    args: "--remove --name LegitMonitor"
opsec_notes: |
  Print provider/processor DLL loading is a SYSTEM-level persistence primitive —
  the DLL runs in the context of spoolsv.exe (SYSTEM). This is relatively obscure
  compared to Run keys or scheduled tasks, making it harder to find in standard
  persistence-hunting queries, but Autoruns and most blue team tooling covers print
  providers. Requires local admin. The Print Spooler (spoolsv.exe) must be running.
gotchas: |
  Requires local admin to install. The Print Spooler service must be running — on
  hardened post-PrintNightmare DCs, Spooler may be disabled. SharpPrinter is not
  actively maintained; test against current Windows versions. DLL must be placed at
  a stable path before installation (spoolsv.exe will try to load it at next restart).
  Cleanup: use `--remove` before cleanup or delete the DLL after removing the registry entry.
related_ttps: [sharpersist, godpotato, printspoofer]
alternatives: [sharpersist-scheduled-task, registry-run-key]
common_args:
  --dll:
    description: Path to DLL to install as print processor
    typical_values: ["C:\\\\Windows\\\\System32\\\\payload.dll"]
    required: false
  --name:
    description: Name for the installed print processor
    typical_values: ["LegitMonitor", "WinPrintHelper"]
  --list:
    description: List currently installed print processors
    typical_values: [flag-only]
  --remove:
    description: Remove a print processor by name
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpPrinter

A .NET assembly for installing DLLs as Windows print processors for persistence.
Print processors are DLLs loaded by the Print Spooler service (spoolsv.exe) at
startup — running as SYSTEM. This is a less-commonly-hunted persistence mechanism
compared to registry Run keys or scheduled tasks, making it useful for staying hidden
from standard autoruns checks.

## Typical use cases
- Establish SYSTEM-level persistence via a DLL loaded by spoolsv.exe at boot
- Persistence that survives user logoff and reboot
- An obscure persistence alternative when common mechanisms are hunted by the blue team

## How Sage uses this
SharpPrinter is a niche persistence option. Sage would suggest it only when more standard
mechanisms (SharPersist registry Run key, scheduled task) are being actively detected or
when SYSTEM-level persistent DLL execution is specifically needed.

## Output
Console confirmation of print processor installation/removal.
