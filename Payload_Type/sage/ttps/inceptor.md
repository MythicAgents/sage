---
name: Inceptor
category: defense-evasion
subcategories: [bof-to-assembly, packer, template-injection, loader]
tradecraft_tags: [defense-evasion, bof, assembly-loader, encoding, obfuscation, antivirus-bypass]
mitre_attack:
  - id: T1027
    name: Obfuscated Files or Information
source:
  url: https://github.com/klezVirus/inceptor
  license: Unknown
  maintained: false
binary_type: python-script
binary_filename: inceptor.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Inceptor generates obfuscated loaders — the detection signal depends on the output
  artifact, not Inceptor itself (which runs on attacker infrastructure). Output
  assemblies/shellcode loaders may bypass static AV but behavioral EDR detects the
  in-memory activity when the payload executes.
usage_examples:
  - description: Convert a BOF to a .NET assembly for Apollo inline_assembly
    args: "python3 inceptor.py -t bof -e xor -p myBOF.o -o output.exe"
  - description: Encode a .NET assembly to bypass AV
    args: "python3 inceptor.py -t dotnet -e b64xor -p SharpHound.exe -o SharpHoundEvaded.exe"
  - description: Generate shellcode loader from raw shellcode
    args: "python3 inceptor.py -t shellcode -e xor -p payload.bin -o loader.exe"
opsec_notes: |
  Inceptor is an attacker-infrastructure tool — it converts and encodes payloads for
  deployment, not a runtime tool. The key value is the BOF-to-assembly conversion path:
  BOFs that Apollo cannot run (nanodump, TrustedSec CS BOFs) can be wrapped into .NET
  assemblies for inline_assembly delivery. Detection evasion quality degrades as AV
  signature databases update — test converted output against target AV before use.
gotchas: |
  Inceptor is Python (attacker infrastructure, not target). Not actively maintained
  (last commit ~2022); compatibility with newer .NET and Windows versions may be limited.
  BOF-to-assembly conversion has limitations — not all BOFs convert cleanly; some BOF
  APIs are not available in the .NET host environment that Inceptor generates. Test
  converted assemblies thoroughly before operational use. Alternative: rewrite the BOF
  as a native .NET assembly if conversion produces unreliable output.
related_ttps: [nanodump, sharphound, rubeus, standin]
alternatives: [donut, pe2shellcode, manual-dotnet-port]
common_args:
  -t:
    name: -t
    description: Target type to convert/encode
    typical_values: [bof, dotnet, shellcode, pe]
    required: true
  -e:
    name: -e
    description: Encoding to apply
    typical_values: [xor, b64, b64xor, aes]
  -p:
    name: -p
    description: Input payload file
    typical_values: ["myBOF.o", "SharpHound.exe", "payload.bin"]
    required: true
  -o:
    name: -o
    description: Output file path
    typical_values: ["output.exe", "evaded.exe"]
    required: true
last_updated: 2026-05-29
---

# Inceptor

An attacker-infrastructure tool for converting and obfuscating payloads — particularly
for converting BOFs to .NET assemblies that can run via Apollo's `inline_assembly` command.
Inceptor's primary value for Apollo operators is the BOF-to-assembly conversion path:
tools like nanodump (which is a BOF) can potentially be wrapped into a .NET assembly
suitable for inline_assembly delivery. The tool also handles general payload encoding
and obfuscation.

## Typical use cases
- Convert a BOF to a .NET assembly for Apollo inline_assembly delivery (Apollo BOF gap workaround)
- Obfuscate/encode a .NET assembly to evade AV static signatures
- Generate shellcode loaders from raw shellcode

## How Sage uses this
Inceptor is an infrastructure-side tool. Its documented relevance to Sage is the BOF
conversion path — enabling BOF-based tools (nanodump, TrustedSec CS BOFs) to be
run via Apollo before native BOF support is added. Operators with Inceptor in their
toolkit can convert nanodump.x64.o to a .NET assembly, then use it via inline_assembly.

## Apollo-specific note
Inceptor runs on attacker infrastructure. The OUTPUT of Inceptor (a .NET assembly) runs
on the target via Apollo inline_assembly. This is the documented workaround for Apollo's
lack of a native BOF runner.
