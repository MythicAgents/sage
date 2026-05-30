---
name: AtlasReaper
category: recon
subcategories: [confluence-recon, jira-recon, credential-farming, saas-recon]
tradecraft_tags: [confluence, jira, atlassian, credential-farming, saas-recon, dotnet, apollo-runnable]
mitre_attack:
  - id: T1087.003
    name: Account Discovery — Email Account
  - id: T1213.001
    name: Data from Information Repositories — Confluence
source:
  url: https://github.com/werdhaihai/AtlasReaper
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: AtlasReaper.exe
supported_os: [windows, linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  AtlasReaper makes authenticated API calls to Confluence/Jira — appearing as a
  legitimate API user. Atlassian's audit log (available in Cloud and Data Center)
  logs bulk content access patterns. Cloud instances with Atlassian Guard may detect
  unusual API usage from non-standard clients or geographic anomalies.
usage_examples:
  - description: Enumerate all Confluence spaces and harvest user emails
    args: "AtlasReaper.exe --url https://confluence.company.com --username jon.snow --password Password123 --action list-spaces"
  - description: Search Confluence for password-related content
    args: "AtlasReaper.exe --url https://confluence.company.com --username jon.snow --password Password123 --action search --query 'password'"
  - description: Harvest all users from Confluence
    args: "AtlasReaper.exe --url https://confluence.company.com --username jon.snow --password Password123 --action list-users"
  - description: Jira-specific credential farming
    args: "AtlasReaper.exe --url https://jira.company.com --username jon.snow --password Password123 --action jira-search --query 'password OR credential OR secret'"
opsec_notes: |
  AtlasReaper is highly valuable in environments where Confluence/Jira stores internal
  documentation. IT teams frequently document:
  - Server credentials in "how-to" pages
  - Service account passwords in runbooks
  - VPN and network device credentials
  - Database connection strings
  - Internal tool API keys
  The tool makes authenticated API calls that look like a normal user reading pages —
  low noise in most environments. Atlassian Cloud audit logs are the primary detection path.
gotchas: |
  Requires valid Confluence/Jira credentials (often the current user's domain credentials
  work if SSO is configured with the domain). For Atlassian Cloud, API tokens are
  needed (not domain password). The `--action search --query 'password'` approach
  is the most effective starting point — Atlassian's full-text search covers all page content.
  Results can be very large in mature documentation environments; filter aggressively.
related_ttps: [sharpmailer, snaffler, credential-hunting-checklist, sharpcloud]
alternatives: [manual-confluence-search, confluence-api-python]
common_args:
  --url:
    description: Confluence or Jira base URL
    typical_values: ["https://confluence.company.com", "https://company.atlassian.net"]
    required: true
  --username:
    description: Username for authentication
    typical_values: ["jon.snow", "jon.snow@company.com"]
    required: true
  --password:
    description: Password or API token
    typical_values: ["Password123", "<api-token>"]
    required: true
  --action:
    description: Action to perform
    typical_values: [list-spaces, list-users, search, jira-search, harvest-pages]
    required: true
  --query:
    description: Search query (for search action)
    typical_values: ["'password'", "'credential OR secret OR token'"]
last_updated: 2026-05-29
---

# AtlasReaper

SpecterOps researcher Craig Wright's .NET tool for Confluence and Jira reconnaissance
and credential farming. In many enterprises, Confluence is the internal documentation
system where IT staff document procedures including credentials, service account passwords,
and configuration details. AtlasReaper systematically searches this content via the
Confluence/Jira API.

## What Confluence Contains (Typically)

- **IT Runbooks**: Server credentials, admin passwords, service account details
- **Database documentation**: Connection strings with credentials
- **Network documentation**: VPN PSKs, firewall credentials, SNMP communities
- **Software deployment guides**: Often include cleartext credentials
- **Incident response documentation**: Sometimes references actual credentials
- **New employee guides**: May have initial credential setup instructions

## Search Strategy

```
1. Start broad:
   --action search --query "password"
   
2. Narrow to high-value:
   --action search --query "password site:IT OR admin OR database"
   
3. Check recent pages (most likely to have active credentials):
   --action search --query "password" --sort lastmodified

4. Check Jira tickets (IT operations tickets often contain credentials):
   --action jira-search --query "password OR credential OR secret"
```

## AtlassianHound Integration

Craig Wright also published AtlassianHound (Go) which exports Atlassian user/permission
data into BloodHound for attack path analysis. Together, AtlasReaper (credential harvest)
+ AtlassianHound (permission mapping) provide a complete Atlassian attack surface analysis.

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible. Useful as a post-foothold recon
step when the compromised user has Confluence/Jira access.
