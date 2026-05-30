---
name: SharpExec
category: lateral-movement
subcategories: [smb-exec, wmi-exec, scheduled-task-exec, winrm-exec]
tradecraft_tags: [lateral-movement, smb, wmi, scheduled-task, winrm, dotnet, apollo-runnable]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
  - id: T1047
    name: Windows Management Instrumentation
  - id: T1053.005
    name: Scheduled Task/Job — Scheduled Task
source:
  url: https://github.com/anthemtotheego/SharpExec
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: SharpExec.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Each execution mode has its own detection signal: SMBExec (service install Event 7045),
  WMIExec (WmiPrvSE.exe child process), ScheduledTask (Event 4698 task creation +
  4702 run), PSExec (service install). The key advantage over impacket equivalents:
  SharpExec runs from within Apollo via inline_assembly — no Python infrastructure needed.
usage_examples:
  - description: WMI command execution on remote host
    args: "SharpExec.exe -m=wmiexec -i=192.168.56.22 -u=administrator -p=Password123 -d=NORTH -c='cmd.exe /c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: SMBExec (service-based, ADMIN$ share)
    args: "SharpExec.exe -m=smbexec -i=192.168.56.22 -u=administrator -p=Password123 -d=NORTH -c='net user backdoor P@ss123 /add'"
  - description: Scheduled task execution and cleanup
    args: "SharpExec.exe -m=scheduledtask -i=192.168.56.22 -u=administrator -p=Password123 -d=NORTH -c='cmd.exe' -a='/c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: WinRM execution (requires WinRM enabled on target)
    args: "SharpExec.exe -m=winrm -i=192.168.56.22 -u=administrator -p=Password123 -d=NORTH -c='whoami'"
opsec_notes: |
  SharpExec's primary advantage is being a .NET assembly — Apollo can use it via
  inline_assembly without Python infrastructure. Detection profiles match equivalent
  impacket tools. WMI execution is the quietest (no service install). Scheduled
  task execution creates a scheduled task artifact that must be cleaned up.
gotchas: |
  Each mode has different requirements:
  - wmiexec: WMI/DCOM reachable, admin on target
  - smbexec: ADMIN$ share accessible, service install rights
  - scheduledtask: Task Scheduler accessible, admin on target, leaves artifact
  - winrm: WinRM enabled, WS-Management accessible (TCP 5985/5986)
  Credentials must be provided explicitly — no current-token pass-through (use
  Apollo's make_token first for token-based lateral movement, or use Kerberos tickets
  via SharpMapExec which supports /kerberos mode).
related_ttps: [sharpwmi, sharp-mapexec, impacket-wmiexec, crackmapexec]
alternatives: [sharpwmi, sharp-mapexec, crackmapexec]
common_args:
  -m:
    description: Execution method
    typical_values: [wmiexec, smbexec, scheduledtask, winrm, psexec]
    required: true
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
    required: true
  -d:
    description: Domain
    typical_values: ["NORTH", "north.sevenkingdoms.local"]
  -c:
    description: Command to execute
    typical_values: ["cmd.exe /c whoami > C:\\\\Windows\\\\Temp\\\\out.txt"]
    required: true
  -a:
    description: Arguments for the command (for scheduled task mode)
    typical_values: ["/c whoami"]
last_updated: 2026-05-29
---

# SharpExec

A .NET assembly multi-mode lateral movement tool — the Apollo-compatible equivalent
of impacket's wmiexec/smbexec/psexec suite. SharpExec provides WMI, SMB-service,
scheduled task, WinRM, and PSExec-style remote execution in a single .NET assembly
runnable via Apollo's inline_assembly.

## Typical use cases
- Windows-to-Windows lateral movement without Python infrastructure
- Multi-mode execution with a single tool (avoids separate binary for each protocol)
- Post-PTT/PTH lateral movement when make_token has been used for credential material

## How Sage uses this
SharpExec is the Apollo-native lateral movement toolkit. When Sage has credentials
(from PTH, PTT, or cleartext) and needs to execute on remote hosts, SharpExec via
inline_assembly avoids Python infrastructure while providing equivalent capability
to impacket's suite.

## Detection preference order (quietest to loudest)

1. **winrm** — no service install, uses WS-Management
2. **wmiexec** — WmiPrvSE.exe parent (anomalous child processes)
3. **scheduledtask** — artifact (task must be cleaned up)
4. **smbexec** — no dropped binary but service install (Event 7045)
5. **psexec** — service + binary drop (most detected)

## Output
Command output from the remote execution returned to the Apollo agent.
