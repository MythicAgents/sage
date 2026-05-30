---
name: Responder
category: coercion-relay
subcategories: [llmnr-poisoning, nbt-ns-poisoning, ntlm-capture, mdns]
tradecraft_tags: [llmnr, nbt-ns, mdns, ntlm-capture, poisoning, initial-access]
mitre_attack:
  - id: T1557.001
    name: Adversary-in-the-Middle — LLMNR/NBT-NS Poisoning and SMB Relay
source:
  url: https://github.com/lgandx/Responder
  license: GPL-3.0
  maintained: true
binary_type: python-script
binary_filename: Responder.py
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  LLMNR/NBT-NS responses from a non-DC host are anomalous and detectable by
  network monitoring (unexpected responders). Windows Defender for Endpoint and
  Microsoft Sentinel have built-in LLMNR/NBT-NS poisoning detection. Organizations
  that have disabled LLMNR and NBT-NS are fully immune. Responder's SMB/HTTP/FTP/NFS
  servers generate network traffic on multiple ports.
usage_examples:
  - description: Capture NTLM hashes by poisoning network name resolution
    args: "python3 Responder.py -I eth0"
  - description: Analyze mode — see queries without responding (passive, no poisoning)
    args: "python3 Responder.py -I eth0 -A"
  - description: Enable all rogue servers for maximum capture surface
    args: "python3 Responder.py -I eth0 --lm --disable-ess"
  - description: Poison specific hostnames only
    args: "python3 Responder.py -I eth0 -f --fingerprint"
opsec_notes: |
  Responder is noisy at the network level — it responds to name queries for all
  failed resolutions, which may alert defenders watching for unusual responders on
  the network. In environments that have disabled LLMNR/NBT-NS, Responder is
  ineffective. Analyze mode (`-A`) is entirely passive. For initial access, combine
  with ntlmrelayx for relay attacks; for hash capture, combine with hashcat (offline
  crack — not Sage's responsibility). The captured NTLMv2 hashes require offline
  cracking; combine with ntlmrelayx relay instead to avoid crack dependency.
gotchas: |
  Python-only — not Apollo-runnable. LLMNR is disabled in modern Windows environments
  (Group Policy). Many corporate networks have these protocols disabled. Check first
  with `-A` (analyze mode) before running full poisoning. NTLMv2 hashes captured
  require offline cracking — Sage cannot crack these. Focus on relay attacks (ntlmrelayx)
  rather than hash capture to avoid the cracking dependency.
related_ttps: [ntlmrelayx, coercer, petitpotam, spoolsample]
alternatives: [inveigh, mitm6]
common_args:
  -I:
    description: Network interface to listen on
    typical_values: ["eth0", "ens18"]
    required: true
  -A:
    description: Analyze mode — passive listening only, no poisoning
    typical_values: [flag-only]
  --lm:
    description: Force LM hashing downgrade (older clients)
    typical_values: [flag-only]
  --disable-ess:
    description: Disable Extended Session Security
    typical_values: [flag-only]
  -f:
    description: Fingerprint hosts that send name queries
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Responder

The standard LLMNR/NBT-NS/mDNS poisoning tool. When a Windows host fails to resolve
a hostname via DNS, it falls back to LLMNR (Link-Local Multicast Name Resolution) and
NBT-NS (NetBIOS Name Service) broadcasts. Responder intercepts these broadcasts and
responds with attacker-controlled IP, forcing the client to authenticate via NTLM —
providing either NTLMv2 hashes (for offline cracking) or relayable NTLM authentication
(paired with ntlmrelayx).

## Typical use cases
- Capture NTLMv2 hashes from misdirected name queries for relay to ntlmrelayx
- Passive network fingerprinting with `-A` (analyze mode) before active exploitation
- Combined with ntlmrelayx for NTLM relay → RBCD/shadow-credentials chains
- Internal network lateral movement from network position without existing credentials

## How Sage uses this
Responder is infrastructure-side tooling. It's used alongside ntlmrelayx to capture
coerced or organic NTLM authentication and relay it to LDAP/ADCS. The combination of
Responder (passive poisoning) + ntlmrelayx (active relay) + coercion tool is the
classic internal NTLM relay chain.

**Important:** Captured NTLMv2 hashes require offline cracking — Sage does not crack
hashes. The operational path that avoids cracking is: relay with ntlmrelayx instead
of capturing for offline crack.

## Apollo-specific note
Python/Linux-only — not Apollo-runnable. Runs on attacker infrastructure.

## Output
Console output listing captured NTLM hashes per client authentication attempt.
Hashes are also written to `/usr/share/responder/logs/` (or equivalent path).
