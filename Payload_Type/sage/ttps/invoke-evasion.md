---
name: Invoke-Evasion
category: defense-evasion
subcategories: [powershell-evasion, data-science-obfuscation, amsi-bypass]
tradecraft_tags: [powershell, evasion, obfuscation, data-science-driven, ghostpack, harmj0y]
mitre_attack:
  - id: T1027.010
    name: Obfuscated Files or Information — Command Obfuscation
source:
  url: https://github.com/GhostPack/Invoke-Evasion
  license: BSD-3-Clause
  maintained: false
binary_type: powershell-script
binary_filename: Invoke-Evasion.ps1
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Obfuscated PowerShell — same detection considerations as Invoke-Obfuscation. Script
  block logging captures deobfuscated content. AMSI catches patterns that survive
  obfuscation. Behavioral EDR detects the execution pattern regardless of obfuscation.
usage_examples:
  - description: Apply evasion to a PowerShell script
    args: "Invoke-Evasion -ScriptBlock { <your PS code> } -Technique DATA"
  - description: Apply string obfuscation with data-science-informed character substitutions
    args: "Invoke-Evasion -ScriptBlock { Invoke-Mimikatz } -Technique DATA"
opsec_notes: |
  Invoke-Evasion applies data-science-informed obfuscation — character substitution
  patterns learned from analyzing which specific characters/strings trigger AMSI.
  More effective than naive obfuscation because it targets specific AMSI detection paths.
  However: (1) deobfuscated content is still captured by script block logging, (2) the
  technique is documented and AMSI vendors have updated, (3) behavioral EDR catches
  the execution regardless. For 2026 environments, prefer .NET assemblies via Apollo
  inline_assembly over PowerShell.
gotchas: |
  Not actively maintained (GhostPack archived). The data-science component (learning
  which character substitutions evade AMSI) was based on AMSI behavior at publication
  time — vendors update. Treat as a starting point for obfuscation, not a solved problem.
  Compare with Invoke-Obfuscation — both do similar work; Invoke-Evasion is more focused
  on data-driven AMSI bypass specifically.
related_ttps: [amsi-bypass, invoke-obfuscation, sharpamsi, unmanaged-powershell]
alternatives: [invoke-obfuscation, chameleon-ps]
common_args:
  -ScriptBlock:
    description: PowerShell script to apply evasion to
    typical_values: ["{<your PS code>}"]
    required: true
  -Technique:
    description: Evasion technique to apply
    typical_values: [DATA, STRING, TOKEN]
last_updated: 2026-05-29
---

# Invoke-Evasion

GhostPack's data-science-driven PowerShell obfuscation tool. Unlike Invoke-Obfuscation
(which applies syntactic transformations), Invoke-Evasion was developed using analysis
of which specific AMSI patterns trigger detections — applying statistically-informed
character substitutions that target known AMSI detection gaps.

## The Data-Science Approach

```
Traditional obfuscation:
  Replace "Invoke-Mimikatz" with "Inv" + "ok" + "e-" + "Mi" + "mi" + "katz"
  
Invoke-Evasion:
  Analyzed thousands of AMSI detection patterns →
  Learned which specific character substitutions cross detection thresholds →
  Applied statistically-optimal transformations
```

## Historical Context and Current Status

Published by @harmj0y as part of GhostPack (2019-2020). The underlying data-science
methodology was presented at security conferences. Archived — the specific AMSI patterns
it was trained on have been updated by vendors. Use as a component in a layered
obfuscation strategy rather than a standalone solution.

## Recommended Approach

For maximum PS evasion in 2026:
1. **Don't use PowerShell** → Apollo inline_assembly (.NET avoids PS entirely)
2. If PS is required: Invoke-Obfuscation + Invoke-Evasion + manual review
3. Test against target EDR before deployment
