---
name: Seatbelt
category: discovery
subcategories: [host-sa, situational-awareness, configuration-check]
tradecraft_tags: [enumeration, post-exploitation, host-info, configuration, ghostpack]
mitre_attack:
  - id: T1082
    name: System Information Discovery
  - id: T1016
    name: System Network Configuration Discovery
  - id: T1518
    name: Software Discovery
source:
  url: https://github.com/GhostPack/Seatbelt
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Seatbelt.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  Seatbelt's WMI and registry queries are noisy but individually benign. Behavioral
  detection focuses on the combination: rapid WMI enumeration, process enumeration,
  and scheduled task reads in quick succession from a non-admin context. Defender for
  Endpoint (MDE) has behavioral signatures for certain Seatbelt check combinations.
  String-based detection catches the literal "Seatbelt" name and some module names.
usage_examples:
  - description: Run all "system" checks (recommended first-pass)
    args: "system"
  - description: Run all checks the current user can access
    args: "-group=all"
  - description: Run all checks including user-specific checks
    args: "-group=all -full"
  - description: Run specific checks for credential hunting
    args: "CredEnum WindowsCredentialFiles TokenPrivileges"
  - description: Run Seatbelt against a remote system
    args: "-computername=DC01 -username=DOMAIN\\\\user -password=Password123 OSInfo"
  - description: Check for Credential Guard and other security features
    args: "CredGuard LSASettings UAC"
  - description: Full enumeration with output to file
    args: "-group=all -full -outputfile=C:\\\\Windows\\\\Temp\\\\sa.txt"
opsec_notes: |
  Individual Seatbelt checks are not especially loud, but running `-group=all` in quick
  succession will generate a burst of WMI, registry, and process activity that may alert
  behavior-based EDRs. For stealth, run targeted check groups rather than all-at-once.
  Rename the assembly before upload — "Seatbelt" is in many string-based blocklists.
  Remote execution via -computername requires authentication to the target.
gotchas: |
  Some checks require elevated privileges (e.g. services, remote registry reads) and
  will silently skip or produce partial output without admin. Use `-group=system` for
  a sensible default that works at user level. The `-full` flag enables more verbose
  output but significantly increases output size. Output is formatted for humans; for
  machine-parsing, consider redirecting to file and post-processing.
related_ttps: [sharpup, powerview, sharphound, sharpdir]
alternatives: [winpeas, watson, powerview]
common_args:
  -group:
    name: -group
    description: Run a named check group
    typical_values: [system, user, misc, chromium, remote, slack, all]
    required: false
  -full:
    description: Include verbose/extra output for each check
    typical_values: [flag-only]
  -computername:
    description: Remote host to run checks against
    typical_values: ["DC01", "10.0.0.5"]
  -username:
    description: Username for remote execution
    typical_values: ["DOMAIN\\\\user"]
  -password:
    description: Password for remote execution
    typical_values: ["Password123"]
  -outputfile:
    description: Write output to file instead of stdout
    typical_values: ["C:\\\\Windows\\\\Temp\\\\sa.txt"]
  -q:
    description: Quiet mode — suppress informational headers
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Seatbelt

GhostPack's host situational-awareness tool. Seatbelt runs a battery of "safety checks"
(security-relevant queries) across OS configuration, credential storage, network settings,
installed software, browser data, and more. The result is a rapid-fire profile of the
compromised host that tells an operator what the machine knows, what credentials it might
hold, what defenses are active, and what high-value pivot targets are reachable. Named
after the aviation pre-flight checklist concept.

## Typical use cases
- First-pass post-exploitation host profiling after initial foothold
- Identify credential material cached on the host (CredEnum, WindowsCredentialFiles, DPAPI blobs)
- Detect active defenses (UAC state, Credential Guard, LSA protection level, AV products)
- Enumerate network configuration for pivot planning (interfaces, routes, ARP, DNS cache)
- Discover scheduled tasks and services that could be used for persistence or privesc
- Check token privileges for local privesc opportunities (used alongside SharpUp)

