---
name: Watson
category: privilege-escalation
subcategories: [patch-vuln-scanner, cve-privesc, kernel-exploit-discovery]
tradecraft_tags: [cve, kernel-exploit, privesc, patch-analysis, hotfix, ghostpack]
mitre_attack:
  - id: T1068
    name: Exploitation for Privilege Escalation
source:
  url: https://github.com/rasta-mouse/Watson
  license: GPL-3.0
  maintained: true
binary_type: .net-assembly
binary_filename: Watson.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Watson reads Windows patch level (hotfix list) via WMI — benign read-only operation.
  No code execution, no network access. EDR detection is low. String-based detection
  for "Watson" assembly name.
usage_examples:
  - description: Check for missing patches that enable local privilege escalation
    args: "Watson.exe"
opsec_notes: |
  Watson is entirely read-only — it reads installed hotfixes and compares against
  known vulnerable patch states. Very low detection signal. The output tells you which
  CVE-based local privilege escalation exploits might work on the target.
gotchas: |
  Watson only checks for KNOWN CVE-based patch vulnerabilities — it doesn't cover
  configuration-based privesc (unquoted paths, writable services) which are SharpUp's domain.
  Run both Watson (CVE-based) and SharpUp (misconfiguration-based) for complete privesc
  coverage. Watson's CVE database is only as current as the last release — check the
  release date against the target's patch level. Old release = missed recent CVEs.
related_ttps: [sharpup, seatbelt, godpotato]
alternatives: [windows-exploit-suggester, wesng, powerup]
common_args: {}
last_updated: 2026-05-29
---

# Watson

GhostPack-adjacent .NET tool by Rasta-Mouse for identifying missing security patches
that enable known local privilege escalation vulnerabilities. Watson reads the installed
hotfix list and compares against a curated database of CVEs with working exploits
(PrintNightmare, EternalBlue era CVEs, and newer kernel-level escalations).

## Typical use cases
- Quickly determine which CVE-based LPE exploits may work on the target
- Complement SharpUp (misconfiguration-based privesc) with CVE-based privesc discovery

## How Sage uses this
Watson is run alongside SharpUp in the post-foothold privilege assessment phase.
Watson handles CVE-based vectors; SharpUp handles misconfiguration-based vectors.
Together they provide a complete privesc opportunity landscape.

## Output
Text listing of applicable CVEs with brief descriptions and patch-check results.
Example: `[CVE-2019-0841] DACL Bypass 7 / Partially Fixed / Unpatched!`
