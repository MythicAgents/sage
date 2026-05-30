---
name: SharpObjectACL
category: acl-abuse
subcategories: [dacl-enumeration, acl-discovery, targeted-acl]
tradecraft_tags: [acl, dacl, enumeration, specific-object, dotnet, apollo-runnable]
mitre_attack:
  - id: T1069.002
    name: Permission Groups Discovery — Domain Groups
source:
  url: https://github.com/FatRodzianko/SharpObjectACL
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpObjectACL.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP query for ACL of specific AD object — same signal as PowerView's
  Get-DomainObjectAcl. Very low noise; standard LDAP read.
usage_examples:
  - description: Get ACL for a specific user account
    args: "SharpObjectACL.exe -target 'CN=administrator,CN=Users,DC=north,DC=sevenkingdoms,DC=local'"
  - description: Get ACL for a computer object
    args: "SharpObjectACL.exe -target 'CN=WINTERFELL,OU=Domain Controllers,DC=north,DC=sevenkingdoms,DC=local'"
  - description: Get ACL for the domain root
    args: "SharpObjectACL.exe -target 'DC=north,DC=sevenkingdoms,DC=local'"
opsec_notes: |
  Single targeted ACL read — very low noise. For bulk ACL enumeration, SharpHound
  or PowerView's Get-DomainObjectAcl is better. SharpObjectACL is useful for quickly
  verifying whether a controlled principal has specific ACL rights on a target object
  before attempting exploitation.
gotchas: |
  Not actively maintained. Target must be specified as Distinguished Name (DN) format.
  Output includes raw ACE GUIDs — use PowerView or BloodHound to translate these to
  human-readable permission names. For verified ACL analysis, BloodHound's graph is
  the authoritative source.
related_ttps: [powerview, sharpldap, bloodhound-ingest, acl-abuse-chain]
alternatives: [powerview-get-domainobjectacl, impacket-dacledit-read]
common_args:
  -target:
    description: Target object Distinguished Name
    typical_values: ["CN=administrator,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
                     "DC=north,DC=sevenkingdoms,DC=local"]
    required: true
last_updated: 2026-05-29
---

# SharpObjectACL

A .NET assembly for reading the DACL (Discretionary Access Control List) of a specific
Active Directory object. Provides quick targeted ACL verification without the overhead
of a full SharpHound collection.

## When to Use

- Pre-exploitation verification: "Does jon.snow actually have GenericWrite on WINTERFELL$?"
- Quick ACL spot check on a specific object before attempting ACL abuse
- Investigating unexpected access denials

## vs PowerView

PowerView's `Get-DomainObjectAcl` provides the same data. SharpObjectACL is useful
when PowerView is not loaded (Apollo inline_assembly vs powershell_import context).
