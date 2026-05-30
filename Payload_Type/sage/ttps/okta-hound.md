---
name: OktaHound / Identity Provider Attack Paths
category: recon
subcategories: [okta-enumeration, idp-attack-paths, bloodhound-extension, identity]
tradecraft_tags: [okta, idp, identity-provider, bloodhound, attack-path, specterops, golang, sso]
mitre_attack:
  - id: T1087.004
    name: Account Discovery — Cloud Account
source:
  url: https://github.com/SpecterOps/OktaHound
  license: Unknown
  maintained: true
binary_type: native-exe
binary_filename: oktahound
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Okta API calls to enumerate users, groups, apps, and policies. Okta Audit Log
  captures all API activity. Bulk enumeration from an API token generates a high
  volume of Okta system log events.
usage_examples:
  - description: Collect Okta data for BloodHound CE
    args: "oktahound --okta-domain company.okta.com --api-token <okta-api-token>"
  - description: Enumerate Okta and output BloodHound-compatible data
    args: "oktahound --okta-domain company.okta.com --api-token <token> --output bh-data/"
opsec_notes: |
  OktaHound requires an Okta API token (or OAuth access token) to enumerate the Okta
  tenant. If the compromised user has Okta admin access, their session cookie can be
  exchanged for an API token. The enumeration generates significant Okta system log
  activity — an Okta security admin will see it.
  This is an infrastructure-side tool for analyzing organizations with Okta SSO —
  attack paths in Okta often connect to Azure AD, AWS, and other cloud resources.
gotchas: |
  Go binary — not Apollo-compatible. Requires Okta API credentials. Most valuable
  in organizations using Okta as their primary SSO/IdP — common in SaaS-heavy
  enterprises. Cross-correlate with AzureHound for full IdP → cloud resource paths.
related_ttps: [azurehound, roadtools, sharpcloud, bloodhound-ingest]
alternatives: [manual-okta-api, okta-cli]
common_args:
  --okta-domain:
    description: Okta domain (company.okta.com)
    typical_values: ["company.okta.com"]
    required: true
  --api-token:
    description: Okta API token for authentication
    typical_values: ["<api-token>"]
    required: true
last_updated: 2026-05-29
---

# OktaHound / Identity Provider Attack Paths

SpecterOps' OktaHound maps Okta tenant data (users, groups, applications, policies,
admin roles) into BloodHound CE for attack path analysis. Organizations using Okta as
their SSO provider have a rich attack surface: Okta admin roles can control access to
all integrated applications, and misconfigured application assignments can enable
privilege escalation.

## Okta Attack Surface

```
Okta admin roles:
  Super Administrator → controls all Okta users/apps/settings
  Application Administrator → controls specific app assignments
  Group Administrator → manages group membership (affects app access)

Attack paths:
  Compromised Okta app assignment → gain access to target SaaS app
  Okta admin via misconfigured group → privilege escalation
  Okta API token exfiltration → persistent tenant access
```

## BloodHound Integration

OktaHound exports to BloodHound CE, creating:
- Nodes for Okta users, groups, applications, admin roles
- Edges for group memberships, app assignments, admin delegations
- Cross-edges to Azure AD/Entra ID when Okta federates with Microsoft

## When This Matters

In Okta-heavy organizations (common in US SaaS companies), Okta is the identity plane:
- Email/calendar (Google Workspace or M365) behind Okta SSO
- Development tools (GitHub, GitLab) behind Okta SSO
- Cloud consoles (AWS, Azure) behind Okta SSO
- Business apps (Salesforce, Workday, Jira) behind Okta SSO

Gaining Okta admin access effectively means access to ALL of these.
