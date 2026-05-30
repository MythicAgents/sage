---
name: impacket-lookupsid
category: recon
subcategories: [sid-enumeration, user-enumeration, linux-side]
tradecraft_tags: [impacket, sid, user-enumeration, linux-side, python, domain-recon]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: lookupsid.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  SID enumeration via LSARPC (MS-LSAD) generates authentication events on the target
  DC. The RPC calls are standard administration calls but bulk enumeration from
  non-admin accounts is anomalous. MDI may detect rapid SID enumeration.
usage_examples:
  - description: Enumerate all domain SIDs (users, groups, computers)
    args: "lookupsid.py north.sevenkingdoms.local/jon.snow:Password123@DC01 0"
  - description: Enumerate with NTLM hash
    args: "lookupsid.py -hashes :nthash north.sevenkingdoms.local/administrator@DC01 0"
  - description: Enumerate only to RID 1500 (faster)
    args: "lookupsid.py north.sevenkingdoms.local/jon.snow:Password123@DC01 1500"
opsec_notes: |
  Python-only — infrastructure side. SID/RID enumeration maps RID values to account
  names. Useful when LDAP is filtered but LSARPC (RPC/DCOM) is accessible. The RID
  brute-force approach to user enumeration avoids LDAP entirely. For Apollo engagements,
  SharpHound or SharpView via LDAP is preferred.
gotchas: |
  Python-only. The number argument (0) tells lookupsid to brute-force all RIDs from 0
  to max (500 by default, configurable). This generates many RPC calls — use a reasonable
  limit (1500) for typical domains. The domain SID is needed for Golden/Diamond Ticket
  forgery — lookupsid output includes it.
related_ttps: [sharphound, sharpldap, pyldapsearch, impacket-secretsdump]
alternatives: [sharphound, pyldapsearch, powerview]
common_args:
  target:
    description: Target DC in DOMAIN/user:pass@DC format
    typical_values: ["north.sevenkingdoms.local/jon.snow:Password123@DC01"]
    required: true
  maxRid:
    description: Maximum RID to enumerate to
    typical_values: [0, 1500, 5000]
last_updated: 2026-05-29
---

# impacket-lookupsid

impacket's SID/RID enumeration tool. Bruteforces RID values via the LSARPC protocol
to discover domain accounts without using LDAP. Useful when LDAP is filtered but
RPC/DCOM is accessible, or for extracting the domain SID needed for ticket forgery.

## Key Use: Domain SID Extraction

```bash
lookupsid.py DOMAIN/user:pass@DC 1  # Just get the first few entries
# Output shows: [*] Domain SID is: S-1-5-21-XXXXXXXX-XXXXXXXXXX-XXXXXXXXX
```

The domain SID is required for Golden Ticket and Diamond Ticket forgery. This is
an alternative to PowerView's `Get-Domain | Select DomainSid`.

## Apollo-specific note
Python/Linux only. For domain SID within Apollo, use Seatbelt's OSInfo check or
SharpLdapSearch to query the domain object.
