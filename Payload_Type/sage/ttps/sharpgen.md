---
name: SharpGen / GadgetToJScript
category: defense-evasion
subcategories: [payload-generation, obfuscation, dotnet-to-js, jscript-shellcode]
tradecraft_tags: [dotnet, jscript, wscript, obfuscation, payload-generation, applocker-bypass]
mitre_attack:
  - id: T1027.010
    name: Obfuscated Files or Information — Command Obfuscation
source:
  url: https://github.com/med0x2e/GadgetToJScript
  license: MIT
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  JScript/VBScript executing .NET assemblies via COM gadgets is detectable by behavioral
  EDR. wscript.exe or cscript.exe loading .NET assemblies is anomalous. Modern EDR
  (particularly Microsoft Defender) has signatures for GadgetToJScript output patterns.
usage_examples:
  - description: Convert a .NET assembly to JScript payload (GadgetToJScript)
    args: "GadgetToJScript.exe -a Seatbelt.exe -w js -o payload.js"
  - description: Convert to VBScript
    args: "GadgetToJScript.exe -a SharpHound.exe -w vbs -o payload.vbs"
  - description: Execute converted JScript payload via wscript (AppLocker bypass)
    args: "wscript.exe payload.js"
opsec_notes: |
  GadgetToJScript converts .NET assemblies to JScript or VBScript payloads that
  can execute via wscript.exe or mshta.exe — useful for AppLocker bypass scenarios
  where .NET assemblies are blocked but scripting hosts are allowed. However,
  the output format is heavily signatured by modern AMSI and EDR. For Apollo
  engagements, inline_assembly is the stealthier path; GadgetToJScript is most
  useful for initial access / phishing scenarios.
gotchas: |
  GadgetToJScript is not actively maintained. The specific COM gadgets it uses
  (Windows Script Encoding + .NET activation) are heavily detected. For 2026
  environments, the primary use case is initial access (email attachment → wscript
  → C2 callback) rather than post-exploitation. Post-foothold, Apollo inline_assembly
  is preferred over scripting-host-based execution.
related_ttps: [lolbas-reference, amsi-bypass, inceptor]
alternatives: [inceptor, donut, sharpgen]
common_args:
  -a:
    description: Input .NET assembly to convert
    typical_values: ["Seatbelt.exe", "SharpHound.exe"]
    required: true
  -w:
    description: Output wrapper type
    typical_values: [js, vbs, hta]
    required: true
  -o:
    description: Output file path
    typical_values: ["payload.js", "payload.vbs"]
    required: true
last_updated: 2026-05-29
---

# SharpGen / GadgetToJScript

GadgetToJScript converts .NET assemblies into JScript or VBScript payloads that
execute via Windows scripting hosts (wscript.exe, mshta.exe). Primary use case:
AppLocker bypass via scripting host (signed Microsoft binary) when .NET assembly
execution is blocked. The resulting scripts are heavily signatured in 2026 — primarily
useful for initial access scenarios rather than post-exploitation.

## Use Case Hierarchy

```
Post-exploitation (already have Apollo):
  → Apollo inline_assembly is better than GadgetToJScript output

Initial access (no foothold yet):
  → GadgetToJScript payload as email attachment → wscript.exe executes
  → Useful against AppLocker environments where custom EXEs are blocked
  → But AMSI on scripting hosts may block the payload
```

## Output formats

| Format | Executed by | Notes |
|--------|-------------|-------|
| `.js` | wscript.exe / cscript.exe | JScript; compact |
| `.vbs` | wscript.exe / cscript.exe | VBScript; larger |
| `.hta` | mshta.exe | HTML application; can embed JS/VBS |

## Detection State

GadgetToJScript output patterns are in AMSI's and Defender's signature database.
Always obfuscate output before use. The underlying COM gadget approach (activating
.NET via COM) is behaviorally detectable regardless of string obfuscation.