## How Sage uses this
Seatbelt is Sage's host-awareness step immediately after foothold. Sage runs a targeted
check group (typically `system` plus specific credential checks) to understand:
- OS version and patch level (influences which exploits are viable)
- Active defenses that might block subsequent tools
- Credential material cached locally (for immediate reuse without LSASS access)
- Token privileges (feeds into SharpUp for privesc decision tree)

Sage parses the text output to extract key data points and presents them to the operator
as a structured summary before proposing next steps.

## Output
Text output to stdout, organized by check name. Each check section is delimited by
`============================` headers. Check output varies from single-line summaries
to multi-table structured data. With `-outputfile`, the same text is written to disk.

## OPSEC considerations
Single targeted checks are low-noise. The catch is running many checks in sequence — the
burst of WMI, registry, and process access is unusual and may trigger behavior-based alerts.
For maximum stealth, run the minimum checks needed (3-5 focused checks) rather than
`-group=all`. The assembly name "Seatbelt" is frequently blocked; always rename before upload.

## Full Reference

> Captured against Seatbelt v1.2.2, 2026-05-29. Source: https://github.com/GhostPack/Seatbelt README.

### Check groups

| Group | Contents |
|-------|---------|
| `system` | AMSIProviders, AntiVirus, AppLocker, ARPTable, AuditPolicies, CredGuard, DotNet, EnvironmentPath, EnvironmentVariables, ExplicitLogonEvents, ExploitGuard, FirewallRules, Hotfixes, InterestingEvents, LocalGPOs, LocalGroups, LSASettings, McAfeeConfigs, NamedPipes, NetworkShares, NTLMSettings, OSInfo, PowerShell, Processes, PSSessionSettings, RDPSessions, RDPSettings, SCCM, ServiceBinaryPermissions, Services, Sysmon, TokenPrivileges, UAC, WifiProfiles, WindowsDefender, WindowsEventForwarding, WindowsFirewall, WMIEventSubscriptions, WMIFilterBinding, WSUS |
| `user` | Bookmarks, Certificates, ChromiumPresence, CredEnum, CurrentDomainGroups, DpapiMasterKeys, ExplicitLogonEvents, FileInfo, FirefoxPresence, IEFavorites, IETabs, IEUrls, MasterKeys, MeetingURLs, OfficeRecentFiles, OracleSQLDeveloper, PowerShellHistory, PuttyHostKeys, PuttySessions, RDPSavedConnections, ScheduledTasks, SuperPutty, TokenPrivileges, WindowsCredentialFiles, WindowsVault |
| `chromium` | ChromiumBookmarks, ChromiumHistory, ChromiumPresence |
| `remote` | RemoteChecks, RDPConnections |
| `misc` | Various miscellaneous checks |
| `slack` | Slack workspace data |
| `all` | All available checks |

### High-value individual checks

| Check | What it returns | Privilege needed |
|-------|----------------|-----------------|
| `CredGuard` | Credential Guard / LSA protection status | user |
| `LSASettings` | LSA provider list, NTLM levels, WDigest state | user |
| `TokenPrivileges` | Current process token privilege list | user |
| `CredEnum` | Windows Credential Manager stored credentials | user |
| `WindowsCredentialFiles` | Credential files in AppData | user |
| `DpapiMasterKeys` | DPAPI master key GUIDs and states | user |
| `Hotfixes` | Installed KB patches (useful for exploit targeting) | user |
| `OSInfo` | Build, version, domain membership | user |
| `PowerShellHistory` | PowerShell transcript history | user |
| `Processes` | Running process list with owner | user |
| `ServiceBinaryPermissions` | Writable service binary paths | user |
| `UAC` | UAC configuration | user |
| `AntiVirus` | Detected AV products | user |
| `WindowsDefender` | Defender state + excluded paths | user |
| `WifiProfiles` | Saved Wi-Fi profiles + PSK if admin | user/admin |
| `Sysmon` | Sysmon configuration if present | user |

### Source for this reference

- https://github.com/GhostPack/Seatbelt (README full check listing)
- Version: v1.2.2 as of 2026-05-29
