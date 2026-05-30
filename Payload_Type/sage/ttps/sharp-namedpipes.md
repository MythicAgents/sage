---
name: Named Pipe Token Impersonation
category: privilege-escalation
subcategories: [named-pipe, token-impersonation, local-pe, service-communication]
tradecraft_tags: [named-pipe, token-impersonation, impersonation, seimpersonate, local-pe]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
source:
  url: https://docs.microsoft.com/en-us/windows/win32/ipc/named-pipes
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Named pipe creation by a non-system process followed by a privileged process connecting
  to it is detectable by Sysmon event 17/18 (PipeEvent: Pipe Connected). Named pipe
  impersonation attempts are visible in security audit logs if pipe object access auditing
  is configured. EDR behavioral signatures for token impersonation from unusual processes.
usage_examples:
  - description: PrintSpoofer uses named pipe impersonation to escalate to SYSTEM
    args: "(see printspoofer.md)"
  - description: GodPotato uses COM-triggered pipe impersonation
    args: "(see godpotato.md)"
  - description: Manual pipe impersonation via SharpSploit or built-in Win32 API
    args: "(SeImpersonatePrivilege required; service creates pipe, tricks privileged service to connect)"
opsec_notes: |
  Named pipe impersonation is the underlying mechanism for most SeImpersonate exploits
  (PrintSpoofer, GodPotato, SweetPotato). For Apollo operators, the native `steal_token`
  and SeImpersonate exploits (GodPotato) provide this capability without needing to
  implement pipe impersonation directly.
gotchas: |
  This is a TECHNIQUE REFERENCE, not a standalone tool. Named pipe impersonation requires
  SeImpersonatePrivilege in the current token (service accounts have this). The pipe must
  be in a location writable by the current user that a privileged process will connect to.
  Apollo's token manipulation commands (steal_token, make_token) provide similar outcomes
  through different primitives.
related_ttps: [godpotato, printspoofer, sweetpotato, sharpup]
alternatives: [godpotato, printspoofer]
common_args: {}
last_updated: 2026-05-29
---

# Named Pipe Token Impersonation

The technique underlying SeImpersonatePrivilege exploitation (PrintSpoofer, GodPotato,
SweetPotato). A process with SeImpersonatePrivilege creates a named pipe and tricks a
higher-privileged service into connecting to it. Upon connection, the attacker calls
`ImpersonateNamedPipeClient()` to impersonate the connecting service's token (typically SYSTEM).

## How This Works

```
1. Current process has SeImpersonatePrivilege (service account / IIS / MSSQL)
2. Create a named pipe at a predictable location
3. Trigger a privileged service to connect to the pipe
   - PrintSpoofer: uses the Print Spooler's name resolution
   - GodPotato: uses DCOM activation with SYSTEM-running COM object
   - SweetPotato: multiple trigger mechanisms
4. Call ImpersonateNamedPipeClient() → obtain SYSTEM token
5. Call CreateProcessWithTokenW() with the SYSTEM token → SYSTEM process
```

## Apollo Usage

For this technique, use Apollo's native `printspoofer` command or upload GodPotato/SweetPotato.
Apollo's `steal_token` can also steal a SYSTEM token from an existing process if one is
accessible. Named pipe impersonation is documented here for conceptual reference.

## Why SeImpersonatePrivilege Matters

SharpUp's `TokenPrivileges` check identifies if this privilege is present. If it is,
the escalation path is clear: GodPotato → SYSTEM in seconds.
