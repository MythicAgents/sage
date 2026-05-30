---
name: Azure AD Connect Credential Extraction
category: credential-access
subcategories: [azure-ad-connect, entra-connect, sync-credentials, hybrid-identity]
tradecraft_tags: [azure-ad-connect, entra-connect, sync-account, dsync, hybrid-identity, credential-access]
mitre_attack:
  - id: T1552
    name: Unsecured Credentials
source:
  url: https://github.com/fox-it/adconnectdump
  license: MIT
  maintained: false
binary_type: python-script
binary_filename: adconnectdump.py
supported_os: [windows, linux]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  Credential extraction from AZUREADSSOACC$ account or the AD Connect service account's
  encrypted credentials in the MSSQL/LocalDB instance on the AD Connect server. Accessing
  the AD Connect service's SQL database generates SQL Server audit events if configured.
  Reading the DPAPI-protected credential store requires SYSTEM-level DPAPI access.
usage_examples:
  - description: Extract AD Connect sync credentials (requires SYSTEM on AD Connect server)
    args: "python3 adconnectdump.py"
  - description: SharpADConnect .NET variant (Apollo-runnable)
    args: "SharpADConnect.exe"
  - description: Manual extraction via SQL query on LocalDB
    args: "sqlcmd -S '(localdb)\\\\ADSync' -Q 'SELECT [private_configuration_xml] FROM [ADSync].[dbo].[mms_management_agent]'"
opsec_notes: |
  The AD Connect server is one of the highest-value targets in a hybrid environment.
  The sync service account has DCSync-equivalent rights on the on-premises AD AND
  access to create/modify users in Azure AD/Entra ID. SYSTEM on the AD Connect server
  effectively equals domain compromise + tenant compromise simultaneously.
  Accessing the LocalDB or DPAPI-protected credential is the extraction path.
gotchas: |
  Requires SYSTEM access on the Azure AD Connect server (typically a dedicated member
  server, sometimes the DC itself in small environments). The sync service account
  credentials are encrypted via DPAPI with the machine key — only extractable with
  SYSTEM access or the DPAPI backup key. SharpADConnect is the .NET assembly variant.
  Two credential sets to extract:
  1. On-premises AD sync account (AAD_xxxxx or MSOL_xxxxx) — has DCSync rights on AD
  2. Azure AD tenant credentials — has write access to Entra ID
related_ttps: [sharpdpapi, mimikatz, impacket-secretsdump, azurehound, roadtools]
alternatives: [manual-sql-query, mimikatz-dpapi-mscache]
common_args: {}
last_updated: 2026-05-29
---

# Azure AD Connect Credential Extraction

Azure AD Connect (now Microsoft Entra Connect) synchronizes on-premises Active Directory
with Azure AD/Entra ID. The service stores sync account credentials in an encrypted
SQL LocalDB database on the AD Connect server. Extracting these credentials provides:
1. The on-premises sync account (MSOL_xxxxx or AAD_xxxxx) — has DCSync-equivalent rights
2. The Azure AD service credentials — has write access to the entire Entra ID tenant

## The Attack Chain

```
Prerequisite: SYSTEM on the Azure AD Connect server

1. Extract sync credentials:
   python3 adconnectdump.py
   OR: SharpADConnect.exe (Apollo-compatible .NET)

2. On-premises account (MSOL_xxxxx): has DCSync rights
   Apollo: dcsync /domain:X /user:krbtgt
   
3. Azure AD credentials: full tenant write access
   roadtools / azurehound using extracted credentials
```

## Why This Matters

In organizations with Azure AD Connect, the sync server is typically not hardened to
DC standards but has privileges that exceed the DC. A compromised AD Connect server
provides both on-prem and cloud domain compromise simultaneously.

## Finding the AD Connect Server

```
# LDAP query for the service account:
Get-DomainUser -Identity "MSOL_*" -Properties *
Get-DomainUser -Identity "AAD_*" -Properties *

# SharpHound shows the AD Connect server via the sync account's logon history
# The server is typically named AADCONNECT, ENTRACONNECT, or similar
```

## Tools

| Tool | Type | Notes |
|------|------|-------|
| adconnectdump.py (fox-it) | Python | Reference implementation; infrastructure-side |
| SharpADConnect | .NET assembly | Apollo-compatible; same functionality |
| Manual SQL query | Built-in sqlcmd | No binary drop needed if sqlcmd is present |
