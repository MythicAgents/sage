---
name: SharpHound Enterprise vs SharpHound CE
category: recon
subcategories: [sharphound-variants, bloodhound-collectors, continuous-collection]
tradecraft_tags: [sharphound, enterprise, ce, community-edition, collector-comparison, specterops]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/SpecterOps/SharpHound
  license: GPL-3.0
  maintained: true
binary_type: .net-assembly
binary_filename: SharpHound.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  See sharphound.md for primary detection notes. This is supplementary reference.
usage_examples:
  - description: See sharphound.md
    args: "(see sharphound.md and sharphound4cme.md)"
opsec_notes: |
  Reference document for collector selection. SharpHound CE (v2.x) is the right
  choice for Sage-based BloodHound CE engagements.
gotchas: |
  SharpHound Enterprise is NOT publicly available — it's distributed with BloodHound
  Enterprise (commercial SaaS). This document clarifies the distinction.
related_ttps: [sharphound, sharphound4cme, bloodhound-enterprise-vs-ce, rusthound, bloodhound-python]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# SharpHound Version Reference

Comprehensive reference for choosing the right SharpHound version.

## SharpHound Version Matrix

| Version | For BloodHound | Binary format | Key features |
|---------|----------------|--------------|-------------|
| SharpHound v1.x | Legacy (v4) | .NET assembly | Classic collection; still functional for legacy BH |
| SharpHound v2.x (CE) | BloodHound CE (v5+) | .NET assembly | Current maintained; CE-schema output |
| SharpHound Enterprise | BloodHound Enterprise | Service-deployed | Continuous/scheduled; not publicly released |

**For Sage: Use SharpHound v2.x** — the open-source release from github.com/SpecterOps/SharpHound.

## What Changed in v2.x (CE)

- Output schema updated for BloodHound CE's graph model
- Improved forest traversal reliability
- Better handling of large domains
- New collection methods (ADCS, some container/GPO improvements)
- Default output: single ZIP (not multiple JSON files)

## Verifying Your SharpHound Version

```
SharpHound.exe --version
```
Or check the file metadata after upload in Mythic.

## Collection Size Guidance

| Domain size | Collection method | Expected ZIP size | Collection time |
|-------------|----------------|------------------|----------------|
| <1000 objects (lab) | `-c All` | < 5 MB | < 1 min |
| 1k-10k objects (small) | `-c All` | 5-50 MB | 2-10 min |
| 10k-100k objects (medium) | `-c All --Throttle 200` | 50-500 MB | 10-60 min |
| >100k objects (large) | `-c DCOnly --Stealth` first | 1-50 MB | 5-15 min |

For large domains, start with DCOnly collection for attack-path analysis,
then follow up with targeted collection methods as needed.
