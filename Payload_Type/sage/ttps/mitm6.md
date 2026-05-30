---
name: mitm6
category: coercion-relay
subcategories: [ipv6-poisoning, dhcpv6, ntlm-relay, dns-hijacking]
tradecraft_tags: [ipv6, dhcpv6, dns, ntlm-relay, initial-access, python]
mitre_attack:
  - id: T1557
    name: Adversary-in-the-Middle
source:
  url: https://github.com/dirkjanm/mitm6
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: mitm6.py
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  DHCPv6 traffic from unexpected sources is detectable by network monitoring. The
  DHCPv6 server responding on a network where no legitimate DHCPv6 server exists
  is anomalous. DNS responses from unexpected hosts (captured when Windows queries
  its "IPv6 DNS server") are visible in DNS logs. Network-level anomaly detection
  and IDS rules for DHCPv6 spoofing.
usage_examples:
  - description: Poison DHCPv6 to become the IPv6 DNS server (combined with ntlmrelayx)
    args: "python3 mitm6.py -d north.sevenkingdoms.local"
  - description: Limit to specific targets
    args: "python3 mitm6.py -d north.sevenkingdoms.local -hw VICTIM_HOST_MAC"
  - description: Set up full relay chain (mitm6 + ntlmrelayx)
    args: "# Terminal 1: mitm6.py -d domain.local\n# Terminal 2: ntlmrelayx.py -6 -t ldaps://DC --delegate-access"
opsec_notes: |
  mitm6 exploits that Windows prefers IPv6 DNS (RFC-standard behavior) to capture
  authentication from machines that Windows computers send to the "new" DNS server.
  Combined with ntlmrelayx, this provides periodic NTLM authentication to relay without
  any active coercion. However, DHCPv6 traffic is very visible on the network and
  many organizations have disabled IPv6 on internal networks, making this ineffective.
  Check whether IPv6 is active on the target network before deploying.
gotchas: |
  IPv6 must be enabled on the target network (many corporate networks disable it).
  mitm6 responds to ALL DHCPv6 SOLICIT messages — very broad scope. Limit with `-hw`
  to specific MAC addresses for targeted operation. The relay chain typically takes
  time (waits for Windows machines to renew their DHCPv6 lease). Python-only — not
  Apollo-runnable.
related_ttps: [ntlmrelayx, responder, coercer]
alternatives: [responder, coercer]
common_args:
  -d:
    description: Target domain FQDN for DNS responses
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -hw:
    description: Limit to specific hardware (MAC) address
    typical_values: ["AA:BB:CC:DD:EE:FF"]
  -i:
    description: Network interface to listen on
    typical_values: ["eth0"]
last_updated: 2026-05-29
---

# mitm6

Dirk-jan Mollema's IPv6 DNS takeover / DHCPv6 poisoning tool. Exploits the default
Windows behavior of preferring IPv6 DNS by responding to DHCPv6 SOLICIT messages,
advertising the attacker's host as the IPv6 DNS server. Once established as the DNS
server, Windows machines send NTLM authentication to the attacker for WPAD / other
name resolution, which is then relayed via ntlmrelayx.

## Typical use cases
- Capture NTLM authentication from domain machines for relay without active coercion
- Initial access in environments where LLMNR/NBT-NS are disabled (IPv6 fallback)
- Combined with ntlmrelayx for RBCD/shadow credential setup

## How Sage uses this
Infrastructure-side Python tool. Documented for the initial access / coercion landscape.
Combine with ntlmrelayx for a passive relay chain that harvests auth without active coercion.

## Apollo-specific note
Python/Linux-only — not Apollo-runnable. Infrastructure-side attack.
