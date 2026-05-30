---
name: SharpKnife
category: lateral-movement
subcategories: [remote-execution, multi-mode, dotnet-exec]
tradecraft_tags: [lateral-movement, remote-exec, dotnet, apollo-runnable, multi-mode]
mitre_attack:
  - id: T1047
    name: Windows Management Instrumentation
source:
  url: https://github.com/uknowsec/SharpKnife
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpKnife.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Same as SharpExec/SharpWMI — authentication events and WMI/service process creation
  on the target. No additional signals.
usage_examples:
  - description: WMI remote execution
    args: "SharpKnife.exe -m wmi -i TARGET -u DOMAIN\\\\admin -p Password123 -c 'whoami > C:\\Windows\\Temp\\out.txt'"
  - description: SMBExec (service-based)
    args: "SharpKnife.exe -m smb -i TARGET -u DOMAIN\\\\admin -p Password123 -c 'whoami'"
opsec_notes: |
  SharpKnife is similar to SharpExec — another multi-mode lateral movement .NET assembly.
  Not actively maintained. SharpExec is preferred (better documented, more modes).
  Documented for completeness as it appears in SharpCollection.
gotchas: |
  Not actively maintained. SharpExec and SharpMove provide equivalent or better functionality.
  Use SharpExec as the primary multi-mode lateral movement tool; SharpKnife as a backup
  if SharpExec is specifically blocked.
related_ttps: [sharpexec, sharpmove, sharpwmi]
alternatives: [sharpexec, sharpmove]
common_args:
  -m:
    description: Execution method
    typical_values: [wmi, smb, winrm]
    required: true
  -i:
    description: Target IP or hostname
    typical_values: ["192.168.56.22"]
    required: true
  -u:
    description: Username (DOMAIN\\user)
    typical_values: ["NORTH\\\\administrator"]
  -p:
    description: Password
    typical_values: ["Password123"]
  -c:
    description: Command to execute
    typical_values: ["whoami > C:\\\\Windows\\\\Temp\\\\out.txt"]
last_updated: 2026-05-29
---

# SharpKnife

Another multi-mode lateral movement .NET assembly. Provides WMI, SMBExec, and WinRM
remote execution modes. SharpExec is the preferred alternative (better documented,
more execution modes). SharpKnife is documented as it appears in the SharpCollection
repository.

## When to Use
SharpKnife is a backup option when SharpExec is blocked or fails. Functionally similar.
