---
name: SharpWSUS / Windows Update Abuse (SharpWUA)
category: persistence
subcategories: [wsus-persistence, windows-update-hijack, software-update-abuse]
tradecraft_tags: [wsus, windows-update, hijack, persistence, pre-auth, lateral-movement]
mitre_attack:
  - id: T1195
    name: Supply Chain Compromise
  - id: T1072
    name: Software Deployment Tools
source:
  url: https://github.com/nettitude/SharpWSUS
  license: Apache-2.0
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  Windows Update client behavior check: the WUA (Windows Update Agent) configuration
  is stored in the registry. Intercepting or modifying these settings is detectable
  by registry monitoring. Redirecting update traffic to a rogue WSUS server requires
  either DNS poisoning or WSUS configuration modification.
usage_examples:
  - description: Check WSUS configuration (discover if WSUS is deployed)
    args: "reg query HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate /v WUServer"
  - description: Check Windows Update Source
    args: "reg query HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate /v UseWUServer"
  - description: Check WSUS URL from registry
    args: "reg query HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate"
opsec_notes: |
  This TTP file documents WSUS DISCOVERY specifically — finding WSUS configuration
  in the registry to determine if a WSUS server is deployed (and is therefore a
  valuable attack target). See SharpWSUS.md for the actual WSUS injection attack.
  WSUS discovery is passive (registry reads) with minimal detection signal.
gotchas: |
  Many organizations use WSUS but HTTPS WSUS (which requires certificate signing) is
  not exploitable via SharpWSUS. Only HTTP WSUS is vulnerable to injection attacks.
  If WSUS URL starts with https://, the injection attack is not viable without the
  WSUS certificate.
related_ttps: [sharpwsus, seatbelt, credential-hunting-checklist]
alternatives: [seatbelt-wsus-check, registry-read]
common_args: {}
last_updated: 2026-05-29
---

# WSUS Discovery and Abuse Reference

Reference for discovering WSUS configuration and determining whether WSUS injection
attacks (via SharpWSUS) are viable.

## WSUS Discovery (Registry)

```cmd
rem Check if WSUS is configured:
reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"

rem Key values to look for:
rem WUServer — WSUS server URL (http:// = vulnerable, https:// = protected)
rem UseWUServer — 1 = using WSUS, 0 = using Microsoft Update directly
```

## Viability Check

| WUServer value | UseWUServer | Viable? |
|----------------|------------|---------|
| Not set | — | No WSUS — machine uses Microsoft Update directly |
| http://... | 1 | YES — HTTP WSUS, injection viable |
| https://... | 1 | NO — HTTPS WSUS, injection blocked by TLS |
| http://... | 0 | Policy set but not active |

## If WSUS is Viable

See `sharpwsus.md` for the full injection attack:
1. `SharpWSUS.exe locate` — confirm WSUS URL from running agent
2. `SharpWSUS.exe inspect` — enumerate WSUS server capabilities
3. `SharpWSUS.exe create` — inject malicious update
4. `SharpWSUS.exe approve` — target specific computer
5. Wait for next Windows Update cycle (or trigger manually)
6. `SharpWSUS.exe delete` — cleanup

## Detection Implication

WSUS injection is stealthy from the endpoint perspective — the update appears legitimate.
Forensic evidence: WSUS server database modification, unusual IIS logs on WSUS server.
