---
name: Athena
mythic_payload_type: athena
supported_os: [windows, linux, macos]
description: Cross-platform .NET Mythic agent with BOF support via AthenaPlugins
author: MythicAgents
source_url: https://github.com/MythicAgents/Athena
version_tested: 3.x (confirmed 2026-05-29)
binary_type_execution:
  .net-assembly:
    command: inline_assembly
    upload_required: true
    parameters_template:
      assembly_name: "<registered assembly name>"
      assembly_file: "<Mythic file UUID>"
      assembly_arguments: "<args string>"
    fallback: null
  bof:
    command: execute-bof
    upload_required: true
    parameters_template:
      bof_file: "<Mythic file UUID for .o BOF file>"
      entry_point: "<entry point name, default: go>"
      arguments: "<packed BOF arguments>"
    fallback: null
  powershell-script:
    command: powershell_import
    upload_required: true
    parameters_template:
      file: "<Mythic file UUID>"
    fallback: null
  shellcode:
    command: shinject
    upload_required: true
    parameters_template:
      pid: "<target PID>"
      shellcode: "<Mythic file UUID>"
    fallback: null
  native-exe:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Athena does not have a generic native EXE runner. Wrap the EXE as shellcode
      or use shinject with a shellcode wrapper.
  python-script:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: Athena cannot run Python directly on Windows.
native_capabilities:
  bof_execution:
    command: execute-bof
    parameters:
      bof_file: "<Mythic file UUID for .o file>"
      entry_point: "<BOF entry point, default: go>"
      arguments: "<packed arguments using BOF argument format>"
    notes: |
      Athena's primary advantage over Apollo is native BOF execution via execute-bof.
      This enables nanodump, TrustedSec CS-Situational-Awareness-BOFs, and other BOF
      tools directly without conversion.
  arp_scan:
    command: arp-scan
    parameters:
      cidr: "<target CIDR range>"
    notes: Network discovery via ARP scanning.
  port_scan:
    command: port-scan
    parameters:
      hosts: "<comma-separated hosts or CIDR>"
      ports: "<comma-separated ports or ranges>"
    notes: TCP port scanner built-in.
  socks_proxy:
    command: socks
    parameters:
      port: "<local SOCKS5 port>"
      action: "<start|stop>"
    notes: Built-in SOCKS5 proxy for network pivoting.
  shell:
    command: shell
    parameters:
      command: "<shell command>"
    notes: Execute shell commands (cmd.exe/bash).
file_upload_pattern: |
  Same as Apollo:
  1. Check Mythic file store for binary (get_all_uploaded_files)
  2. If not found, check Payload_Type/sage/tools/<binary_filename>
  3. Upload via upload_file_by_file_uuid → receive UUID
  4. Reference UUID in command parameters (assembly_file for .NET, bof_file for BOFs)
opsec_notes: |
  Athena's cross-platform design means it's compiled per-target-OS. The BOF runner
  (execute-bof) uses a clrmd-based loader — the same BOF API surface as Cobalt Strike's
  BOF runner. BOF execution is in-process and doesn't spawn child processes, reducing
  detection signal vs inline_assembly for the same tradecraft.
known_gaps:
  - native-exe (no generic EXE runner)
  - python-script (no Python runtime on Windows targets)
known_quirks: |
  Athena requires a per-platform build (Windows x64, Linux x64, macOS ARM, etc.).
  BOF arguments must be packed using Athena's BOF argument packing format (same as
  Cobalt Strike — uint32/uint16/short/int/binary blob). Some BOFs have Athena-specific
  wrappers in the AthenaPlugins repository for ease of use.
last_updated: 2026-05-29
---

# Athena

The cross-platform .NET Mythic agent, maintained by MythicAgents. Athena's key
differentiator from Apollo is **native BOF execution** via the `execute-bof` command —
enabling Cobalt Strike-compatible BOFs to run inside Athena without conversion. This
directly addresses Apollo's primary gap. Athena also ships with network discovery,
SOCKS5 proxy, and cross-platform support for Windows, Linux, and macOS.

## Execution model
- .NET assemblies execute in a disposable AppDomain (same as Apollo's inline_assembly)
- BOFs execute in-process via a managed BOF loader (compatible with Cobalt Strike BOF API)
- PowerShell via powershell_import
- Shellcode injection via shinject
- No generic native EXE runner

## Notable native commands
- `execute-bof` — run any Cobalt Strike-compatible BOF directly (THE key feature)
- `arp-scan` — built-in ARP network scanner
- `port-scan` — built-in TCP port scanner
- `socks` — built-in SOCKS5 proxy on a local port
- `shell` — execute shell commands
- `download` / `upload` — file transfer

## Upload workflow
For BOFs: same as Apollo assembly upload — upload the `.o` file to Mythic's file store,
then reference the UUID in execute-bof's bof_file parameter.

## OPSEC considerations
- BOF execution is in-process: no child process, reduced detection signal vs inline_assembly
- Cross-platform compilation: each target OS/arch requires the correct Athena binary
- Rename the Athena binary to something benign before deployment

## Known gaps
- **Native EXE**: no runner for plain Windows PEs (same as Apollo)
- **Python**: no Python runtime on Windows

## Why use Athena over Apollo?
Use Athena when:
1. BOF execution is required (nanodump, TrustedSec CS BOFs, any Cobalt Strike BOF)
2. Targeting Linux or macOS systems (Athena is cross-platform; Apollo is Windows-only)
3. Network pivoting via SOCKS5 is needed (built-in vs external proxy)

## See also
- Apollo — Windows-focused .NET agent with strong native command set
- Poseidon — Linux/macOS sibling of Apollo
- Merlin — Go-based cross-platform agent
