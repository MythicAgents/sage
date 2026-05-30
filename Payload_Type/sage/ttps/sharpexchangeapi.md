---
name: SharpExchangeAPI / ExchangeRelayX
category: recon
subcategories: [exchange-enumeration, ews-api, email-enumeration]
tradecraft_tags: [exchange, ews, email, enumeration, dotnet, apollo-runnable, o365]
mitre_attack:
  - id: T1087.003
    name: Account Discovery — Email Account
  - id: T1114.002
    name: Email Collection — Remote Email Collection
source:
  url: https://github.com/dirkjanm/privexchange
  license: MIT
  maintained: false
binary_type: .net-assembly
binary_filename: SharpExchangeAPI.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Exchange Web Services (EWS) API calls generate authentication events and EWS access
  logs on Exchange servers. Bulk email access is flagged by Microsoft Defender for
  Office 365 and Exchange audit policies. GAL (Global Address List) enumeration
  via Autodiscover is relatively low-noise.
usage_examples:
  - description: Enumerate all mailboxes via Exchange GAL
    args: "SharpExchangeAPI.exe -url https://mail.north.sevenkingdoms.local/EWS/Exchange.asmx -action gal"
  - description: Search a mailbox for credential-containing emails
    args: "SharpExchangeAPI.exe -url https://mail.domain.local/EWS/Exchange.asmx -action search -terms 'password,vpn,credential'"
  - description: Check Exchange version and configuration
    args: "SharpExchangeAPI.exe -url https://mail.domain.local/EWS/Exchange.asmx -action version"
opsec_notes: |
  Exchange enumeration is valuable for several reasons: GAL reveals all user accounts
  (including service accounts, distribution groups), email search can yield credentials
  in IT support tickets or welcome emails, and Exchange infrastructure itself may be
  vulnerable (PrivExchange, ProxyLogon). For Apollo operators, this runs via inline_assembly.
  Prefer searching own mailbox first (lower privilege requirement, less logging than
  impersonation-based searches).
gotchas: |
  Exchange URL varies by organization (autodiscover, internal Exchange server FQDN).
  Modern O365 may require Modern Authentication (OAuth) rather than Basic auth — check
  the auth requirements before attempting. For O365, MailSniper (PowerShell) or
  GraphAPI-based tools may be more reliable.
related_ttps: [sharpmailer, exchange-privesc, powerview, sharphound]
alternatives: [sharpmailer-mailsniper, ruler]
common_args:
  -url:
    description: Exchange EWS endpoint URL
    typical_values: ["https://mail.domain.local/EWS/Exchange.asmx"]
    required: true
  -action:
    description: Operation to perform
    typical_values: [gal, search, version, getmail]
  -terms:
    description: Search terms (for search action)
    typical_values: ["'password,vpn,credential'"]
last_updated: 2026-05-29
---

# SharpExchangeAPI / ExchangeRelayX

A .NET assembly for Exchange Web Services (EWS) enumeration and email access.
Provides GAL (Global Address List) enumeration to discover all mailboxes, email
search for credential-containing messages, and Exchange server fingerprinting.

## What Exchange Reveals

- **GAL**: Complete list of all mailboxes (users, shared mailboxes, distribution lists,
  service accounts) — often more complete than LDAP user enumeration
- **Email content**: IT tickets with server credentials, welcome emails with temp
  passwords, vendor communications with access details
- **Exchange version**: Informs vulnerability selection (PrivExchange, ProxyLogon,
  ProxyNotShell version checks)

## Authentication Notes

| Environment | Auth method | Notes |
|------------|------------|-------|
| On-premises Exchange | NTLM/Kerberos | Works with current Windows credentials |
| Exchange Online (O365) | Modern Auth (OAuth) | MailSniper better for O365 |
| Hybrid | Depends on endpoint | Check ADFS/AAD join status |

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible for on-premises Exchange targeting.
