---
name: ldapdomaindump
category: recon
subcategories: [ad-enumeration, ldap, python, offline-analysis]
tradecraft_tags: [ldap, python, linux-side, ad-dump, html-report, json]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/dirkjanm/ldapdomaindump
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: ldapdomaindump.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries from non-standard clients. Heavy sequential LDAP dumping generates
  typical enumeration-burst traffic patterns visible to MDI and LDAP audit logging.
usage_examples:
  - description: Full domain dump to HTML/JSON
    args: "ldapdomaindump -u 'NORTH\\\\jon.snow' -p Password123 192.168.56.10 -o /tmp/ldapdump"
  - description: Pass-the-hash domain dump
    args: "ldapdomaindump -u 'NORTH\\\\administrator' -p aad3b435b51404eeaad3b435b51404ee:nthash 192.168.56.10 -o /tmp/ldapdump"
opsec_notes: |
  Python tool — infrastructure side. Produces an offline-browseable HTML report of
  the domain — useful for operator analysis before more targeted exploitation. Less
  noisy than SharpHound's full collection for simple user/group/computer enumeration.
gotchas: |
  Python-only. Output includes HTML reports, JSON files, and a grepn file — designed
  for offline analysis rather than piping to other tools. For BloodHound integration,
  use bloodhound-python or SharpHound instead.
related_ttps: [sharphound, bloodhound-python, powerview, pyldapsearch]
alternatives: [bloodhound-python, sharphound, pyldapsearch]
common_args:
  -u:
    description: Username in DOMAIN\\\\user format
    typical_values: ["NORTH\\\\jon.snow"]
    required: true
  -p:
    description: Password or LM:NT hash
    typical_values: ["Password123", "aad3b435...:nthash"]
    required: true
  target:
    description: DC IP or FQDN
    typical_values: ["192.168.56.10"]
    required: true
  -o:
    description: Output directory
    typical_values: ["/tmp/ldapdump"]
last_updated: 2026-05-29
---

# ldapdomaindump

Dirk-jan Mollema's LDAP domain dumper. Connects to the DC via LDAP, enumerates all
users, groups, computers, OUs, and trusts, and produces HTML/JSON reports for offline
analysis. The HTML reports provide a quick browsable overview of the domain before
planning targeted exploitation.

## Typical use cases
- Quick domain census with browsable HTML output
- Offline analysis of domain structure before targeted attacks
- Pass-the-hash LDAP enumeration when credentials are hashes

## How Sage uses this
Infrastructure-side Python tool. For comprehensive attack-path collection, use
bloodhound-python (BloodHound-compatible output). ldapdomaindump is useful for
quick domain inventory with human-readable HTML output.
