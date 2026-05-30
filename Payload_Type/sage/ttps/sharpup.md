---
name: SharpUp
category: privilege-escalation
subcategories: [privesc-audit, misconfiguration-discovery]
tradecraft_tags: [privesc, enumeration, service-abuse, unquoted-path, alwaysinstallelevated, ghostpack]
mitre_attack:
  - id: T1574.005
    name: Hijack Execution Flow — Executable Installer File Permissions Weakness
  - id: T1574.009
    name: Hijack Execution Flow — Path Interception by Unquoted Path
source:
  url: https://github.com/GhostPack/SharpUp
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: SharpUp.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  SharpUp reads service configuration via WMI and the SCM, queries registry keys
  (AlwaysInstallElevated, HKLM/HKCU Run), and checks file ACLs. Each individual query
  is benign; the combination is detectable by behavioral analytics. Sysmon captures
  the process creation + command line. Some EDRs string-match "SharpUp".
usage_examples:
  - description: Run all privilege escalation checks
    args: "audit"
  - description: Run specific check — unquoted service path
    args: "UnquotedServicePath"
  - description: Check modifiable service binaries
    args: "ModifiableServices"
  - description: Check AlwaysInstallElevated registry key
    args: "AlwaysInstallElevated"
  - description: Check scheduled task file permissions
    args: "ModifiableScheduledTaskFiles"
opsec_notes: |
  SharpUp is a passive enumeration tool — it reads service configs and file ACLs without
  modification. Detection signal is primarily from process creation and WMI enumeration
  patterns. Running `audit` (all checks) generates more WMI queries than individual
  targeted checks. Rename before upload; the string "SharpUp" is in some EDR blocklists.
gotchas: |
  SharpUp identifies *opportunities* — it does not exploit them. The output must be
  actioned by the operator (e.g. replacing a writable service binary, using msiexec
  for AlwaysInstallElevated). Some findings require specific conditions to be
  exploitable (e.g. the unquoted path must have a writable parent directory at the
  right position). Verify each finding manually before exploitation.
related_ttps: [seatbelt, godpotato, printspoofer, juicypotatong]
alternatives: [winpeas, watson, powerup]
common_args:
  audit:
    name: audit
    description: Run all available privilege escalation checks
    typical_values: [flag-only]
  UnquotedServicePath:
    description: Find services with unquoted paths containing spaces (writable directory check)
    typical_values: [flag-only]
  ModifiableServices:
    description: Find services where the current user can modify the binary path
    typical_values: [flag-only]
  AlwaysInstallElevated:
    description: Check if AlwaysInstallElevated is set in both HKLM and HKCU
    typical_values: [flag-only]
  ModifiableScheduledTaskFiles:
    description: Find scheduled tasks pointing to files the current user can modify
    typical_values: [flag-only]
  DomainGPPPassword:
    description: Check Group Policy Preferences for cleartext passwords
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpUp

GhostPack's local privilege escalation opportunity enumerator. SharpUp checks a host
for common Windows misconfiguration-based privesc vectors: unquoted service paths,
modifiable service binaries, AlwaysInstallElevated, weak scheduled task file permissions,
GPP passwords, and more. It is the .NET successor to PowerView's `Invoke-AllChecks`.

## Typical use cases
- Quick post-foothold scan for local privesc vectors before attempting LSASS access
- Identify modifiable service executables (writable-binary attack)
- Find unquoted service paths with writable parent directories
- Check AlwaysInstallElevated for MSI-based privesc
- Discover GPP cleartext passwords in SYSVOL (legacy domain policy)
- Find modifiable scheduled task file targets

## How Sage uses this
Sage runs SharpUp early in the post-foothold phase to determine whether local privesc
is needed (and achievable without leaving the assembly). If SharpUp identifies a
modifiable service binary, Sage can chain into the exploitation step. If no local
vectors are found, Sage pivots to domain-level privesc (SharpHound → delegation/ADCS paths).

## Output
Text output listing each check name and findings. Checks with no vulnerabilities print
`[!] Not Found`. Vulnerable checks list the specific vector with details (service name,
binary path, modifiable parent directory, etc.).

## OPSEC considerations
Passive enumeration tool — no modification occurs. Primary risk is process-level
detection from the audit scan pattern. Run targeted checks rather than `audit` when
minimal noise is required.

## Full Reference

> Captured against SharpUp v1.0.1, 2026-05-29. Source: https://github.com/GhostPack/SharpUp README.

### Available checks (audit sub-commands)

| Check | Detects |
|-------|---------|
| `AlwaysInstallElevated` | HKLM + HKCU AlwaysInstallElevated both set to 1 |
| `CachedGPPPassword` | Cleartext passwords in GPP XML files in SYSVOL |
| `DomainGPPPassword` | Same as above but via SYSVOL share |
| `ModifiableScheduledTaskFiles` | Scheduled tasks pointing to user-writable files |
| `ModifiableServiceBinaries` | Service binaries the current user can overwrite |
| `ModifiableServices` | Services where current user can change binary path (SCM write) |
| `ProcessDLLHijack` | Processes missing DLLs from user-writable directories |
| `RegistryAutoRuns` | HKLM/HKCU Run/RunOnce values pointing to writable files |
| `RegistryAutoLogon` | Auto-logon registry keys with credentials |
| `TokenPrivileges` | Dangerous privileges in current token (SeImpersonate, SeAssignPrimaryToken, etc.) |
| `UnquotedServicePath` | Services with unquoted paths + writable parent directories |

### TokenPrivileges output — dangerous privileges

| Privilege | What it enables |
|-----------|----------------|
| `SeImpersonatePrivilege` | GodPotato, PrintSpoofer, JuicyPotatoNG token impersonation |
| `SeAssignPrimaryTokenPrivilege` | Assign a primary token to a process |
| `SeBackupPrivilege` | Read any file regardless of ACL (SAM dump path) |
| `SeRestorePrivilege` | Write any file regardless of ACL |
| `SeLoadDriverPrivilege` | Load kernel drivers (BYOVD path) |
| `SeDebugPrivilege` | Open any process including LSASS |
| `SeTcbPrivilege` | Act as part of the operating system |

### Source for this reference

- https://github.com/GhostPack/SharpUp (README full check listing)
- Version: v1.0.1 as of 2026-05-29
