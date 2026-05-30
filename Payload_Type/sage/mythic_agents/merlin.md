---
name: Merlin
mythic_payload_type: merlin
supported_os: [windows, linux, macos]
description: Go-based cross-platform Mythic agent with built-in OPAQUE authentication
author: Ne0nd0g
source_url: https://github.com/MythicAgents/merlin
version_tested: 1.x (reference 2026-05-29)
binary_type_execution:
  .net-assembly:
    command: load-assembly
    upload_required: true
    parameters_template:
      assembly: "<Mythic file UUID>"
      args: "<arguments>"
    fallback: null
  powershell-script:
    command: run
    upload_required: false
    parameters_template:
      program: "powershell.exe"
      args: "-Command <ps command>"
    fallback: null
  shellcode:
    command: shinject
    upload_required: true
    parameters_template:
      pid: "<target PID>"
      shellcode: "<Mythic file UUID>"
    fallback: null
  bof:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Merlin does not have a built-in BOF runner. Use Athena for BOF execution needs.
  native-exe:
    command: run
    upload_required: true
    parameters_template:
      program: "<uploaded EXE path>"
      args: "<arguments>"
    fallback: null
  python-script:
    command: run
    upload_required: false
    parameters_template:
      program: "python3"
      args: "<script>"
    fallback: Only works on Linux/macOS targets with Python installed.
native_capabilities:
  run:
    command: run
    parameters:
      program: "<program to run>"
      args: "<arguments>"
    notes: Generic program execution (all platforms).
  socks:
    command: socks
    parameters:
      action: "<start|stop>"
      port: "<port>"
    notes: Built-in SOCKS5 proxy.
  memory_patch:
    command: memory
    parameters:
      method: "<patch method>"
    notes: Memory patching for AMSI/ETW bypass.
file_upload_pattern: |
  Same as Apollo — upload binary to Mythic file store, get UUID, reference in command.
opsec_notes: |
  Merlin is written in Go — distinct binary characteristics from .NET agents (Apollo,
  Athena). OPAQUE protocol for C2 authentication provides stronger cryptographic
  guarantees. Cross-platform: same binary structure across OS targets.
known_gaps:
  - bof (no BOF runner)
last_updated: 2026-05-29
---

# Merlin

Ne0nd0g's Go-based cross-platform Mythic agent. Merlin supports Windows, Linux, and
macOS targets and uses the OPAQUE protocol for C2 authentication. Unlike Apollo and
Athena (.NET-based), Merlin is a Go binary — different EDR signature profile.

## Execution model
- Native Go binary execution via `run` command
- .NET assembly loading via `load-assembly`
- Shellcode injection via `shinject`
- Built-in SOCKS5 proxy
- No BOF runner (use Athena for BOF needs)

## Notable native commands
- `run` — generic program execution with arguments
- `socks` — SOCKS5 proxy for network pivoting
- `memory` — memory patching (AMSI/ETW)
- `load-assembly` — .NET assembly execution
- Standard file/directory operations

## OPSEC considerations
Go binary has different detection profile from .NET agents. Cross-compilation to many
platforms. OPAQUE authentication prevents C2 traffic analysis attacks.

## When to use Merlin vs Apollo
- Linux/macOS targets: Merlin is appropriate; Apollo is Windows-only
- When .NET detection is a concern: Merlin's Go binary has different signatures
- BOF needs: use Athena instead

## See also
- Apollo — Windows .NET agent with richer native commands
- Athena — .NET agent with BOF support
- Poseidon — Linux/macOS Go-based agent
