---
name: pyldapsearch
category: recon
subcategories: [ad-enumeration, ldap, python, quiet-recon]
tradecraft_tags: [ldap, python, ad-enumeration, linux-side, bloodhound-alternative]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/fortalice/pyldapsearch
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: pyldapsearch.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries from non-standard clients against the DC. Python-based tools produce
  different LDAP protocol fingerprints than native Windows LDAP clients. LDAP server
  logs on DCs capture all authenticated queries.
usage_examples:
  - description: Enumerate all domain users with their attributes
    args: "pyldapsearch.py -d north.sevenkingdoms.local -u jon.snow -p Password123 '(objectClass=user)'"
  - description: Find computers with unconstrained delegation
    args: "pyldapsearch.py -d north.sevenkingdoms.local -u jon.snow -p Password123 '(&(objectClass=computer)(userAccountControl:1.2.840.113556.1.4.803:=524288))'"
  - description: Find SPN-mapped accounts (kerberoastable)
    args: "pyldapsearch.py -d north.sevenkingdoms.local -u jon.snow -p Password123 '(&(objectClass=user)(servicePrincipalName=*))'"
  - description: Enumerate using NTLM hash (pass-the-hash to LDAP)
    args: "pyldapsearch.py -d north.sevenkingdoms.local -u administrator -H nthash '(objectClass=domain)'"
opsec_notes: |
  Python tool — runs from attacker infrastructure (Linux). The key advantage over
  SharpHound/PowerView is that it runs entirely from the attacker's side without
  requiring a Windows foothold. Pass-the-hash support means valid NTLM hashes can
  be used directly for enumeration without cracking.
gotchas: |
  Python-only — not Apollo-runnable. Designed for Linux-side LDAP enumeration when
  a Windows foothold isn't yet established. For Apollo-based enumeration, use
  SharpHound (.net-assembly) or PowerView (powershell_import). The raw LDAP filter
  syntax requires knowledge of AD attribute names and UAC flag values.
related_ttps: [sharphound, powerview, sharpdir, bloodhound-ingest]
alternatives: [ldapsearch, sharphound, powerview]
common_args:
  -d:
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -u:
    description: Username
    typical_values: ["jon.snow"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  -H:
    description: NTLM hash for pass-the-hash authentication
    typical_values: ["<nthash>"]
  filter:
    description: LDAP filter string
    typical_values: ["(objectClass=user)", "(&(objectClass=computer)(userAccountControl:1.2.840.113556.1.4.803:=524288))"]
    required: true
last_updated: 2026-05-29
---

# pyldapsearch

A Python LDAP query tool for Active Directory enumeration from Linux infrastructure.
Supports NTLM hash authentication (pass-the-hash to LDAP), making it useful when
credentials are available without a Windows foothold. Accepts raw LDAP filter syntax
for flexible targeted queries.

## Typical use cases
- LDAP enumeration from Linux attack infrastructure before establishing a Windows foothold
- Pass-the-hash LDAP queries using recovered NTLM hashes
- Targeted LDAP queries for specific delegation, SPN, or UAC flag patterns

## How Sage uses this
Infrastructure-side Python tool. For Apollo-based Windows enumeration, use SharpHound
or PowerView. pyldapsearch is the Linux-side equivalent when working from infrastructure
before a Windows agent is established.

## Apollo-specific note
Python-only — not runnable from Apollo. Documented for completeness in Linux-side
attack infrastructure workflows.
