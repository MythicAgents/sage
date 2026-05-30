---
name: RunasCs
category: privilege-escalation
subcategories: [credential-reuse, token-creation, alternate-user-execution]
tradecraft_tags: [runas, credential-reuse, alternate-credentials, logon-session, dotnet]
mitre_attack:
  - id: T1134.003
    name: Access Token Manipulation — Make and Impersonate Token
source:
  url: https://github.com/antonioCoco/RunasCs
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: RunasCs.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  Creates a new logon session with explicit credentials — Event 4624 (logon type 2 or 9
  depending on mode). Sysmon tracks process creation with new credential material.
  The spawned process has a different user context visible in process audit logs.
usage_examples:
  - description: Run a command as a different user with known password
    args: "RunasCs.exe administrator P@ssw0rd 'cmd.exe /c whoami'"
  - description: Run with a network logon (type 3, less visible on local system)
    args: "RunasCs.exe administrator P@ssw0rd 'cmd.exe /c whoami' -l 9"
  - description: Bypass restricted token (UAC bypass path)
    args: "RunasCs.exe administrator P@ssw0rd 'cmd.exe' --bypass-uac"
opsec_notes: |
  RunasCs creates a visible logon event but is useful when Apollo's `make_token` is
  insufficient (make_token creates a network-only session; RunasCs runs a full interactive
  or service-level logon). Compare with Apollo's native `make_token` command — `make_token`
  creates a type-9 (new credentials, network only) logon which is less visible than RunasCs.
  Use `make_token` first; RunasCs when full local logon is required.
gotchas: |
  Apollo's native `make_token` command does the same thing as RunasCs for most use cases
  and is preferred (no binary upload needed). RunasCs is useful when:
  - Full interactive logon is required (GUI, full registry load)
  - UAC bypass is needed
  - Specific logon types are needed
  Otherwise, use Apollo's `make_token`.
related_ttps: [mimikatz, sharpsploit-tokens, sharpup]
alternatives: [apollo-make-token, mimikatz-pth]
common_args:
  username:
    description: Username to run as
    typical_values: ["administrator", "DOMAIN\\\\user"]
    required: true
  password:
    description: Password for the user
    typical_values: ["P@ssw0rd"]
    required: true
  command:
    description: Command to execute as the specified user
    typical_values: ["cmd.exe /c whoami"]
    required: true
  -l:
    description: Logon type (2=interactive, 3=network, 9=new-creds-only)
    typical_values: [2, 3, 9]
  --bypass-uac:
    description: Attempt UAC bypass when running as a UAC-restricted admin
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# RunasCs

A .NET alternative to Windows' built-in `runas.exe` with additional logon type control
and UAC bypass capability. RunasCs creates a new logon session with specified credentials
and runs a command in that context. The key advantage over Apollo's `make_token`: RunasCs
creates a full logon session (not just network-credentials), enabling local resource access
with the new identity.

## Typical use cases
- Execute commands as a different user when the password is known
- Full interactive logon (vs make_token's network-only session)
- UAC bypass when running as a local admin whose token is restricted

## How Sage uses this
Apollo's `make_token` is preferred for most credential-reuse scenarios. RunasCs is the
fallback when a full logon session (not just network credentials) is needed, or when
UAC bypass is required for a known admin account.

## Output
Process output of the executed command, returned to Apollo.
