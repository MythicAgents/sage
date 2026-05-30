---
name: Kerbrute
category: credential-access
subcategories: [password-spray, user-enumeration, kerberos-brute]
tradecraft_tags: [kerberos, password-spray, user-enumeration, brute-force, golang]
mitre_attack:
  - id: T1110.003
    name: Brute Force — Password Spraying
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/ropnop/kerbrute
  license: MIT
  maintained: true
binary_type: native-exe
binary_filename: kerbrute
supported_os: [linux, windows]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  Kerberos AS-REQ failures (KRB5KDC_ERR_PREAUTH_FAILED) are logged in DC Security
  Event Log (Event 4768 failure). Kerberos-based enumeration generates valid-user probes
  (AS-REQ without pre-auth = username enumeration). Modern MDI detects spray patterns
  from Kerberos traffic patterns. The advantage over NTLM-based spray: no NTLM auth logs.
usage_examples:
  - description: User enumeration (determine which accounts exist)
    args: "userenum -d north.sevenkingdoms.local --dc 192.168.56.10 users.txt"
  - description: Password spray via Kerberos
    args: "passwordspray -d north.sevenkingdoms.local --dc 192.168.56.10 users.txt 'Spring2026!'"
  - description: Brute-force a specific account
    args: "bruteuser -d north.sevenkingdoms.local --dc 192.168.56.10 passwords.txt jon.snow"
opsec_notes: |
  Kerbrute's Kerberos-based spray avoids NTLM authentication logs (no Event 4625) —
  failures appear as Kerberos pre-auth failures (Event 4768) instead. This is QUIETER
  than NTLM-based spray for initial recon. However, AS-REQ floods are detectable by MDI.
  Valid username probes (userenum) work because Kerberos responds differently for
  valid vs invalid users during pre-auth. Spray lockout risk: same as domain spray — one
  attempt per user per password, wait between rounds.
gotchas: |
  Native EXE (Go binary) — Apollo cannot run via inline_assembly. Run from attacker
  infrastructure. User enumeration via Kerberos relies on the KDC sending different
  error codes for valid (PREAUTH_REQUIRED) vs invalid (PRINCIPAL_UNKNOWN) users.
  Some DCs with logging policies treat all failed Kerberos as equal — test enumeration
  behavior first. Lockout risk is the same as any password spray.
related_ttps: [domainpasswordspray, rubeus-brute, msolspray]
alternatives: [domainpasswordspray, rubeus-kerberoast, sprayhound]
common_args:
  userenum:
    description: Sub-command for username enumeration
    typical_values: [flag-only]
  passwordspray:
    description: Sub-command for password spray
    typical_values: [flag-only]
  bruteuser:
    description: Sub-command for brute-forcing a specific account
    typical_values: [flag-only]
  -d:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  --dc:
    description: Domain controller IP or FQDN
    typical_values: ["192.168.56.10"]
    required: true
  users.txt:
    description: File with usernames to test
    typical_values: ["users.txt"]
    required: true
last_updated: 2026-05-29
---

# Kerbrute

A Go-based Kerberos enumeration and password spray tool. Kerbrute uses Kerberos
pre-authentication to enumerate valid domain users (no NTLM traffic, no Event 4625)
and spray passwords against discovered accounts. The Kerberos-based approach avoids
the NTLM authentication logs that DomainPasswordSpray generates, making it a quieter
initial access technique.

## Typical use cases
- Username enumeration using Kerberos pre-auth response differences
- Password spray via Kerberos (avoids NTLM-based detection)
- Brute-force a specific known account

## How Sage uses this
Infrastructure-side Go binary. For spray operations, Kerbrute is preferred over
DomainPasswordSpray when Kerberos-based spray is available from Linux infrastructure
(quieter NTLM event footprint). Apollo-side spray uses DomainPasswordSpray or
Rubeus brute.

## Apollo-specific note
Native Go binary — not Apollo-runnable via inline_assembly. Run from attacker
Linux infrastructure.
