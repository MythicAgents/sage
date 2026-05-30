---
name: Seatbelt Full Reference (Expanded)
category: discovery
subcategories: [host-sa, seatbelt, all-checks, comprehensive-reference]
tradecraft_tags: [seatbelt, ghostpack, host-sa, all-checks, comprehensive, reference]
mitre_attack:
  - id: T1082
    name: System Information Discovery
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
  See seatbelt.md for detection notes. This is a supplementary reference expanding
  on the complete check catalog, check groupings, and operational patterns.
usage_examples:
  - description: Sage-recommended first-foothold checks (targeted, low-noise)
    args: "Seatbelt.exe AntiVirus CredGuard LSASettings UAC TokenPrivileges RegistryAutoLogon"
  - description: Full system checks
    args: "Seatbelt.exe -group=system"
  - description: Full user checks
    args: "Seatbelt.exe -group=user"
  - description: Credential-focused checks
    args: "Seatbelt.exe CredEnum WindowsCredentialFiles DpapiMasterKeys PowerShellHistory"
  - description: Everything
    args: "Seatbelt.exe -group=all -full"
opsec_notes: |
  See seatbelt.md. This file is supplementary — it documents the complete check list
  with operational notes per check. Use this when selecting targeted checks.
gotchas: |
  See seatbelt.md. This file supplements with the complete check catalog for reference.
related_ttps: [seatbelt, sharpup, watson, sharpedrchecker, post-exploitation-playbook]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Seatbelt Full Reference (Expanded Check Catalog)

> See `seatbelt.md` for the schema entry. This file provides the expanded check
> catalog with per-check operational notes for the most important checks.

## Priority Check Groups for Sage

### Tier 1: Run on EVERY foothold (< 5 seconds)

```
Seatbelt.exe AntiVirus TokenPrivileges UAC CredGuard LSASettings
```

| Check | What it tells you | Operational value |
|-------|------------------|------------------|
| AntiVirus | Which AV/EDR vendor is deployed | Choose tool evasion approach |
| TokenPrivileges | Current process privilege set | SeImpersonate → GodPotato immediately |
| UAC | UAC configuration level | Is UAC bypass needed? |
| CredGuard | Credential Guard status | Will LSASS dump yield credentials? |
| LSASettings | LSA provider list, WDigest, NTLM settings | WDigest enabled → cleartext in dump |

### Tier 2: Run when time permits

```
Seatbelt.exe RegistryAutoLogon PowerShellHistory CredEnum WindowsCredentialFiles
             Hotfixes OSInfo DpapiMasterKeys WifiProfiles
```

| Check | What it reveals |
|-------|----------------|
| RegistryAutoLogon | Cleartext credentials in AutoLogon registry keys |
| PowerShellHistory | Commands with passwords in PS history |
| CredEnum | Windows Credential Manager entries |
| WindowsCredentialFiles | Credential files in AppData |
| Hotfixes | Missing patches → Watson-type CVE correlation |
| OSInfo | OS version, domain, build → informs exploit selection |
| DpapiMasterKeys | DPAPI key GUIDs → input for SharpDPAPI |
| WifiProfiles | Saved WiFi PSKs |

### Tier 3: Specific scenarios

```
Seatbelt.exe Sysmon        # If Sysmon is deployed (detection-aware choice)
Seatbelt.exe WindowsDefender   # Defender exclusion paths (file staging locations)
Seatbelt.exe WSUS              # WSUS server configured? (SharpWSUS viable?)
Seatbelt.exe NetworkShares     # Accessible shares (pre-Snaffler survey)
Seatbelt.exe Services          # Service binary paths (pre-SharpUp verify)
Seatbelt.exe Processes         # Running processes (identify targets for steal_token)
```

## Complete Check Reference

### System Group

| Check | Description | Admin? |
|-------|-------------|--------|
| AMSIProviders | AMSI provider DLLs | No |
| AntiVirus | Installed AV via WMI | No |
| AppLocker | AppLocker policies | No |
| ARPTable | ARP cache | No |
| AuditPolicies | Audit configuration | No |
| CredGuard | Credential Guard state | No |
| DotNet | .NET versions | No |
| EnvironmentPath | PATH variable entries | No |
| EnvironmentVariables | All env vars | No |
| ExplicitLogonEvents | Recent explicit logon events | No |
| ExploitGuard | EMET/Exploit Guard policies | No |
| FirewallRules | Windows Firewall rules | No |
| Hotfixes | Installed Windows patches | No |
| InterestingEvents | Recent security events | No |
| LocalGPOs | Applied GPO settings | No |
| LocalGroups | Local group memberships | No |
| LSASettings | LSA configuration | No |
| McAfeeConfigs | McAfee configuration | No |
| NamedPipes | Open named pipes | No |
| NetworkShares | SMB shares | No |
| NTLMSettings | NTLM configuration | No |
| OSInfo | OS info, domain, build | No |
| PowerShell | PS version, execution policy | No |
| Processes | Running processes | No |
| PSSessionSettings | PS remoting config | No |
| RDPSessions | Active RDP sessions | No |
| RDPSettings | RDP configuration | No |
| SCCM | SCCM client configuration | No |
| ServiceBinaryPermissions | Writable service binaries | No |
| Services | Running services | No |
| Sysmon | Sysmon configuration | No |
| TokenPrivileges | Current token privileges | No |
| UAC | UAC level and policies | No |
| WifiProfiles | Saved WiFi networks | No |
| WindowsDefender | Defender state + exclusions | No |
| WindowsEventForwarding | Event forwarding config | No |
| WindowsFirewall | Firewall status | No |
| WMIEventSubscriptions | WMI persistence | No |
| WMIFilterBinding | WMI filter-to-consumer | No |
| WSUS | WSUS configuration | No |

### User Group (current user context)

| Check | Description | Admin? |
|-------|-------------|--------|
| Bookmarks | Browser bookmark files | No |
| Certificates | User certificate store | No |
| ChromiumPresence | Chrome/Edge detection | No |
| CredEnum | Credential Manager entries | No |
| CurrentDomainGroups | Current user domain groups | No |
| DpapiMasterKeys | User DPAPI master key GUIDs | No |
| ExplicitLogonEvents | User's explicit logon events | No |
| FileInfo | File metadata | No |
| FirefoxPresence | Firefox installation detection | No |
| IEFavorites | IE favorites | No |
| MasterKeys | DPAPI master keys (extended) | No |
| OfficeRecentFiles | Recent Office documents | No |
| PowerShellHistory | PS command history | No |
| PuttyHostKeys | Putty saved host keys | No |
| PuttySessions | Putty saved sessions + credentials | No |
| RDPSavedConnections | Saved RDP connections | No |
| RDPConnections | RDP connection history | No |
| ScheduledTasks | User scheduled tasks | No |
| TokenPrivileges | Token privilege set | No |
| WindowsCredentialFiles | Credential files in AppData | No |
| WindowsVault | Windows Vault entries | No |
| WifiProfiles | Saved WiFi profiles | No |

## Operational OPSEC Note

Running `-group=all` generates ~100+ WMI queries, registry reads, and file system
checks in rapid succession — very noisy. For stealth, run targeted individual checks.
The Tier 1 checks above run in < 3 seconds and answer the most critical post-foothold questions.
