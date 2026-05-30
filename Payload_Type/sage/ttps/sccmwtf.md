---
name: SCCMWtf
category: recon
subcategories: [sccm-abuse, credential-access, lateral-movement]
tradecraft_tags: [sccm, mecm, configmgr, naa, credential-access, lateral-movement]
mitre_attack:
  - id: T1072
    name: Software Deployment Tools
source:
  url: https://github.com/subat0mik/Misconfiguration-Manager
  license: Unknown
  maintained: true
binary_type: multi
binary_filename: (see sub-tools)
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  HTTP requests to the SCCM management point (MP) for policy retrieval; WMI queries
  for client configuration data. Anomalous SCCM enrollment requests from machines that
  aren't registered SCCM clients. SCCM logging on the MP server tracks all policy
  requests.
usage_examples:
  - description: Dump NAA credentials from SCCM without admin rights (if SCCM MP is accessible)
    args: "SharpSCCM.exe local naa"
  - description: Enumerate the SCCM hierarchy from a non-domain-member
    args: "SharpSCCM.exe get site -sms SCCMSERVER"
  - description: Retrieve SCCM device policies as a fake client
    args: "(sccm-dp-dump technique via HTTP to MP)"
opsec_notes: |
  Misconfiguration-Manager documents multiple SCCM attack paths, including network-based
  approaches that don't require an agent on target SCCM machines. The most impactful
  paths (NAA credential extraction, secret policy retrieval) may be detectable by SCCM
  MP logs. SharpSCCM covers most of the same attacks; this TTP file covers the broader
  Misconfiguration-Manager / SCCMWtf attack surface.
gotchas: |
  SCCMWtf is actually a collection of techniques from the Misconfiguration-Manager
  project rather than a single tool. The attacks range from no-creds-needed (HTTP to
  MP) to full-admin. NAA credential extraction is the highest-value no-admin attack.
  Only relevant in environments with SCCM/MECM deployed. See SharpSCCM for the
  primary .NET assembly implementation of these techniques.
related_ttps: [sharpsccm, seatbelt, sharphound]
alternatives: [sharpsccm]
common_args: {}
last_updated: 2026-05-29
---

# SCCMWtf

A reference to the Misconfiguration-Manager project's SCCM attack techniques, beyond
what SharpSCCM implements. Covers network-based SCCM attacks (HTTP to management point
without registering as a client), policy retrieval attacks, and the full SCCM privilege
escalation chain. The most valuable no-admin technique is extracting Network Access Account
(NAA) credentials which are distributed to all SCCM clients as domain credentials.

## Typical use cases
- Enumerate SCCM hierarchy from a network position without being a registered SCCM client
- Retrieve NAA credentials via HTTP policy request to the SCCM Management Point
- Attack SCCM administrative infrastructure for lateral movement to all managed devices

## How Sage uses this
SCCMWtf represents the broader SCCM attack surface. Sage uses SharpSCCM for the
primary SCCM toolkit; this file documents the network-based attack paths that don't
require a foothold on a managed machine.

## See also
- `sharpsccm.md` — the primary .NET tool for SCCM attacks
- Misconfiguration-Manager project: https://github.com/subat0mik/Misconfiguration-Manager
- Chris Thompson / @\_Mayyhem blog posts on SCCM attacks
