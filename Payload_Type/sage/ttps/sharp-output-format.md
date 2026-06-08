---
name: Sage Output and Reporting Reference
category: discovery
subcategories: [sage-output, reporting, operator-interface, structured-output]
tradecraft_tags: [sage, output, reporting, operator, structured, mythic, reference]
mitre_attack: []
source:
  url: https://github.com/MythicAgents/sage
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows, linux, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  This is an internal Sage reference document. No tools, no detection signal.
usage_examples:
  - description: Reference for how Sage should structure its output to operators
    args: "(internal reference)"
opsec_notes: |
  Sage's output quality directly affects operator efficiency. Well-structured output
  allows operators to make fast decisions about next steps.
gotchas: |
  This document is for Sage's own reference — describing how it should format findings
  and what level of detail to include in operator-facing responses.
related_ttps: [post-exploitation-playbook, lateral-movement-decision, goadabuse-reference]
alternatives: []
common_args: {}
last_updated: 2026-06-08
---

# Sage Output and Reporting Reference

Internal reference for how Sage structures output from tool execution and presents
findings to the operator. Good operator-facing output enables fast, confident decisions.

## Guiding Principles

1. **Lead with what matters** — answer "what did we find that enables the next step?"
2. **Structured findings first, raw output on request** — extract key data
3. **Always state what's needed next** — each finding should have a clear "so what"
4. **Highlight OPSEC flags** — if an action is high-risk, say so before proposing it
5. **No offline-crack recommendations** — rephrase as "this account may be exploitable via ADCS/delegation instead of cracking"

## Standard Output Blocks

### After SharpHound Collection

```
ENUMERATION COMPLETE
- Domain: north.sevenkingdoms.local
- Users: 47 | Computers: 12 | Groups: 28
- DCs: WINTERFELL.north.sevenkingdoms.local (2019)

HIGH-VALUE FINDINGS:
1. ESC1 template: VulnTemplate (CA: NORTH-CA) - any domain user can enroll
   → Path: certify_esc1
   
2. Unconstrained delegation: CASTELBLACK$ (non-DC)
   → Path: unconstrained_delegation_coercion
   
3. GenericWrite: jon.snow → WINTERFELL$ (computer object)
   → Path: rbcd_via_standin_rubeus
   
ATTACK PATHS: [3 to Domain Admins, 1 directly]
Shortest path: jon.snow → ESC1 cert → PKINIT → UnPAC → DA (4 steps)
```

### After Seatbelt SA Check

```
HOST: CASTELBLACK (192.168.56.22)
OS: Windows Server 2019 (Build 17763)
Privilege Level: DOMAIN SERVICES ACCOUNT (medium integrity)

IMMEDIATE OPPORTUNITIES:
- SeImpersonatePrivilege: PRESENT
  → GodPotato available: YES (Win Server 2019 ✓)
  → Recommendation: escalate to SYSTEM before proceeding

CREDENTIAL QUICK-WINS:
- AutoLogon: NONE
- Credential Manager: 2 entries (require SharpDPAPI to decrypt)
- PowerShell History: PRESENT - check manually

DEFENSES ACTIVE:
- Windows Defender: enabled, real-time
- EDR: [CrowdStrike Falcon detected via SharpEDRChecker]
- AMSI: active (inline_assembly bypasses automatically)
```

### After GodPotato Escalation

```
PRIVILEGE ESCALATION: SUCCESS
Method: GodPotato (SeImpersonatePrivilege)
Context: NT AUTHORITY\SYSTEM on CASTELBLACK

RECOMMENDED NEXT STEPS:
1. LSASS credential harvest (now have SYSTEM):
   → Apollo: mimikatz sekurlsa::logonpasswords
   → Expected: NTLM hashes for domain accounts logged into this machine
   
2. LSASS harvest output will enable:
   → Pass-the-hash lateral movement
   → Rubeus asktgt (overpass-the-hash) → Kerberos ticket
```

### After LSASS Harvest

```
CREDENTIAL HARVEST: CASTELBLACK
Method: Apollo native mimikatz (sekurlsa::logonpasswords)

HARVESTED CREDENTIALS:
- jon.snow [NORTH]: NT hash = [HASH] | AES256 = [KEY]
- administrator [CASTELBLACK local]: NT hash = [HASH]
- svc-backup [NORTH]: NT hash = [HASH]

IMMEDIATE USES:
- jon.snow hash: OPtH → Kerberos TGT for domain operations
- svc-backup: check if kerberoastable AND if ADCS enrollment available
  (to avoid cracking requirement)

NOTE: Offline hash cracking is NOT performed by Sage. 
If cracking is needed, provide hashes to operator.
```

### After Domain Compromise

```
DOMAIN COMPROMISE: north.sevenkingdoms.local
Method: ADCS ESC1 → PKINIT → UnPAC hash → Apollo dcsync
Operator: DA context established

krbtgt HASH (north.sevenkingdoms.local):
  NT hash: [HASH]
  AES256: [KEY]

PERSISTENCE OPTIONS (requires operator approval):
1. Golden Ticket: valid ~10h, survives single krbtgt rotation
   → Mimikatz kerberos::golden /krbtgt:[HASH] ... /ptt
   → OPSEC: without /ptt, Mimikatz writes ticket.kirbi to the current directory
   
2. Certificate persistence (more durable):
   → ForgeCert with CA key (requires CA compromise) → survives password changes

CROSS-DOMAIN OPPORTUNITIES:
- sevenkingdoms.local: trust exists → SID History attack possible
  → Requires: ExtraSIDs Golden Ticket with EA SID
  → Risk: MDI ExtraSIDs detection (high)

Recommended next action: [operator choice]
```

## What NOT to Include in Output

- Raw tool output (too noisy — extract key findings only)
- Hash cracking suggestions or hashcat commands
- Speculative claims without verification
- "I think" or "maybe" — state findings factually or note uncertainty explicitly
