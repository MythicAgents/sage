---
name: SharpWMI
category: lateral-movement
subcategories: [wmi-execution, remote-execution, event-subscription]
tradecraft_tags: [wmi, lateral-movement, remote-exec, dotnet, ghostpack-adjacent]
mitre_attack:
  - id: T1047
    name: Windows Management Instrumentation
  - id: T1546.003
    name: Event Triggered Execution — Windows Management Instrumentation Event Subscription
source:
  url: https://github.com/GhostPack/SharpWMI
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: SharpWMI.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: local-admin
network_required: true
detection_signal: |
  WMI process creation events (Sysmon event 20/21 for WmiEvent, Event 4688 for process
  creation). WMI event subscriptions are persistent and generate Event 5861 when added.
  Remote WMI connections generate authentication events on the target. EDR behavioral
  signatures for WmiPrvSE.exe child processes.
usage_examples:
  - description: Remote WMI command execution
    args: "action=exec computername=192.168.56.22 username=NORTH\\\\administrator password=Password123 command='cmd.exe /c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: Local WMI command execution
    args: "action=exec command='cmd.exe /c whoami'"
  - description: Create a WMI event subscription for persistence
    args: "action=executevbs computername=192.168.56.22 username=NORTH\\\\administrator password=Password123 command='malicious_script.vbs' eventname='SystemCheck' interval=300"
  - description: List WMI event subscriptions
    args: "action=query computername=192.168.56.22 username=NORTH\\\\administrator password=Password123 query='SELECT * FROM __EventFilter'"
opsec_notes: |
  .NET assembly runnable via Apollo inline_assembly. Remote WMI execution is detectable
  by behavioral EDR (WmiPrvSE.exe child processes are anomalous). WMI event subscriptions
  are persistent across reboots — clean up after use. Consider WMI as a lateral movement
  primitive when SMB-based movement is blocked or logged.
gotchas: |
  WMI lateral movement requires DCOM/WMI ports (TCP 135 + dynamic RPC) to be accessible.
  WMI event subscriptions for persistence require admin and leave Registry artifacts.
  Remote WMI requires credentials in DOMAIN\user format or explicit domain specification.
  Lateral movement creates a WmiPrvSE.exe child process on the target — high detection signal.
  A target-side DistributedCOM Event 10036 after a valid elevated network logon means
  DCOM activation authentication was below packet integrity; it is not by itself a bad-password
  or WMI namespace ACL signal. Apollo's native explicit-credential `wmiexecute` branch can hit
  this on hardened targets; use `make_token`, then passwordless `wmiexecute`, prove via readback,
  and `rev2self`.
related_ttps: [seatbelt, sharphound, sharpersist]
alternatives: [impacket-wmiexec, crackmapexec-wmi]
common_args:
  action:
    description: Action to perform
    typical_values: [exec, executevbs, query, query_list, delete]
    required: true
  computername:
    description: Remote target hostname or IP
    typical_values: ["192.168.56.22", "WINTERFELL"]
  username:
    description: Authentication username (DOMAIN\\\\user format)
    typical_values: ["NORTH\\\\administrator"]
  password:
    description: Authentication password
    typical_values: ["Password123"]
  command:
    description: Command to execute (for exec action)
    typical_values: ["cmd.exe /c whoami"]
last_updated: 2026-07-11
---

# SharpWMI

GhostPack's .NET assembly for WMI-based remote command execution and WMI event subscription
management. Unlike impacket-wmiexec (Python/infrastructure), SharpWMI runs directly
from Apollo via inline_assembly for Windows-to-Windows lateral movement. Supports both
local and remote WMI execution, and WMI event subscriptions for persistence.

## Typical use cases
- Windows-to-Windows lateral movement via WMI (no external infrastructure needed)
- WMI event subscriptions as a stealthy persistence alternative
- WMI query execution on remote systems for enumeration

## How Sage uses this
SharpWMI is the Apollo-compatible WMI lateral movement tool. When Sage needs to execute
commands on another Windows machine from an Apollo-compromised host, SharpWMI via
inline_assembly avoids needing to drop additional tools.

## Output
Command execution output captured to a file on the target (use `> output.txt` and then
download). WMI event subscription operations report success/failure to stdout.

## DCOM Hardening Triage

When remote WMI returns `0x80070005`, separate authentication from activation:

1. Confirm the target logged a successful elevated Type 3 logon for the intended account.
2. Check the target System log for DistributedCOM Event 10036 at the same timestamp.
3. If Event 10036 is present, the client activation request was below
   `RPC_C_AUTHN_LEVEL_PKT_INTEGRITY`; changing the password or WMI namespace ACL is the wrong fix.
4. For Apollo native WMI, use a NetOnly token first and omit explicit credential fields from
   `wmiexecute` so the agent takes its current-token COM activation path.

Apply that same transport choice to WMI-backed follow-on operations such as remote CA export;
an explicit-credential retry can recreate the same DCOM activation failure.

Always verify a target-side proof artifact and revert the temporary token context after readback.

## Public Source Trail

Microsoft KB5004442 documents the relevant DCOM hardening signal: Event 10036 is emitted when a client
tries to activate a DCOM server below `RPC_C_AUTHN_LEVEL_PKT_INTEGRITY`. Microsoft also documents that
`System.Management.ConnectionOptions.Authentication` controls the COM authentication level used for a
WMI connection.

Apollo's public `wmiexecute.cs` source confirms that it has two materially different remote paths:
the explicit-credential path uses `System.Management.ManagementScope` plus `ConnectionOptions`, while
the current-token path uses direct COM activation and applies `CoSetProxyBlanket(..., PKT_PRIVACY, ...)`.
In this lab the explicit-credential path still produced Event 10036 while the token-backed path succeeded.
The public sources explain the mechanism and the branch split, but I did not find a public Apollo-specific
write-up that explains that exact failure pair; keep that part labeled as lab-observed behavior.
