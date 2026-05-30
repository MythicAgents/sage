---
name: SharpRDP
category: lateral-movement
subcategories: [rdp, lateral-movement, remote-exec, pass-the-hash-rdp]
tradecraft_tags: [rdp, lateral-movement, restricted-admin, pth-rdp, dotnet]
mitre_attack:
  - id: T1021.001
    name: Remote Services — Remote Desktop Protocol
source:
  url: https://github.com/0xthirteen/SharpRDP
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpRDP.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  RDP connections generate Event 4624 (logon type 10 — remote interactive) on the target,
  Event 1149 in Terminal Services log (remote connection attempted), and Sysmon Event 3
  (network connection). Automated RDP lateral movement (no human interaction) may be
  detectable by behavioral analytics distinguishing automated from manual RDP sessions.
usage_examples:
  - description: Execute a command on a remote host via RDP
    args: "SharpRDP.exe computername=WINTERFELL command='cmd.exe /c whoami > C:\\Windows\\Temp\\out.txt' username=NORTH\\\\administrator password=Password123"
  - description: Execute via Restricted Admin Mode (pass-the-hash)
    args: "SharpRDP.exe computername=WINTERFELL command='cmd.exe /c whoami' username=administrator restricted=true"
  - description: Send keystrokes to an existing RDP session
    args: "SharpRDP.exe computername=WINTERFELL command='powershell -enc <base64>'"
opsec_notes: |
  SharpRDP uses .NET's RDP client (mstscax.dll) to open an RDP connection and send
  keystrokes — not a traditional remote-exec approach. Restricted Admin Mode allows
  pass-the-hash RDP without cleartext credentials (requires target to have Restricted
  Admin Mode enabled). RDP lateral movement is typically noisier than WMI or SMB.
gotchas: |
  Restricted Admin Mode must be enabled on the target for pass-the-hash RDP
  (`reg add HKLM\System\CurrentControlSet\Control\Lsa /v DisableRestrictedAdmin /t REG_DWORD /d 0`).
  SharpRDP is not actively maintained. The RDP session creates a full Windows session —
  this consumes a remote desktop CAL and may disconnect an existing session if the target
  has limited RDP seats. More detectable than WMI/SMB-based lateral movement.
related_ttps: [impacket-wmiexec, crackmapexec, sharpwmi]
alternatives: [impacket-rdp, xfreerdp-pth, apollo-shell]
common_args:
  computername:
    description: Target computer hostname or IP
    typical_values: ["WINTERFELL", "192.168.56.22"]
    required: true
  command:
    description: Command to execute on the remote system
    typical_values: ["cmd.exe /c whoami"]
    required: true
  username:
    description: Username for RDP authentication
    typical_values: ["NORTH\\\\administrator", "administrator"]
    required: true
  password:
    description: Password for authentication
    typical_values: ["Password123"]
  restricted:
    description: Use Restricted Admin Mode (no password needed; uses current token)
    typical_values: [true, false]
last_updated: 2026-05-29
---

# SharpRDP

A .NET tool for RDP-based command execution. SharpRDP uses the Windows RDP client
library to open an RDP session to a target and send keystrokes to execute commands.
Supports Restricted Admin Mode for pass-the-hash RDP lateral movement.

## Typical use cases
- RDP-based lateral movement when SMB/WMI are blocked
- Pass-the-hash via Restricted Admin Mode (if enabled on target)
- Execute commands via RDP as a different credential context

## How Sage uses this
SharpRDP is a fallback lateral movement method when WMI (SharpWMI) and SMB-based
execution (CrackMapExec) are blocked. RDP is less commonly filtered than SMB.

## Output
Command output must be redirected to a file and then downloaded separately.
