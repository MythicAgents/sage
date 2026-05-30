---
name: MSOLSpray
category: credential-access
subcategories: [password-spray, cloud-auth, azure-ad]
tradecraft_tags: [password-spray, azure-ad, office365, entra-id, o365, cloud]
mitre_attack:
  - id: T1110.003
    name: Brute Force — Password Spraying
source:
  url: https://github.com/dafthack/MSOLSpray
  license: Unknown
  maintained: false
binary_type: powershell-script
binary_filename: MSOLSpray.ps1
supported_os: [windows, linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  Azure AD sign-in logs capture all spray attempts (Event in Azure AD: failed sign-ins
  from single IP to multiple accounts). Microsoft Defender for Identity and Smart Lockout
  detect spray patterns. IP-based throttling is applied by Azure AD for excessive failed
  auths. Azure AD SSPR / conditional access violations are logged.
usage_examples:
  - description: Spray a password against O365 user list
    args: "Invoke-MSOLSpray -UserList users.txt -Password 'Spring2026!'"
  - description: Spray with delay between attempts
    args: "Invoke-MSOLSpray -UserList users.txt -Password 'Company2026' -Delay 30"
  - description: Check if spray would trigger lockout (verbose without lockout)
    args: "Invoke-MSOLSpray -UserList users.txt -Password 'test' -OutFile results.txt"
opsec_notes: |
  Azure AD / Microsoft 365 has Smart Lockout — IP-based rate limiting that activates
  before per-account lockout. Multiple failed attempts from a single IP trigger Smart
  Lockout for that IP, not the account. Azure AD sign-in logs capture all attempts.
  Consider using residential proxies or anonymous IPs for spray operations to avoid
  IP lockout. Spray from multiple source IPs for larger user lists.
gotchas: |
  Smart Lockout means IP-based blocking can prevent the spray from completing without
  per-account lockout (different from on-prem AD spray behavior). Users with MFA will
  yield "Success - Microsoft Strong Auth required" which is actionable (valid credentials
  found). Timing: spray at business hours when valid logins occur — anomaly detection
  based on location/time is less effective during normal working hours. MSOLSpray is
  not actively maintained; newer Azure AD auth endpoints may require updated tooling.
related_ttps: [domainpasswordspray, azurehound, roadtools]
alternatives: [trevorspray, o365spray, spray-and-pray]
common_args:
  -UserList:
    description: "Path to file containing usernames (UPN format: user@domain.com)"
    typical_values: ["users.txt"]
    required: true
  -Password:
    description: Password to spray
    typical_values: ["Spring2026!", "Company2026!", "P@ssw0rd1"]
    required: true
  -Delay:
    description: Seconds between each authentication attempt
    typical_values: [30, 60]
  -OutFile:
    description: Output file for results
    typical_values: ["spray_results.txt"]
last_updated: 2026-05-29
---

# MSOLSpray

A PowerShell-based Microsoft Online (MSOL) / Azure AD password spray tool. Targets the
Azure AD authentication endpoint for Microsoft 365 accounts — useful for external
enumeration of O365 tenants or as an initial access technique against hybrid environments.
Different behavior from on-prem AD spray: Azure AD Smart Lockout is IP-based, not
per-account, changing the risk calculus.

## Typical use cases
- External password spray against Microsoft 365 / Azure AD tenants
- Initial foothold when targets use Office 365 / Entra ID
- Identify valid credentials for cloud portals (OWA, Teams, SharePoint)

## How Sage uses this
MSOLSpray is an initial access / external engagement tool. For internal post-foothold
operations, DomainPasswordSpray is preferred. MSOLSpray is relevant in fully-cloud or
hybrid environments where Entra ID is the primary identity store.

## Output
Results file and console output with success/failure per user. Successful logins show
`SUCCESS` or `Success - Microsoft Strong Auth required` (MFA enabled but credentials valid).
