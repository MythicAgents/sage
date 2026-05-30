---
name: Kerberos User Enumeration (without authentication)
category: recon
subcategories: [user-enumeration, kerberos, pre-auth, no-credentials]
tradecraft_tags: [kerberos, user-enumeration, pre-auth, no-credentials, initial-access, kerbrute, technique]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/ropnop/kerbrute
  license: MIT
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux, windows]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  AS-REQ requests for valid users return KRB5KDC_ERR_PREAUTH_REQUIRED.
  AS-REQ requests for invalid users return KRB5KDC_ERR_C_PRINCIPAL_UNKNOWN.
  Bulk AS-REQ requests from a single source generate Kerberos traffic visible to
  network monitoring and MDI. However, failed Kerberos AS-REQs are not logged as
  Windows Security events by default — this is the key OPSEC advantage.
usage_examples:
  - description: Enumerate valid users from a wordlist (no creds needed)
    args: "kerbrute userenum -d north.sevenkingdoms.local --dc DC01 users.txt"
  - description: Generate username wordlist from common patterns
    args: "for firstname in names; do echo $firstname.${lastname}; done > candidates.txt"
  - description: Automated username generation from LinkedIn/OSINT
    args: "(combine with linkedin2username or similar OSINT tool for candidate list)"
opsec_notes: |
  Kerberos user enumeration exploits the difference in KDC response for valid vs
  invalid usernames — no Windows Security Event is generated for AS-REQ failures
  (unlike NTLM 4625 events). MDI does detect this pattern, but legacy SIEM rules
  relying on Windows event logs won't. This is the stealth initial recon technique
  for user discovery without any credentials.
gotchas: |
  Username wordlists are critical — generic wordlists produce poor results. Combine
  OSINT (LinkedIn, company website) with username pattern guessing (firstname.lastname,
  f.lastname, firstnamelastname, etc.) for best results. Kerbrute outputs valid usernames
  in the format expected by other tools. If AS-REP roastable accounts are found during
  enumeration (no pre-auth required), they appear in the output.
related_ttps: [kerbrute, domainpasswordspray, msolspray]
alternatives: [kerbrute-userenum, nmap-krb5-enumusers, impacket-lookupsid]
common_args: {}
last_updated: 2026-05-29
---

# Kerberos User Enumeration (without authentication)

A technique (primarily implemented in Kerbrute) that determines valid domain usernames
without any authentication by observing KDC response differences:
- **Valid user**: KDC returns `KRB5KDC_ERR_PREAUTH_REQUIRED` (user exists, provide pre-auth)
- **Invalid user**: KDC returns `KRB5KDC_ERR_C_PRINCIPAL_UNKNOWN` (user doesn't exist)

## Why This Is Valuable

Before password spraying (DomainPasswordSpray, Kerbrute passwordspray), you need a
valid username list. This technique provides that WITHOUT:
- Any authentication credentials
- Windows Security Event 4625 (NTLM failure events)
- Account lockout risk

## Practical Username List Generation

```bash
# Common corporate username patterns to test:
firstname.lastname
flastname  
firstnamel
firstname_lastname
firstlast

# Tools for candidate generation:
# - linkedin2username (from LinkedIn OSINT)
# - namemash.py (various pattern combinations)
# Manual: company website, email signatures, annual reports

# Example for GOAD:
echo -e "jon.snow\narya.stark\nrobb.stark\njoffrey.baratheon\ndaenerys.targaryen" > candidates.txt
kerbrute userenum -d north.sevenkingdoms.local --dc DC01 candidates.txt
```

## Output Format

```
2026/05/29 10:00:00 >  [+] VALID USERNAME:    jon.snow@north.sevenkingdoms.local
2026/05/29 10:00:00 >  [+] VALID USERNAME:    arya.stark@north.sevenkingdoms.local
2026/05/29 10:00:00 >  [+] AS REP ROASTABLE:  hodor@north.sevenkingdoms.local
```

## After Enumeration

Valid usernames → Kerbrute passwordspray → valid credentials → Apollo foothold
