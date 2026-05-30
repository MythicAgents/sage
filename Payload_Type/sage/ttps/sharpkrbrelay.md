---
name: SharpKrbRelay
category: coercion-relay
subcategories: [kerberos-relay, rbcd, privilege-escalation, local-pe]
tradecraft_tags: [kerberos, relay, rbcd, privilege-escalation, dotnet, local-pe]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://github.com/CICADA8-Research/SharpKrbRelay
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: SharpKrbRelay.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Same detection surface as KrbRelay: DCOM activation events (COM object instantiation),
  Kerberos service ticket requests for LDAP SPN visible in Windows Security Log, LDAP
  write to DC (Event 5136 for msDS-AllowedToActOnBehalfOfOtherIdentity). MDI detects
  RBCD attribute writes from unusual principals. Sysmon event 3 for loopback connections.
usage_examples:
  - description: Local privesc via RBCD — relay to LDAP, write RBCD for attacker machine account
    args: "-spn ldap/dc01.north.sevenkingdoms.local -clsid 90f18417-f0f1-484e-9d3c-59dceee5dbd8 -rbcd S-1-5-21-...-COMPUTERSID"
  - description: Add shadow credentials to the machine account
    args: "-spn ldap/dc01.north.sevenkingdoms.local -clsid 90f18417-f0f1-484e-9d3c-59dceee5dbd8 -shadowcred"
  - description: Automatically find a suitable CLSID and perform RBCD
    args: "-spn ldap/dc01.north.sevenkingdoms.local -rbcd S-1-5-21-...-COMPUTERSID -auto"
opsec_notes: |
  SharpKrbRelay improves on KrbRelay with `-auto` CLSID selection (reduces trial-and-error)
  and better Windows 11 / Server 2022 compatibility. The detection surface is identical
  to KrbRelay. Prefer single-shot exploitation over iterative CLSID scanning in monitored
  environments. The LDAP write is the highest-fidelity detection signal.
gotchas: |
  Requires domain membership. The `-auto` flag tries multiple CLSIDs automatically
  but may fail on hardened systems or custom DCOM configurations. If `-auto` fails,
  enumerate working CLSIDs manually with the companion CheckCOM tool. Same RBCD
  prerequisites as KrbRelay apply: you need a machine account (with SPN) to write
  into the RBCD attribute, then a Rubeus S4U chain to complete exploitation.
  Windows 11 22H2+ has additional mitigations for some DCOM-coercion methods — test first.
related_ttps: [krbrelay, standin, rubeus, whisker]
alternatives: [krbrelay, krbrelayup]
common_args:
  -spn:
    name: -spn
    description: Target SPN for Kerberos relay (ldap/dc01.domain or cifs/target)
    typical_values: ["ldap/dc01.north.sevenkingdoms.local"]
    required: true
  -clsid:
    name: -clsid
    description: CLSID of SYSTEM-context DCOM object to trigger coercion
    typical_values: ["90f18417-f0f1-484e-9d3c-59dceee5dbd8"]
  -auto:
    name: -auto
    description: Auto-discover a working CLSID (tries multiple candidates)
    typical_values: [flag-only]
  -rbcd:
    name: -rbcd
    description: SID of machine account to write into RBCD attribute on the target
    typical_values: ["S-1-5-21-...-COMPUTERSID"]
  -shadowcred:
    name: -shadowcred
    description: Perform shadow credentials attack via relayed auth
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpKrbRelay

The maintained C# successor to KrbRelay, with improved CLSID auto-discovery and
broader Windows version compatibility. SharpKrbRelay implements the same Kerberos
loopback relay primitive — DCOM coercion triggers a SYSTEM-context Kerberos ticket
that is relayed to the DC's LDAP to perform RBCD writes or shadow credential injection.
The result is local privilege escalation from any domain user to SYSTEM without external
infrastructure.

## Typical use cases
- Local privilege escalation to SYSTEM on domain-joined machines (RBCD path)
- Shadow credential injection into the machine account for persistent access
- Auto-discover working CLSID with `-auto` flag (removes manual CLSID hunting)
- Windows 11 / Server 2022 compatible successor to KrbRelay

## How Sage uses this
SharpKrbRelay is the preferred Windows-side local PE tool over KrbRelay due to its
active maintenance and `-auto` CLSID discovery. The full chain:
1. SharpKrbRelay (or KrbRelay) → RBCD write on local machine
2. Rubeus asktgt for the attacker machine account
3. Rubeus s4u → SYSTEM-context service ticket
4. Use ticket for command execution (Apollo: inject or token manipulation)

## Output
Console output confirming DCOM activation, relay progress, and LDAP write result.
`[+]` prefixed lines indicate success. Error messages indicate CLSID mismatch or
privilege/connectivity issues.

## Full Reference

> Captured against SharpKrbRelay v1.x, 2026-05-29. Source: https://github.com/CICADA8-Research/SharpKrbRelay README.

### Primary flags

| Flag | Description |
|------|-------------|
| `-spn X` | Target SPN for relay |
| `-clsid X` | Specific CLSID to use for DCOM coercion |
| `-auto` | Auto-select a working CLSID |
| `-rbcd X` | SID of account to write to RBCD |
| `-shadowcred` | Perform shadow credential injection |
| `-port X` | Local listener port |
| `-v` | Verbose output |

### Source for this reference

- https://github.com/CICADA8-Research/SharpKrbRelay (README)
- CICADA8 blog posts
- Version: v1.x as of 2026-05-29
