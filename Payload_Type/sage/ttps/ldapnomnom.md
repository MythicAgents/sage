---
name: LDAPNomNom
category: recon
subcategories: [ldap-enumeration, user-enumeration, no-auth]
tradecraft_tags: [ldap, user-enumeration, no-auth, anonymous, initial-access, golang]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/lkarlslund/ldapnomnom
  license: MIT
  maintained: true
binary_type: native-exe
binary_filename: ldapnomnom
supported_os: [linux, windows]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  LDAP queries to the DC — even anonymous LDAP queries are logged if LDAP audit
  logging is configured. The brute-force approach generates many LDAP queries in
  a short time — anomalous from a single source.
usage_examples:
  - description: Brute-force user enumeration via anonymous LDAP (no credentials)
    args: "ldapnomnom --server DC01.north.sevenkingdoms.local --input users.txt"
  - description: Enumerate with domain credentials
    args: "ldapnomnom --server DC01.north.sevenkingdoms.local --username jon.snow --password Password123 --input users.txt"
  - description: Generate username candidates and enumerate
    args: "ldapnomnom --server DC01 --generate --domain north.sevenkingdoms.local"
opsec_notes: |
  LDAPNomNom performs user enumeration via anonymous LDAP binding (if allowed on
  the DC) or with domain credentials. Many DCs allow anonymous LDAP reads of
  basic user attributes — this is configuration-dependent. The anonymous path
  requires no credentials at all, making it useful for initial access reconnaissance.
gotchas: |
  Anonymous LDAP enumeration is disabled by default on most modern DCs (AD LDS / Server 2016+).
  Check if anonymous LDAP is enabled before attempting. If disabled, credential-based
  LDAP enumeration (with existing domain creds) is the alternative.
  Native EXE (Go binary) — not Apollo inline_assembly compatible.
related_ttps: [kerbrute, pyldapsearch, sharphound, impacket-lookupsid]
alternatives: [kerbrute-userenum, pyldapsearch, impacket-lookupsid]
common_args:
  --server:
    description: DC LDAP server
    typical_values: ["DC01.north.sevenkingdoms.local"]
    required: true
  --input:
    description: Username candidate file
    typical_values: ["users.txt"]
  --generate:
    description: Generate username candidates automatically
    typical_values: [flag-only]
  --domain:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# LDAPNomNom

A Go-based LDAP user enumeration tool that works with or without credentials.
LDAPNomNom tests username candidates against the LDAP directory — if anonymous
LDAP reading is enabled (less common on modern DCs), it works with zero credentials.
Useful as a pre-credential-access user enumeration option alongside Kerbrute.

## Anonymous LDAP Check

```bash
# Test if anonymous LDAP works:
ldapsearch -x -H ldap://DC01 -b "DC=domain,DC=local" "(objectClass=user)" sAMAccountName
# If it returns results without -D/-w flags, anonymous LDAP is enabled
```

## Apollo-specific note
Go native EXE — not Apollo inline_assembly compatible. Run from attacker infrastructure.
