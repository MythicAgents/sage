---
name: GodPotato
category: privilege-escalation
subcategories: [seimpersonate, token-impersonation, local-pe]
tradecraft_tags: [seimpersonate, token-impersonation, iis, service-account, local-pe]
mitre_attack:
  - id: T1134.002
    name: Access Token Manipulation — Create Process with Token
source:
  url: https://github.com/BeichenDream/GodPotato
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: GodPotato-NET4.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  COM object instantiation (Sysmon event 10), DCOM loopback connections, and token
  impersonation events are the primary signals. EDR behavioral signatures for potato-family
  exploits detect token manipulation from service accounts. Spawned processes as SYSTEM
  from an IIS/service worker parent is a strong anomaly.
usage_examples:
  - description: Run a command as SYSTEM from a service account (SeImpersonatePrivilege)
    args: "-cmd 'cmd /c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: Add a local user as admin
    args: "-cmd 'net user backdoor P@ss123! /add && net localgroup administrators backdoor /add'"
  - description: Run a new shell
    args: "-cmd 'cmd.exe'"
opsec_notes: |
  GodPotato works on Windows Server 2012 through 2022+ and Windows 10/11. It uses a
  different COM coercion approach than older potato variants and targets the RPC endpoint.
  The spawned process will have a SYSTEM-level token but its parent will be the service
  account process — this parent/child relationship is anomalous and detected by behavioral
  EDRs (CrowdStrike, SentinelOne). Use the SYSTEM shell to further migrate to a cleaner
  parent process.
gotchas: |
  Requires SeImpersonatePrivilege or SeAssignPrimaryTokenPrivilege in the token.
  Service accounts (IIS AppPool, SQL Server, MSSQL$, network service) typically have
  these. Administrator accounts generally do NOT need this path. If SharpUp identifies
  `TokenPrivileges` → SeImpersonatePrivilege, GodPotato is the recommended next step.
  Multiple .NET version variants are available (NET2, NET4, NET35) — match to the
  .NET version available on target.
related_ttps: [printspoofer, juicypotatong, sharpup, standin]
alternatives: [printspoofer, juicypotatong, sweetpotato]
common_args:
  -cmd:
    name: -cmd
    description: Command to execute as SYSTEM
    typical_values: ["cmd /c whoami", "cmd.exe", "powershell -enc ..."]
    required: true
last_updated: 2026-05-29
---

# GodPotato

The latest evolution of the Potato-family SeImpersonatePrivilege exploits. GodPotato
achieves SYSTEM by abusing COM/DCOM impersonation to capture a SYSTEM-context token
from a background Windows service, then impersonating it. Works on Windows Server 2012
through 2022 and Windows 10/11 — broader compatibility than PrintSpoofer or JuicyPotatoNG.
The name reflects it being the "god" potato — working across all recent Windows versions.

## Typical use cases
- Escalate from service account (IIS, MSSQL, network service) to SYSTEM
- Any context with SeImpersonatePrivilege and/or SeAssignPrimaryTokenPrivilege
- Post-RBCD or Kerberos relay cleanup: already at privileged domain account but need SYSTEM
- Exploitation after finding `TokenPrivileges → SeImpersonatePrivilege` in SharpUp output

## How Sage uses this
Sage reaches for GodPotato when SharpUp (or Seatbelt TokenPrivileges check) confirms
SeImpersonatePrivilege is present in the current token. This is common in:
- IIS-hosted web app RCE → GodPotato → SYSTEM
- MSSQL sa-level code execution → GodPotato → SYSTEM
- Any xp_cmdshell or similar service-account execution context

## Output
The specified command executes as NT AUTHORITY\SYSTEM. Output depends on the command.
GodPotato itself prints minimal console output; process creation is confirmed by the
spawned process running.

## Full Reference

> Captured against GodPotato v1.2, 2026-05-29. Source: https://github.com/BeichenDream/GodPotato README.

### Available binaries

| Binary | Target .NET version |
|--------|-------------------|
| `GodPotato-NET2.exe` | .NET 2.0 / 3.5 |
| `GodPotato-NET35.exe` | .NET 3.5 |
| `GodPotato-NET4.exe` | .NET 4.x (most common) |

### Argument

| Arg | Description |
|-----|-------------|
| `-cmd X` | Command to execute as SYSTEM (required) |

### Windows version compatibility

| Version | Compatible |
|---------|------------|
| Windows Server 2012 | Yes |
| Windows Server 2016 | Yes |
| Windows Server 2019 | Yes |
| Windows Server 2022 | Yes |
| Windows 10 | Yes |
| Windows 11 | Yes |

### Source for this reference

- https://github.com/BeichenDream/GodPotato (README)
- Version: v1.2 as of 2026-05-29
