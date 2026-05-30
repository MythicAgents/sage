---
name: SharpCOM
category: lateral-movement
subcategories: [dcom-exec, com-lateral-movement, wmi-alternative]
tradecraft_tags: [com, dcom, lateral-movement, remote-execution, dotnet, apollo-runnable]
mitre_attack:
  - id: T1021.003
    name: Remote Services — Distributed Component Object Model
source:
  url: https://github.com/rasta-mouse/SharpCOM
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpCOM.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  DCOM execution triggers COM object activation events, which are generally less
  commonly scrutinized than WMI or SMB-based execution. Remote DCOM activation generates
  Event 4776 (NTLM authentication) or Kerberos auth events plus the COM activation
  from the DCOM server (dllhost.exe on the remote host). Some EDR behavioral rules
  flag suspicious DCOM parent processes (dllhost.exe spawning cmd.exe, etc.).
usage_examples:
  - description: Remote DCOM execution via MMC20 object
    args: "SharpCOM.exe -target 192.168.56.22 -username NORTH\\\\admin -password Password123 -command 'cmd.exe /c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: DCOM execution via ShellWindows object
    args: "SharpCOM.exe -target 192.168.56.22 -method shellwindows -command 'cmd.exe /c whoami'"
opsec_notes: |
  DCOM lateral movement (MMC20.Application, ShellWindows, ShellBrowserWindow) is a
  different execution path than WMI or SMB service. The resulting process on the target
  runs under dllhost.exe (COM surrogate) rather than WmiPrvSE.exe — a different parent
  process that some rules don't cover. Not actively maintained. SharpMove's dcom action
  provides equivalent capability with more maintenance.
gotchas: |
  DCOM requires the target to have DCOM enabled and the specific COM object to be
  registered. MMC20.Application works on most machines with MMC installed. ShellWindows
  requires a user session on the target. Remote DCOM requires DCOM ports (TCP 135 +
  dynamic RPC ports) to be accessible.
related_ttps: [sharpmove, sharpexec, sharpwmi]
alternatives: [sharpmove-dcom, impacket-dcomexec]
common_args:
  -target:
    description: Target IP or hostname
    typical_values: ["192.168.56.22"]
    required: true
  -username:
    description: Username (DOMAIN\\user)
    typical_values: ["NORTH\\\\administrator"]
    required: true
  -password:
    description: Password
    typical_values: ["Password123"]
    required: true
  -command:
    description: Command to execute on target
    typical_values: ["cmd.exe /c whoami > C:\\\\Windows\\\\Temp\\\\out.txt"]
    required: true
  -method:
    description: DCOM method to use
    typical_values: [mmc20, shellwindows, shellbrowserwindow]
last_updated: 2026-05-29
---

# SharpCOM

A .NET assembly for DCOM-based lateral movement. Uses COM objects (MMC20.Application,
ShellWindows) to execute commands on remote hosts — a different execution primitive than
WMI or SMB service methods. SharpMove's dcom action is the better-maintained equivalent;
SharpCOM is documented for completeness.

## DCOM Execution Objects

| COM Object | CLSID | Requirement | Notes |
|-----------|-------|-------------|-------|
| MMC20.Application | 49B2791A-B1AE-4C90-9B8E-E860BA07F889 | MMC installed | Most common |
| ShellWindows | 9BA05972-F6A8-11CF-A442-00A0C90A8F39 | Explorer running | Needs user session |
| ShellBrowserWindow | C08AFD90-F2A1-11D1-8455-00A0C91F3880 | Explorer running | Needs user session |

## How Sage uses this
SharpCOM provides DCOM lateral movement as an Apollo inline_assembly operation.
Preferred when WMI is monitored more closely than DCOM and a different parent process
(dllhost.exe vs WmiPrvSE.exe) reduces detection.
