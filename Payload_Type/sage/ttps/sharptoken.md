---
name: SharpToken
category: privilege-escalation
subcategories: [token-manipulation, impersonation, process-token, seimpersonate]
tradecraft_tags: [token, impersonation, seimpersonate, process-enum, dotnet, apollo-runnable]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
source:
  url: https://github.com/BeichenDream/SharpToken
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpToken.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Token enumeration generates process-access events for each process whose token is read.
  Token impersonation from service accounts generates Windows logon events. Behavioral
  EDR watches for unusual impersonation patterns from non-system processes.
usage_examples:
  - description: Enumerate all accessible process tokens
    args: "SharpToken.exe"
  - description: Enumerate tokens with specific privilege
    args: "SharpToken.exe SeImpersonatePrivilege"
  - description: Impersonate a specific process's token and run a command
    args: "SharpToken.exe impersonate <PID> cmd.exe"
opsec_notes: |
  SharpToken is a focused .NET token enumerator/impersonator. Apollo's native
  steal_token command provides equivalent impersonation capability without a binary
  upload. SharpToken's primary value-add is the enumeration function — listing all
  accessible tokens with their privilege details, which Apollo's native commands
  don't expose as cleanly. Compare with Seatbelt's TokenPrivileges check (current
  process only) vs SharpToken (all processes).
gotchas: |
  Not actively maintained. Apollo's steal_token command is preferred for actual
  impersonation. SharpToken is useful for its enumeration output — understanding which
  processes have high-value tokens (SYSTEM, DA sessions, etc.) before choosing a
  steal_token target.
related_ttps: [sharpup, seatbelt, outflank-remote-ops-bofs, sharp-token-handler-bof]
alternatives: [apollo-steal-token, mimikatz-pth, sharpsploit-tokens]
common_args:
  privilege_filter:
    description: Filter processes by required privilege (optional)
    typical_values: ["SeImpersonatePrivilege", "SeDebugPrivilege"]
  impersonate:
    description: Impersonate a specific PID's token
    typical_values: ["<PID> cmd.exe"]
last_updated: 2026-05-29
---

# SharpToken

A .NET assembly for enumerating process tokens and optionally impersonating them.
Extends beyond Seatbelt's TokenPrivileges (which only shows the current process)
by enumerating tokens across all accessible processes — identifying high-value
impersonation targets (SYSTEM tokens, domain admin sessions).

## Typical use cases
- Enumerate all process tokens to find SYSTEM or DA session tokens for steal_token
- Identify which processes have specific privileges (SeImpersonatePrivilege, SeDebugPrivilege)
- Verify which token would provide the highest privilege before impersonation

## How Sage uses this
Pre-steal_token reconnaissance: before Apollo steal_token, run SharpToken to identify
the highest-value accessible token. Then steal_token on the most appropriate PID.

## Output
Table of accessible processes with their user context, integrity level, and
privilege set. Impersonation mode executes the specified command in the target
process's token context.
