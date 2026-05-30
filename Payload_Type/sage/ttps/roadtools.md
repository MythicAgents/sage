---
name: ROADtools
category: recon
subcategories: [azure-ad-enumeration, entra-id, cloud-recon]
tradecraft_tags: [azure-ad, entra-id, graph-api, python, cloud, roadrecon]
mitre_attack:
  - id: T1087.004
    name: Account Discovery — Cloud Account
source:
  url: https://github.com/dirkjanm/ROADtools
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: roadrecon
supported_os: [linux, windows, macos]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  ROADtools enumerates Azure AD via Microsoft Graph API and Azure endpoints. Graph API
  mass enumeration from new/unusual application contexts appears in Azure AD sign-in
  logs and Microsoft Defender for Cloud Apps alerts.
usage_examples:
  - description: Authenticate with device code flow (interactive)
    args: "roadrecon auth -t <tenant-id>"
  - description: Gather all Azure AD data into local database
    args: "roadrecon gather"
  - description: Launch the GUI for browsing the collected data
    args: "roadrecon gui"
  - description: Export data for BloodHound or other analysis
    args: "roadrecon plugin bloodhound"
opsec_notes: |
  Python tool — infrastructure side. Authentication is the most detectable step.
  Device code flow creates visible interactive logins. Token-based auth is less visible
  but still logged. Prefer AzureHound for BloodHound CE integration (more maintained).
  ROADtools is excellent for its GUI and the depth of data it collects.
gotchas: |
  Python — not Apollo-runnable. Requires Python 3.8+. The gathered database is a
  SQLite file that can be analyzed offline with the ROADtools GUI or exported to
  BloodHound-compatible format via the bloodhound plugin.
related_ttps: [azurehound, bloodhound-ingest]
alternatives: [azurehound, aadinternal]
common_args:
  auth:
    description: Authenticate to Azure AD
    typical_values: [flag-only]
  gather:
    description: Collect all accessible Azure AD data into local SQLite database
    typical_values: [flag-only]
  gui:
    description: Launch web-based GUI for browsing collected data
    typical_values: [flag-only]
  plugin:
    description: Run a plugin (e.g. bloodhound for BloodHound export)
    typical_values: [bloodhound]
  -t:
    description: Azure tenant ID
    typical_values: ["<tenant-guid>"]
last_updated: 2026-05-29
---

# ROADtools

Dirk-jan Mollema's Azure AD reconnaissance toolkit. `roadrecon` enumerates all accessible
Azure AD objects via Graph API and stores them in a local SQLite database with a
searchable GUI. Excellent for in-depth Azure AD analysis and for generating BloodHound-
compatible output. Complements AzureHound with deeper data and a browseable interface.

## Typical use cases
- Comprehensive Azure AD enumeration with GUI-based analysis
- Export Azure AD data to BloodHound CE via bloodhound plugin
- Deep analysis of conditional access policies, app registrations, guest accounts

## How Sage uses this
Infrastructure-side Python tool. AzureHound is preferred for BloodHound CE integration.
ROADtools is the go-to for in-depth Azure AD investigation with its GUI.

## Apollo-specific note
Python-only — not runnable from Apollo. Run from attacker infrastructure.
