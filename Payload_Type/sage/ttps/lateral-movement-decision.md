---
name: Lateral Movement Decision Reference
category: lateral-movement
subcategories: [decision-tree, technique-selection, execution-primitives]
tradecraft_tags: [lateral-movement, decision-tree, smb, wmi, winrm, rdp, dcom, technique]
mitre_attack:
  - id: T1021
    name: Remote Services
source:
  url: https://attack.mitre.org/techniques/T1021/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Lateral movement always generates authentication events on the target. The specific
  detection signal varies by technique. SMB-based movement (PSExec, SMBExec) is highest
  signal; WMI is medium; PSRemote/WinRM generates Event 4688 for wsmprovhost.exe child
  processes; DCOM generates COM activation events.
usage_examples:
  - description: "Decision: choose best lateral movement method for current context"
    args: "(see decision tree below)"
opsec_notes: |
  Lateral movement technique selection is a risk/reward tradeoff. The quieter methods
  (WinRM, DCOM, WMI) are less commonly blocked but may still generate detection events.
  SMB-based exec (PSExec-style) is highly detected. Always use pass-the-ticket over
  NTLM where possible.
gotchas: |
  This is a REFERENCE DOCUMENT for lateral movement decision-making, not a tool.
  Technique selection should consider: what's running on the target, what protocols
  are accessible, what credential material is available, and how much noise is acceptable.
  On DCOM-hardened Windows targets, a valid local-admin credential can still fail WMI
  activation with target-side DistributedCOM Event 10036. Treat that as an activation
  authentication-level problem, not as proof that the password, SMB access, or WMI ACL is wrong.
related_ttps: [pass-the-hash, pass-the-ticket, sharpwmi, crackmapexec, impacket-wmiexec, sharprdp, sharp-mapexec]
alternatives: []
common_args: {}
last_updated: 2026-07-11
---

# Lateral Movement Decision Reference

A decision tree for selecting the appropriate lateral movement technique based on
available credentials, target state, and OPSEC requirements.

## Decision Tree

```
Credentials available?
├── Yes — Hash or ticket?
│   ├── Kerberos ticket (TGT) → inject with Rubeus ptt → use Kerberos-based exec
│   │                          → SharpMapExec /kerberos | SharpWMI | WinRM
│   ├── NT hash → Overpass-the-Hash first (Rubeus asktgt /rc4)
│   │           → Then use ticket-based methods above
│   │           OR
│   │           → Direct NTLM PTH (Apollo pth → SharpWMI | SharpMapExec)
│   └── Cleartext password → all methods available
│
└── No credentials → need to harvest first
    → SharpHound ACL path → delegation/RBCD/shadow-cred chain
    → LAPS read → local admin on target
    → Snaffler → credential file → use it
```

## By Protocol / Technique

| Protocol | Tool | Detection Signal | Notes |
|----------|------|-----------------|-------|
| SMB + Service | impacket-psexec | HIGH | Service install/start/delete logged |
| SMB + Shell | CrackMapExec | MEDIUM | No service, but uses ADMIN$ |
| WMI | SharpWMI / wmiexec | MEDIUM | WmiPrvSE.exe child process |
| WinRM | SharpMapExec / evil-winrm | LOW-MEDIUM | wsmprovhost.exe parent |
| RDP (automated) | SharpRDP | MEDIUM | Full session creation |
| DCOM | custom | MEDIUM | COM activation events |
| Payload drop + exec | Apollo spawn + inject | MEDIUM | Process creation |

## By Apollo Native Capability

| Goal | Apollo Command | Notes |
|------|---------------|-------|
| Move agent to new process | migrate PID | Uses existing process |
| Spawn new agent process | spawn process | New C2 channel |
| Token-based access | make_token | Network-only; no new process |
| NTLM PTH | pth | Spawns process with PTH token |
| Ticket injection | ticket_cache_add | Uses existing process |

## OPSEC Priority Order (quiet → loud)

1. **Ticket-based WinRM/WMI** — Kerberos auth, no new agent process
2. **Apollo make_token + remote WMI** — network logon, WMI execution
3. **NTLM PTH + WMI** — NTLM auth events
4. **New Apollo payload delivery** — clean but generates full EDR process event
5. **SMBExec / PSExec-style** — service creation, highest signal

## DCOM-Hardened Apollo WMI

If a local-admin credential proves `\\target\C$` access but Apollo `wmiexecute` returns
`0x80070005` during WMI connect, check the target System log before changing credentials.
DistributedCOM Event 10036 means the client tried to activate the DCOM server below
`RPC_C_AUTHN_LEVEL_PKT_INTEGRITY`; in that case the failure occurs before WMI namespace
authorization.

For Apollo, prefer the token-backed sequence:

1. `make_token` with the local-admin credential.
2. `wmiexecute` with only `host` and `command` so Apollo uses its current-token COM branch.
3. Read a target-side proof file over `C$`.
4. `rev2self` after proof readback.

Keep later WMI-backed follow-on work on the same token-backed branch too, including remote
CA export or other administrative scripts; switching back to explicit-credential WMI can
reintroduce the same activation failure.

Do not treat `Command spawned PID` alone as objective proof. The proof file must show the
marker, target hostname, and expected remote identity.

## Pre-Movement Checklist

Before lateral movement, verify:
1. Target reachability (ping/port scan — netscan or Athena port-scan)
2. Credential validity (SharpMapExec smb /command:check)
3. Firewall rules for chosen protocol
4. WinRM enabled if using WinRM path
5. Current ticket validity (Rubeus triage)
