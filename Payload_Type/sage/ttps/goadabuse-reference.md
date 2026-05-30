---
name: GOAD Environment Quick Reference
category: recon
subcategories: [goad, lab-environment, target-reference, demo]
tradecraft_tags: [goad, game-of-active-directory, lab, demo, target-reference, mayfly277]
mitre_attack: []
source:
  url: https://github.com/Orange-Cyberdefense/GOAD
  license: GPL-3.0
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  GOAD is a lab environment — no production detection concerns.
usage_examples:
  - description: Reference for GOAD domain accounts and machines during demos
    args: "(reference document)"
opsec_notes: |
  GOAD (Game of Active Directory) is a deliberately vulnerable AD lab environment.
  This reference documents the key machines, accounts, and misconfigurations for
  rapid operation planning during demos.
gotchas: |
  GOAD credentials and network layout may vary between GOAD versions and deployment
  configurations. Verify actual credentials during engagement assessment.
related_ttps: [sharp-trust-walker, sharphound, post-exploitation-playbook]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# GOAD Environment Quick Reference

Reference for the GOAD (Game of Active Directory) lab environment used in Sage demos.
GOAD simulates a Westeros-themed three-domain Active Directory forest with intentional
misconfigurations targeting real-world AD attack patterns.

## Domain Structure

```
Forest Root:     sevenkingdoms.local
  ├── Child:     north.sevenkingdoms.local
  └── External:  essos.local (separate forest with trust)
```

## Key Machines

| Hostname | Domain | Role | OS | IP |
|----------|--------|------|----|----|
| KINGSLANDING | sevenkingdoms.local | Domain Controller | Server 2019 | 192.168.56.10 |
| WINTERFELL | north.sevenkingdoms.local | Domain Controller | Server 2019 | 192.168.56.11 |
| CASTELBLACK | north.sevenkingdoms.local | Member Server | Server 2019 | 192.168.56.22 |
| BRAAVOS | essos.local | Domain Controller | Server 2016 | 192.168.56.12 |
| MEEREEN | essos.local | Domain Controller | Server 2016 | 192.168.56.13 |
| DRAGONSTONE | sevenkingdoms.local | Member Server | Server 2019 | 192.168.56.23 |

## Key Domain Accounts

### north.sevenkingdoms.local
| Account | Type | Notes |
|---------|------|-------|
| jon.snow | User | Initial foothold candidate |
| robb.stark | User | Has some privileges |
| sansa.stark | User | Standard user |
| eddard.stark | DA | Domain Admin north |
| catelyn.stark | User | — |
| arya.stark | User | — |
| hodor | Service account | Often has SPN |

### sevenkingdoms.local
| Account | Type | Notes |
|---------|------|-------|
| joffrey.baratheon | DA | Domain Admin |
| cersei.lannister | User | Has specific privileges |
| tywin.lannister | DA | Domain Admin |
| administrator | DA | Default admin |

### essos.local
| Account | Type | Notes |
|---------|------|-------|
| daenerys.targaryen | DA | Domain Admin essos |
| khal.drogo | User | — |
| jorah.mormont | User | — |
| missandei | User | — |
| administrator | DA | Default admin |

## Known GOAD Misconfigurations (varies by version)

### ADCS
- ESC1 vulnerable certificate template typically exists in north domain
- ADCS CA: NORTHCA\NORTH-CA (verify during engagement)

### Kerberos Delegation
- Constrained delegation configured on specific service accounts
- RBCD exploitation path via specific machine objects

### ACL Abuse
- GenericAll / GenericWrite on specific accounts enabling Whisker attacks
- WriteDACL paths for DCSync grants

### GPO
- At least one GPO with weaker-than-expected ACLs (verify with Grouper2)

## Mayfly277 GOAD Walkthrough Reference

The authoritative GOAD exploitation guide:
- https://mayfly277.github.io/posts/GOADv2-pwning-part1/ (and subsequent parts)
- Full exploitation chain documented per vulnerability
- Excellent reference for expected attack paths

## Quick Credential Reference

Default GOAD user passwords follow `Password123!` pattern or GOAD-specific passwords.
Run Seatbelt, check for GPP passwords in SYSVOL, and use Kerbrute enumeration to discover
actual credentials in the deployment.

## Sage Demo Target State

The Trust Walker demo targets:
1. Initial foothold as jon.snow (north.sevenkingdoms.local)
2. Escalate via ADCS/delegation to admin in north
3. DCSync for north krbtgt hash
4. Cross-domain to sevenkingdoms.local
5. DCSync for sevenkingdoms krbtgt hash
6. Trust/SID-history to essos.local
7. DCSync for essos krbtgt hash
Final state: control of all three domains' krbtgt hash
