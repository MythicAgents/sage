---
name: Outflank CS-Remote-OPs BOFs
category: lateral-movement
subcategories: [bof-collection, remote-operations, token-manipulation, persistence]
tradecraft_tags: [bof, outflank, cobalt-strike, remote-ops, token, registry, scm, athena]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
  - id: T1547.001
    name: Boot or Logon Autostart Execution — Registry Run Keys
  - id: T1543.003
    name: Create or Modify System Process — Windows Service
source:
  url: https://github.com/outflanknl/C2-Tool-Collection
  license: GPL-3.0
  maintained: true
binary_type: bof
binary_filename: (per-BOF .x64.o files)
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  BOF execution is in-process — no child process for the operations themselves.
  Specific operations (service creation via SCM, registry modification for persistence)
  generate their own audit events (Event 7045 for service creation, Event 13 for registry
  writes in Sysmon), but without a standalone process doing the work. Token operations
  generate logon session events.
usage_examples:
  - description: Steal a token from another process (in-process, no separate tokenplayer)
    args: "execute-bof BOF_TokenStealer.x64.o <pid>"
  - description: Enumerate running services on a remote host via SCM
    args: "execute-bof BOF_SCMEnum.x64.o <target_host>"
  - description: Add registry Run key persistence in-process
    args: "execute-bof BOF_RegistryPersist.x64.o"
  - description: Execute a .NET assembly inside a remote process (BOF-based assembly injection)
    args: "execute-bof BOF_ExecuteAssembly.x64.o <pid> <assembly_b64>"
  - description: List WMI subscriptions for persistence enumeration
    args: "execute-bof BOF_WMIPersist.x64.o"
opsec_notes: |
  Outflank's Remote-OPs BOF collection provides offensive operations that typically
  require spawning child processes or loading additional tools — executed entirely
  in-process instead. The ExecuteAssembly BOF is particularly valuable: it runs a
  .NET assembly inside a BOF context (inside the C2 agent's process), bypassing the
  standard inline_assembly AppDomain pattern that some EDRs detect.
gotchas: |
  Apollo has no BOF runner — use Athena's `execute-bof` command. These BOFs are from
  the C2-Tool-Collection repository (the newer home of Outflank's BOF work). BOF
  argument passing in Athena uses a different packing format than Cobalt Strike's
  `bof_pack` — verify Athena-compatible argument wrappers are available. Some BOFs
  in this collection use undocumented Windows internals and may break on minor
  Windows updates; test before operational use.
related_ttps: [trustedsec-bofs, bofnet, nanodump, seatbelt]
alternatives: [sharpsploit-tokens, sharpwmi, sharpersist]
common_args:
  BOF_TokenStealer:
    name: BOF_TokenStealer
    description: Steal the primary token of a target PID in-process (no OpenProcess from a child process)
    typical_values: ["<PID>"]
  BOF_SCMEnum:
    name: BOF_SCMEnum
    description: Enumerate services on a local or remote host via Service Control Manager
    typical_values: ["<hostname or blank for local>"]
  BOF_ExecuteAssembly:
    name: BOF_ExecuteAssembly
    description: Run a .NET assembly inside the BOF context within the C2 agent's process
    typical_values: ["<pid> <base64-assembly>"]
  BOF_RegistryPersist:
    name: BOF_RegistryPersist
    description: Add/remove HKCU Run key persistence via in-process registry API call
    typical_values: [flag-only]
  BOF_WMIPersist:
    name: BOF_WMIPersist
    description: Enumerate or create WMI event subscriptions for persistence
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Outflank CS-Remote-OPs BOFs

Outflank's collection of BOFs for remote offensive operations. The defining feature
of this collection vs TrustedSec's SA BOFs is the **remote operations** focus —
these BOFs perform actions that would normally require spawning child processes or
running separate tools. ExecuteAssembly is the standout: running a .NET assembly
entirely within the BOF/C2 agent process is a materially stealthier execution model
than inline_assembly's AppDomain approach.

## Complete BOF Inventory

### Token Operations

| BOF | Description | Privilege | Detection avoided |
|-----|-------------|-----------|------------------|
| `BOF_TokenStealer` | Steal primary token from target PID in-process | user | No OpenProcess from child process |
| `BOF_TokenStalker` | Continuously monitor and steal tokens as they appear | user | Persistent token harvesting |
| `BOF_WhoAmI` | Detailed token info including groups, privileges, integrity | user | No whoami.exe spawn |

### Service and Remote Management

| BOF | Description | Privilege |
|-----|-------------|-----------|
| `BOF_SCMEnum` | Enumerate services via SCM (local or remote) | user/admin |
| `BOF_SCMModify` | Modify service binary path for service binary hijack | admin |
| `BOF_GetSPN` | Enumerate SPNs via LDAP in-process | domain-user |

### Execution and Injection

| BOF | Description | Privilege | Notes |
|-----|-------------|-----------|-------|
| `BOF_ExecuteAssembly` | Run .NET assembly inside BOF context (no AppDomain in new process) | user | **High-value: stealthier than inline_assembly** |
| `BOF_Inject` | Inject shellcode into a remote process from within the BOF | user | No separate injector process |
| `BOF_SpawnAs` | Spawn process with alternate credentials from within C2 agent | user | No runas.exe |

### Persistence

| BOF | Description | Privilege |
|-----|-------------|-----------|
| `BOF_RegistryPersist` | Add/remove HKCU Run key in-process | user |
| `BOF_ScheduledTaskPersist` | Create scheduled task via COM without schtasks.exe | admin |
| `BOF_WMIPersist` | Create/enumerate/delete WMI event subscriptions | admin |

### Enumeration

| BOF | Description | Privilege |
|-----|-------------|-----------|
| `BOF_ProcessList` | Process list with parent, user, integrity level | user |
| `BOF_HandleScanner` | Scan process handles for open handle types | user |
| `BOF_FindFile` | Filesystem search by pattern (in-process, no cmd.exe) | user |

## Why BOF_ExecuteAssembly Matters

Standard Apollo inline_assembly execution path:
```
Agent → CreateThread → New AppDomain → Load assembly → Execute → Unload AppDomain
```
Detection: AppDomain creation is a strong signal; EDR hooks .NET CLR loading.

BOF_ExecuteAssembly path:
```
Agent → execute-bof → Assembly runs inside agent's process memory
```
Detection: No separate AppDomain creation; assembly runs in existing CLR context.
The BOF approach is materially harder to attribute to "agent loading a tool."

## How Sage uses this

With Athena as the agent, Sage reaches for BOF_ExecuteAssembly when stealth is a
priority and inline_assembly's AppDomain pattern is a concern. BOF_TokenStealer
is the in-process alternative to Apollo's `steal_token` command.

## Apollo-specific note

Apollo has no BOF runner. These BOFs require Athena (`execute-bof`). For equivalent
Apollo-compatible operations, see: steal_token (token), inline_assembly (assembly exec).
