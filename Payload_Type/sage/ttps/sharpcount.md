---
name: SharpCount
category: recon
subcategories: [domain-statistics, ad-object-count, quick-audit]
tradecraft_tags: [ad-statistics, domain-size, quick-audit, dotnet, apollo-runnable]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/0xthirteen/SharpCount
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpCount.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries for object counts — very low noise. Standard LDAP count operations
  that administrators run routinely.
usage_examples:
  - description: Get domain object counts (users, computers, groups, DCs, etc.)
    args: "SharpCount.exe"
  - description: Count specific object type
    args: "SharpCount.exe /domain:north.sevenkingdoms.local"
opsec_notes: |
  SharpCount does LDAP queries to count AD objects — one of the lowest-noise enumeration
  operations possible. Useful for understanding domain size before launching a full
  SharpHound collection (determines expected output size and collection time).
gotchas: |
  Not actively maintained. Simple tool — domain object counts don't change frequently.
  Primarily useful for pre-collection planning (large domain = longer SharpHound run,
  larger ZIP output, longer BloodHound import time).
related_ttps: [sharphound, sharpldap, powerview]
alternatives: [powerview-count, sharpldap-count]
common_args:
  /domain:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# SharpCount

A .NET assembly that counts Active Directory objects (users, computers, groups, DCs, OUs)
to give a quick picture of domain size. Useful for pre-collection planning before
running SharpHound — understanding domain size sets expectations for collection time
and output size.

## Output Example

```
Users: 847
Computers: 312
Groups: 234
Domain Controllers: 4
OUs: 67
GPOs: 43
```

## Use Case
Run SharpCount before SharpHound to estimate collection time and output size.
Large domains (>10k objects) warrant --Stealth or targeted collection methods.
