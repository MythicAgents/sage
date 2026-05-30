---
name: KrbRelayUp
category: privilege-escalation
subcategories: [kerberos-relay, rbcd, local-pe, coercion-relay]
tradecraft_tags: [kerberos, relay, rbcd, privilege-escalation, local-pe, dotnet]
mitre_attack:
  - id: T1134.002
    name: Access Token Manipulation — Create Process with Token
source:
  url: https://github.com/Dec0ne/KrbRelayUp
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: KrbRelayUp.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  KrbRelayUp automates the KrbRelay + RBCD chain in a single command. Detection signals:
  DCOM activation events, computer account creation (Event 4741), LDAP RBCD write
  (Event 5136), and Kerberos S4U ticket requests from unusual sources. MDI detects RBCD
  attribute writes. The full automation makes it faster to execute but also means all
  signals appear in a tight temporal window.
usage_examples:
  - description: Full automated local PE — create machine account, RBCD, S4U, get SYSTEM shell
    args: "full -m rbcd -cls 90f18417-f0f1-484e-9d3c-59dceee5dbd8"
  - description: Full PE with shadow credentials (no machine account creation needed)
    args: "full -m shadowcred -cls 90f18417-f0f1-484e-9d3c-59dceee5dbd8"
  - description: Only perform the relay step (manual follow-up)
    args: "relay -cls 90f18417-f0f1-484e-9d3c-59dceee5dbd8 -m rbcd"
  - description: Full chain with auto CLSID discovery
    args: "full -m rbcd --auto"
opsec_notes: |
  KrbRelayUp chains multiple high-signal operations in sequence: DCOM activation,
  machine account creation, LDAP write, and S4U ticket generation. Each is individually
  detectable; the combination in a tight time window is a very strong detection signal for
  MDI and behavioral EDR. Use on engagements where detection has already occurred or in
  environments with immature detection capabilities. For stealthy approaches, execute
  StandIn (machine account creation) and SharpKrbRelay (relay) separately with time gaps.
gotchas: |
  Requires domain membership and machine account quota > 0 (for RBCD mode). The -cls
  (CLSID) must be one that runs as SYSTEM and is activatable by current user — use
  `--auto` for auto-discovery. Shadow credential mode (`-m shadowcred`) doesn't require
  machine account quota but requires ADCS/PKINIT support. The full SYSTEM shell is
  obtained via the S4U2self chain — a Rubeus-equivalent is embedded. Machine accounts
  created by KrbRelayUp should be cleaned up after use.
related_ttps: [krbrelay, sharpkrbrelay, standin, rubeus, whisker]
alternatives: [krbrelay, sharpkrbrelay]
common_args:
  full:
    description: Automated full chain (RBCD setup + exploit)
    typical_values: [flag-only]
    required: false
  relay:
    description: Relay-only step (manual setup of RBCD before this)
    typical_values: [flag-only]
  -m:
    name: -m
    description: Mode — rbcd or shadowcred
    typical_values: [rbcd, shadowcred]
    required: true
  -cls:
    name: -cls
    description: CLSID for DCOM coercion trigger
    typical_values: ["90f18417-f0f1-484e-9d3c-59dceee5dbd8"]
  --auto:
    description: Auto-discover a working CLSID
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# KrbRelayUp

An automated "one-shot" Kerberos relay → RBCD → local privilege escalation tool by
Dec0ne. KrbRelayUp wraps the complete KrbRelay + RBCD chain into a single command:
DCOM coercion, LDAP relay to write RBCD, Rubeus-equivalent S4U2self chain, and
SYSTEM shell execution. The `full` command is the "press one button, get SYSTEM" local
PE primitive for domain-joined machines.

## Typical use cases
- One-command local privilege escalation on domain-joined machines
- Quick SYSTEM access from a domain user shell without manual multi-step relay setup
- Shadow credentials mode for environments with ADCS but strict machine account quotas

## How Sage uses this
KrbRelayUp is the fastest local PE path when RBCD prerequisites are met and detection
risk is acceptable. Sage may use it when SharpUp identifies SeImpersonatePrivilege is
absent (ruling out potato exploits) but domain membership is present.

## Output
Progress output showing each step of the chain, culminating in a SYSTEM-level shell
or command execution confirmation.

## Full Reference

> Captured against KrbRelayUp v1.3.x, 2026-05-29. Source: https://github.com/Dec0ne/KrbRelayUp README.

### Sub-commands

| Sub-command | Description |
|-------------|-------------|
| `full` | Complete automated chain: relay + RBCD (or shadowcred) + S4U |
| `relay` | Only perform the relay and LDAP write step |
| `spawn` | Spawn a SYSTEM process (after relay step completed separately) |

### Full argument listing

| Arg | Description |
|-----|-------------|
| `-m X` | Mode: `rbcd` (creates machine account, writes RBCD) or `shadowcred` (writes shadow cred) |
| `-cls X` | CLSID for DCOM activation |
| `--auto` | Auto-discover working CLSID |
| `-domain X` | Domain FQDN (defaults to current) |
| `-dc X` | Domain controller FQDN |
| `-cn X` | Computer account name for RBCD mode (default: random) |
| `-cp X` | Password for new computer account |
| `-p X` | Process to spawn as SYSTEM (default: cmd.exe) |

### Source for this reference

- https://github.com/Dec0ne/KrbRelayUp (README)
- Version: v1.3.x as of 2026-05-29
