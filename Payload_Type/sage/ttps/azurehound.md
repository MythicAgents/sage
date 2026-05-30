---
name: AzureHound
category: recon
subcategories: [azure-ad-enumeration, attack-path-mapping, hybrid-ad]
tradecraft_tags: [azure-ad, entra-id, bloodhound, attack-path, cloud, hybrid]
mitre_attack:
  - id: T1087.004
    name: Account Discovery — Cloud Account
source:
  url: https://github.com/bloodhoundad/AzureHound
  license: GPL-3.0
  maintained: true
binary_type: native-exe
binary_filename: azurehound
supported_os: [linux, windows, macos]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  AzureHound uses the Microsoft Graph API and Azure Resource Manager API to enumerate
  Azure AD / Entra ID objects. Graph API calls from uncommon applications/tokens are
  logged in Azure AD sign-in logs. Mass enumeration of users, groups, and roles via
  Graph API is anomalous. Microsoft Defender for Cloud Apps may alert on bulk
  enumeration from new app registrations.
usage_examples:
  - description: Collect all Azure AD data with user token (device code flow)
    args: "list -t <tenant-id> --all"
  - description: Collect with service principal credentials
    args: "list -t <tenant-id> -a <app-id> -s <client-secret> --all"
  - description: Collect using refresh token from ROADtools or a browser session
    args: "list -t <tenant-id> --refresh-token <token> --all"
  - description: Collect only Azure AD relationships (no Azure Resource Manager)
    args: "list -t <tenant-id> --all --no-aad"
opsec_notes: |
  Graph API enumeration from a non-standard application is visible in Azure AD audit
  logs (sign-in logs, Graph API activity). Device code flow (interactive token acquisition)
  may require user interaction. Service principal credentials produce less interactive
  noise but leave app-based access records. Best OPSEC: use a legitimate pre-existing
  service principal if one is available.
gotchas: |
  AzureHound is a NATIVE EXE — Apollo cannot use inline_assembly for it. Run from
  attacker infrastructure. Requires Azure AD access (any user can enumerate by default
  unless tenant has restricted Graph API access). AzureHound data is imported into
  BloodHound CE (v5+) which supports Azure AD paths alongside on-prem AD paths.
  Hybrid identity paths (on-prem AD → Azure AD sync) are particularly valuable.
related_ttps: [sharphound, bloodhound-ingest, roadtools]
alternatives: [roadtools, aadinternal, microsoft-graph-powershell]
common_args:
  list:
    description: Collect Azure AD data
    typical_values: [flag-only]
    required: true
  -t:
    description: Azure tenant ID
    typical_values: ["<tenant-guid>"]
    required: true
  --all:
    description: Collect all available data (users, groups, roles, apps, devices, etc.)
    typical_values: [flag-only]
  -a:
    description: Application (client) ID for service principal auth
    typical_values: ["<app-id>"]
  -s:
    description: Client secret for service principal auth
    typical_values: ["<client-secret>"]
  --refresh-token:
    description: Refresh token for delegated auth (from existing browser session)
    typical_values: ["<refresh-token>"]
last_updated: 2026-05-29
---

# AzureHound

SpecterOps' Azure AD / Entra ID attack-path collector. AzureHound enumerates Azure AD
users, groups, roles, service principals, applications, conditional access policies,
and Azure resource permissions, then feeds the data to BloodHound CE for graph-based
attack-path analysis that spans both on-premises AD and Azure AD. Critical for hybrid
environments where on-prem AD and Azure AD are sync'd (Entra Connect) — the sync
relationship often creates cross-forest attack paths.

## Typical use cases
- Enumerate Azure AD for attack paths in hybrid environments
- Find over-privileged service principals and app registrations
- Discover Azure AD roles that can be abused for privilege escalation
- Identify hybrid attack paths (on-prem user → Azure AD sync → Azure subscription)
- Map conditional access policy gaps

## How Sage uses this
AzureHound is relevant in hybrid AD/Azure engagements. The data feeds into BloodHound
CE alongside SharpHound on-prem data — enabling Sage to reason about cross-environment
attack paths. Native EXE means it requires non-assembly execution.

## Apollo-specific note
Native executable — inline_assembly won't run this. Run from attacker infrastructure.
AzureHound data is imported into BloodHound CE for combined on-prem/cloud path analysis.

## Output
NDJSON zip file containing Azure AD relationship data for BloodHound CE import.
