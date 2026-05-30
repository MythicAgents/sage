---
name: SharpSpray
category: credential-access
subcategories: [password-spray, kerberos-spray, dotnet]
tradecraft_tags: [password-spray, kerberos, ldap, domain-users, lockout-aware, dotnet, apollo-runnable]
mitre_attack:
  - id: T1110.003
    name: Brute Force — Password Spraying
source:
  url: https://github.com/iomoath/SharpSpray
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: SharpSpray.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Kerberos pre-auth failures (Event 4768 type 18 failures) or LDAP authentication
  failures. Spray pattern — many users, one password, short timeframe — is detectable
  by SIEM Kerberos analytics and MDI. More visible than Kerbrute from infrastructure
  because it originates from a Windows host in the domain.
usage_examples:
  - description: Spray a single password against all domain users
    args: "--Password 'Spring2026!' --Sleep 1500"
  - description: Spray with custom user list
    args: "--UserList users.txt --Password 'Company2026' --Sleep 2000"
  - description: Spray using LDAP authentication
    args: "--Password 'Spring2026!' --Sleep 1500 --LDAP"
opsec_notes: |
  SharpSpray is a .NET assembly (Apollo inline_assembly compatible) — the key difference
  from DomainPasswordSpray (PowerShell) is avoiding PowerShell script block logging.
  Kerberos-mode spray generates Event 4768 failures. LDAP-mode spray generates LDAP
  authentication failures. --Sleep value is critical — default is 0ms; ALWAYS set a
  sleep to avoid triggering lockout.
gotchas: |
  LOCKOUT RISK: --Sleep defaults to 0 — ALWAYS specify --Sleep > lockout_reset_period.
  Check domain lockout policy first. Same cracking-not-needed caveat: successful spray
  yields valid credentials, not hashes. The .NET assembly advantage over DomainPasswordSpray
  is the absence of PowerShell logging.
related_ttps: [domainpasswordspray, kerbrute, rubeus-brute]
alternatives: [domainpasswordspray, kerbrute]
common_args:
  --Password:
    description: Password to spray
    typical_values: ["Spring2026!", "Company2026!"]
    required: true
  --UserList:
    description: Path to user list file
    typical_values: ["users.txt"]
  --Sleep:
    description: Milliseconds to sleep between attempts
    typical_values: [1500, 3000]
    required: true
  --LDAP:
    description: Use LDAP authentication instead of Kerberos
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpSpray

A .NET assembly password spray tool — the inline_assembly equivalent of DomainPasswordSpray.
Uses Kerberos or LDAP authentication to test a password against domain accounts. The
.NET approach avoids PowerShell script block logging (Event 4104), making it preferable
to DomainPasswordSpray when PowerShell logging is a concern.

## Typical use cases
- Password spray from within an Apollo agent without PowerShell
- Windows-side spray when Kerbrute (Go binary) isn't available

## How Sage uses this
SharpSpray is the Apollo-compatible (inline_assembly) password spray tool. Use when
DomainPasswordSpray's PowerShell logging risk is a concern and Kerbrute isn't on the
infrastructure. Always confirm lockout policy before running.

## Output
Console output listing successful authentications. Same result format as DomainPasswordSpray.
