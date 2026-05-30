---
name: UnmanagedPowerShell
category: defense-evasion
subcategories: [clr-host, powershell-without-powershell-exe, amsi-bypass, etw-bypass]
tradecraft_tags: [powershell, unmanaged, clr, amsi-bypass, etw-bypass, tifkin, defense-evasion, dotnet]
mitre_attack:
  - id: T1059.001
    name: Command and Scripting Interpreter — PowerShell
  - id: T1562.001
    name: Impair Defenses — Disable or Modify Tools
source:
  url: https://github.com/leechristensen/UnmanagedPowerShell
  license: Unknown
  maintained: false
binary_type: multi
binary_filename: UnmanagedPowerShell.exe / UnmanagedPowerShell.dll
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  UnmanagedPowerShell hosts the PowerShell engine (System.Management.Automation.dll)
  inside a native (C/C++) process rather than powershell.exe. Detection:
  - The hosting process (whatever loads the DLL) appears to load PS automation DLL
    without being powershell.exe — detectable by DLL load monitoring (Sysmon Event 7)
  - PowerShell script block logging still applies to scripts run through the hosted engine
    IF the ETW provider is initialized, BUT UnmanagedPowerShell intentionally avoids
    initializing the PS ETW providers → script block logging is bypassed
  - AMSI is also bypassed because the typical PS AMSI initialization via powershell.exe
    is skipped in the unmanaged host
usage_examples:
  - description: Run PowerShell script from an unmanaged (non-PS-exe) process
    args: "(load UnmanagedPowerShell.dll into a native process and call RunScript)"
  - description: Integrated into Cobalt Strike via shellcode wrapper
    args: "(CS aggressor script calls UnmanagedPowerShell to execute PS without spawning powershell.exe)"
  - description: Direct execution (example EXE wrapper)
    args: "UnmanagedPowerShell.exe 'Invoke-Mimikatz'"
opsec_notes: |
  UnmanagedPowerShell's core value:
  1. **No powershell.exe process**: Script runs inside any .exe or .dll host — avoids
     the "powershell.exe spawned by agent process" anomaly
  2. **AMSI bypassed**: The unmanaged host doesn't initialize AMSI the same way PS.exe does
  3. **Script block logging bypassed**: ETW PowerShell provider not initialized →
     Event 4104 not generated for scripts run this way
  
  Modern behavioral EDR (CrowdStrike Falcon) detects CLR hosting in native processes
  and the resulting PS automation DLL load — this technique is less effective than it was
  in 2017 when originally published. For modern environments, Apollo inline_assembly
  with .NET assemblies (no PowerShell at all) is a better alternative.
gotchas: |
  @tifkin_ (Lee Christensen) published this in 2017. It was integrated into Empire,
  Metasploit, and Cobalt Strike. Modern EDR has largely caught up. The technique is
  most effective against:
  - Legacy endpoints without behavioral EDR
  - Environments relying on PS script block logging as primary PS detection
  For Apollo engagements: inline_assembly avoids PowerShell entirely, making
  UnmanagedPowerShell rarely needed. Use it only when a specific PS-only tool MUST
  run without spawning powershell.exe.
related_ttps: [amsi-bypass, invoke-obfuscation, sharpamsi, apollo]
alternatives: [apollo-inline-assembly, bofnet-execute, invoke-obfuscation]
common_args: {}
last_updated: 2026-05-29
---

# UnmanagedPowerShell

Lee Christensen (@tifkin_) / SpecterOps' technique for hosting the PowerShell engine
inside an unmanaged (native C/C++) process. UnmanagedPowerShell runs PowerShell scripts
without spawning powershell.exe, bypassing Script Block Logging and AMSI initialization.
Widely integrated into C2 frameworks circa 2017-2020; modern behavioral EDR detects
the underlying CLR load pattern.

## The Technique

```
Standard execution:
  Agent → spawn powershell.exe → run script
  Detection: powershell.exe process creation, script block logging

UnmanagedPowerShell:
  Agent (native) → load System.Management.Automation.dll → host PS engine inline
  → run script without ever creating powershell.exe
  Detection: DLL load (SMA.dll) in non-PS process (Sysmon Event 7)
```

## Why AMSI and Script Block Logging Are Bypassed

When powershell.exe starts, it:
1. Loads AMSI provider and registers AMSI callbacks
2. Initializes ETW PowerShell provider (for script block logging)

When UnmanagedPowerShell loads the PS engine:
1. Neither AMSI nor ETW PS provider are initialized → both bypassed
2. Scripts run "bare" without AMSI scanning or logging

## Historical Context

- 2017: Published by @tifkin_; major breakthrough in PS evasion
- 2018-2019: Integrated into Empire, Metasploit, Cobalt Strike PowerPick
- 2020+: Behavioral EDR (CrowdStrike, SentinelOne) detects CLR-in-native-process pattern
- 2026: Less effective against modern EDR; Apollo inline_assembly is generally preferred

## Apollo Integration Context

Apollo's `powershell_import` command may use an unmanaged PS host internally (depending
on Apollo version). If it does, this technique is already baked in. Check Apollo's
implementation before deciding whether to use this separately.
