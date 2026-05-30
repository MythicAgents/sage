---
name: DomainPasswordSpray
category: credential-access
subcategories: [password-spray, brute-force-avoidance]
tradecraft_tags: [password-spray, domain-users, authentication, lockout-aware, powershell]
mitre_attack:
  - id: T1110.003
    name: Brute Force — Password Spraying
source:
  url: https://github.com/dafthack/DomainPasswordSpray
  license: Unknown
  maintained: true
binary_type: powershell-script
binary_filename: DomainPasswordSpray.ps1
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Password spray attempts generate failed authentication events (Event 4625 with
  Sub Status 0xC000006A for bad password) for each failed attempt. Modern SIEM
  correlations detect spray patterns (many users, same time, same source IP, failed+1
  attempt per user). Azure AD / Entra ID has built-in spray detection. Account lockout
  is the most visible indicator — if lockout threshold is exceeded, accounts are disabled.
usage_examples:
  - description: Spray a single password against all domain users (respects lockout)
    args: "Invoke-DomainPasswordSpray -Password 'Spring2026!' -Delay 30"
  - description: Spray with custom user list
    args: "Invoke-DomainPasswordSpray -UserList users.txt -Password 'P@ssw0rd' -Delay 30"
  - description: Spray with multiple passwords (one per user iteration)
    args: "Invoke-DomainPasswordSpray -PasswordList passwords.txt -Delay 30"
opsec_notes: |
  Password spraying is inherently noisy from an event-log perspective. Key safeguard:
  NEVER exceed the domain's lockout threshold (typically 3-5 attempts per account) — use
  `-Delay` and verify lockout policy first (SharpHound/LDAP). One attempt per user per
  password, with at least 30-minute delay between rounds, is the minimum safe interval.
  Event 4625 is generated on every failed attempt — SIEM systems alert on spray patterns
  regardless of lockout. Consider whether credential access from spray is worth the
  detection risk compared to ADCS or delegation-based paths.
gotchas: |
  LOCKOUT RISK: Always verify the lockout policy before spraying. Check Fine-Grained
  Password Policies (FGPP) for specific high-value groups that may have different thresholds.
  Spray timing matters: business hours = accounts actively in use = faster detection;
  off-hours = fewer people to notice but logs still accumulate. The tool auto-respects
  lockout threshold when domain policy is readable. Passwords with seasonal/year patterns
  (Spring2026!, Company2026) are common first-spray guesses.
related_ttps: [rubeus, seatbelt, powerview]
alternatives: [kerbrute-spray, crackmapexec-spray, msolspray]
common_args:
  -Password:
    description: Single password to spray across all domain users
    typical_values: ["Spring2026!", "Company2026!", "P@ssw0rd1"]
  -UserList:
    description: Path to file of usernames to spray (instead of full domain enumeration)
    typical_values: ["users.txt"]
  -PasswordList:
    description: Path to file of passwords (sprays one per user per cycle)
    typical_values: ["passwords.txt"]
  -Delay:
    description: Seconds to wait between authentication attempts
    typical_values: [30, 60]
    required: true
  -Domain:
    description: Target domain (defaults to current)
    typical_values: ["north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# DomainPasswordSpray

A PowerShell-based domain password spray tool that enumerates domain users and attempts
a single password against each, respecting the domain lockout policy. Useful as an
initial foothold technique or to test for weak passwords in a domain without triggering
account lockouts.

## Typical use cases
- Test for seasonal/corporate passwords (Spring2026!, CompanyName123!) across domain users
- Initial foothold when no credentials are available
- Credential access as part of a broader reconnaissance effort

## How Sage uses this
Password spray is typically an initial access technique. Sage may suggest it when no
foothold exists and other paths (ADCS, coercion) require existing credentials. Use only
with explicit operator approval given the detection risk. Always verify lockout policy first.

## Output
Console output listing successful authentications. Failed attempts are silent (unless
verbose mode). Successful sprays show `[*] SUCCESS! User: username Password: password`.

## Important safety note
One of the most detection-visible techniques in this library. Always confirm lockout
policy with the operator before running. A misconfigured spray that exceeds lockout
threshold can disable hundreds of domain accounts — confirm this is acceptable before use.
