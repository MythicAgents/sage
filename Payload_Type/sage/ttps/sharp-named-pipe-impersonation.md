---
name: Sharp Named Pipe Impersonation
category: privilege-escalation
subcategories: [named-pipe, token-impersonation, seimpersonate]
tradecraft_tags: [named-pipe, impersonation, seimpersonate, dotnet, apollo-runnable]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
source:
  url: https://github.com/antonioCoco/RoguePotato
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpNamedPipeImpersonation.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Named pipe creation followed by another process connecting — Sysmon Events 17/18
  (PipeEvent: Named Pipe Connected). Token impersonation from service account context.
  Behavioral signatures for potato-family exploit patterns.
usage_examples:
  - description: Create a named pipe, wait for SYSTEM to connect, impersonate
    args: "SharpNamedPipeImpersonation.exe -pipeName malicious_pipe -command 'cmd.exe /c whoami'"
opsec_notes: |
  Named pipe impersonation is the underlying mechanism for GodPotato, PrintSpoofer,
  and SweetPotato. SharpNamedPipeImpersonation provides explicit control over the
  named pipe creation and impersonation — useful for understanding the mechanism or
  in edge cases where the automated potato tools fail.
  Apollo's native printspoofer command covers this. For most scenarios, GodPotato
  (with its automatic DCOM coercion) is more reliable.
gotchas: |
  Requires SeImpersonatePrivilege. The named pipe approach requires waiting for a
  SYSTEM process to connect — in automated potato exploits this is triggered by DCOM.
  In manual mode, you need to trigger a specific SYSTEM process to connect to the pipe.
  GodPotato automates this — prefer it.
related_ttps: [godpotato, printspoofer, sweetpotato, sharpup]
alternatives: [godpotato, printspoofer]
common_args:
  -pipeName:
    description: Named pipe path to create
    typical_values: ["malicious_pipe"]
    required: true
  -command:
    description: Command to execute after impersonation
    typical_values: ["cmd.exe /c whoami"]
    required: true
last_updated: 2026-05-29
---

# Sharp Named Pipe Impersonation

A .NET assembly demonstrating the named pipe impersonation technique — creating a
named pipe, waiting for a privileged process to connect, and impersonating its token.
This is the underlying primitive used by GodPotato, PrintSpoofer, and SweetPotato.

For operational use: **use GodPotato**. This tool is documented for educational
understanding of the mechanism and as a fallback for edge cases.
