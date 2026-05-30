---
name: SharpPack / Compile-from-Source Reference
category: defense-evasion
subcategories: [compilation, obfuscation, av-evasion, tool-preparation]
tradecraft_tags: [compilation, obfuscation, av-bypass, tool-prep, csharp, dotnet, pre-engagement]
mitre_attack: []
source:
  url: https://github.com/Flangvik/SharpCollection
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Compilation is performed on attacker infrastructure — no detection signal on target.
  The compiled binary's detection profile depends on compilation and obfuscation applied.
usage_examples:
  - description: Compile SharpHound with randomized class/method names (basic obfuscation)
    args: "dotnet build SharpHound.sln -c Release /p:Optimize=true"
  - description: Use ConfuserEx to obfuscate .NET assembly
    args: "ConfuserEx.exe -n SharpHound.exe -o obfuscated/"
  - description: Rename the binary (defeats string-based signature matching)
    args: "mv SharpHound.exe sysreport.exe"
opsec_notes: |
  The SharpCollection repo provides pre-compiled binaries with KNOWN HASHES signatured
  by all major EDR vendors. For operational use, ALWAYS compile from source.
  Minimum obfuscation steps:
  1. Compile from source (different compiler optimization artifacts than pre-built)
  2. Rename the output binary
  3. Use ConfuserEx or similar for .NET method/string obfuscation
  4. Test against the target EDR product before use
gotchas: |
  This is a PRE-ENGAGEMENT reference, not a run-time tool. Tool preparation happens
  on attacker infrastructure. The most impactful obfuscation for .NET tools is
  method/class renaming (ConfuserEx) since it changes internal symbols that some
  EDR rules reference. String encryption prevents string-based YARA/AMSI matches.
related_ttps: [sharp-collection, inceptor, donut]
alternatives: [confuserex, obfuscar, custom-build-scripts]
common_args: {}
last_updated: 2026-05-29
---

# Compile-from-Source Reference (Pre-Engagement Tool Preparation)

Operational guidance for preparing .NET tools before engagement deployment.
Pre-compiled binaries from SharpCollection have KNOWN HASHES and are signatured
by all major EDR vendors — never use them in production engagements.

## Compilation + Obfuscation Pipeline

### Step 1: Clone and Compile from Source

```bash
# Clone source (latest commit, clean build):
git clone https://github.com/SpecterOps/SharpHound
cd SharpHound
dotnet restore
dotnet build -c Release

# Output: bin/Release/net462/SharpHound.exe
```

### Step 2: Basic Obfuscation (ConfuserEx)

ConfuserEx applies multiple obfuscation passes to .NET assemblies:
- **Rename**: methods, classes, fields → random names (defeats EDR symbol matching)
- **ControlFlow**: alters method control flow (defeats static analysis)
- **AntiDebug**: adds anti-debug checks
- **Constants**: encrypts string literals

```
ConfuserEx.exe -n input.exe -o protected/
```

### Step 3: Rename Binary

Rename the output binary to something non-suspicious:
```
sysreport.exe (SharpHound → looks like a system report tool)
WinUpdate.exe (Rubeus → looks like Windows Update)
SvcMgr.exe (Seatbelt → looks like a service manager)
```

### Step 4: Test Against Target EDR

Before deployment, test the compiled+obfuscated binary against:
- Windows Defender (built-in, always test)
- The specific EDR vendor identified by SharpEDRChecker

Use tools like VirusTotal (WARNING: uploads to public DB — never use for operational
tools!) or private scanning infrastructure.

## EDR Bypass Reality Check (2026)

| Technique | Effectiveness against modern EDR |
|-----------|----------------------------------|
| Rename binary only | Low — behavioral detection catches usage regardless |
| Compile from source | Medium — changes file hash, avoids static sig |
| ConfuserEx + rename | Medium-High — bypasses most static analysis |
| Custom obfuscation + modify source | High — requires vendor-specific research |
| BOF execution (in-process) | High — no binary to scan, behavior is still flagged |

## Key Tools for Obfuscation

| Tool | Type | What it does |
|------|------|-------------|
| ConfuserEx | .NET | Full obfuscation (rename, control flow, strings) |
| Obfuscar | .NET | Simpler .NET obfuscator |
| InvisibilityCloak | .NET | String encryption focused |
| Donut | Shellcode gen | PE→shellcode (changes delivery format) |
| Inceptor | BOF/shellcode | Multi-format conversion + encoding |
