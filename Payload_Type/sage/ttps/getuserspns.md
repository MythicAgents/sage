---
name: GetUserSPNs
category: credential-access
subcategories: [kerberoasting, spn-enumeration]
tradecraft_tags: [kerberoasting, spn, impacket, python, linux-side, tgs-hash]
mitre_attack:
  - id: T1558.003
    name: Steal or Forge Kerberos Tickets — Kerberoasting
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: GetUserSPNs.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  TGS requests for service accounts appear in Kerberos traffic (Event 4769 on DC).
  Mass TGS requests for many service accounts in a short time window is detectable
  by MDI and SIEM Kerberos analytics. Individual TGS requests are normal traffic.
usage_examples:
  - description: List all kerberoastable accounts (no hash request)
    args: "GetUserSPNs.py north.sevenkingdoms.local/jon.snow:Password123 -dc-ip 192.168.56.10"
  - description: Request TGS tickets and output as hashcat format
    args: "GetUserSPNs.py north.sevenkingdoms.local/jon.snow:Password123 -dc-ip 192.168.56.10 -request -outputfile kerberoast.txt"
  - description: Request TGS with pass-the-hash
    args: "GetUserSPNs.py -hashes :nthash north.sevenkingdoms.local/administrator -dc-ip 192.168.56.10 -request"
opsec_notes: |
  Kerberoasting captures TGS hashes which require OFFLINE CRACKING — Sage does not
  crack hashes. The operational value is inventory (find kerberoastable accounts)
  and potential lateral movement if a weak password is discovered offline. For
  Sage-driven chains, prefer ADCS/delegation paths that don't require offline cracking.
  Python-only — use Rubeus kerberoast for Windows-side enumeration.
gotchas: |
  TGS hashes require OFFLINE CRACKING (hashcat, john) — Sage cannot perform this.
  Documented for completeness; in Sage-driven chains, kerberoasting is generally
  lower priority than crack-free paths (ADCS, delegation, RBCD). If kerberoastable
  accounts are service accounts with weak passwords, note them for operator follow-up
  rather than attempting in-chain exploitation.
related_ttps: [rubeus, sharphound, bloodhound-ingest]
alternatives: [rubeus-kerberoast, crackmapexec-kerberoast]
common_args:
  target:
    description: DOMAIN/username:password
    typical_values: ["north.sevenkingdoms.local/jon.snow:Password123"]
    required: true
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
    required: true
  -request:
    description: Request TGS tickets and output hashes
    typical_values: [flag-only]
  -outputfile:
    description: Write hashes to file for offline cracking
    typical_values: ["kerberoast.txt"]
  -hashes:
    description: NTLM hash for pass-the-hash
    typical_values: [":nthash"]
last_updated: 2026-05-29
---

# GetUserSPNs

impacket's `GetUserSPNs.py` — the Linux-side kerberoasting tool. Enumerates service
principal name (SPN)-mapped accounts in the domain and optionally requests their TGS
tickets in hashcat-compatible format. The hashes require offline cracking. GetUserSPNs
is the Linux equivalent of Rubeus kerberoast, used when operating entirely from Linux
infrastructure.

## Typical use cases
- Enumerate kerberoastable service accounts from Linux
- Request TGS hashes for offline cracking (operator task, not Sage)
- Identify which service accounts have SPNs for intelligence

## How Sage uses this
Infrastructure-side Python tool. Sage primarily uses this for SPN enumeration
(without requesting hashes) to identify service accounts. Hash cracking is not a
Sage capability — any kerberoastable accounts found are reported to the operator.

## Important note on cracking
GetUserSPNs.py with `-request` outputs TGS hashes that require offline cracking.
Sage does not perform offline cracking. This tool's primary value in Sage's workflow
is the enumeration phase (no `-request`) to identify kerberoastable accounts.
