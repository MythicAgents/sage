---
name: OPSEC Pre-Flight Checklist
category: defense-evasion
subcategories: [opsec, pre-flight, checklist, tradecraft]
tradecraft_tags: [opsec, checklist, tradecraft, pre-engagement, anti-forensics, reference]
mitre_attack: []
source:
  url: https://attack.mitre.org/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows, linux, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Reference document — no tools, no detection signal.
usage_examples:
  - description: Review before running any tool or technique
    args: "(operator reference)"
opsec_notes: |
  This checklist codifies Sage's OPSEC decision framework. Apply before any tool
  execution or technique selection.
gotchas: |
  OPSEC is always a trade-off between speed and stealth. In red team assessments,
  OPSEC decisions must align with the engagement scope and timeline.
related_ttps: [post-exploitation-playbook, lateral-movement-decision, etw-patching-bof, amsi-bypass]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# OPSEC Pre-Flight Checklist

Sage's operational security decision framework. Apply before each tool execution.

## Before Running ANY Tool

- [ ] **EDR identified?** (SharpEDRChecker — know what's watching)
- [ ] **Tool appropriate for EDR level?** (.NET assembly vs PowerShell vs BOF based on EDR)
- [ ] **Binary renamed?** (Never use the original tool name — "SharpHound", "Rubeus", "Seatbelt" are signatured)
- [ ] **Compiled from source?** (Public pre-compiled binaries have known hashes)
- [ ] **Execution method appropriate?** (Apollo inline_assembly > powershell_import > shell command)

## Before Making AD Changes

- [ ] **Change is reversible?** If not, get explicit operator approval
- [ ] **Scope confirmed?** (GPO changes affect all machines in scope — verify the GPO's OU)
- [ ] **Cleanup plan documented?** What needs to be removed after exploitation?
- [ ] **Shadow credentials → always clear after use** (Whisker clear /target:X)
- [ ] **Machine accounts → delete after use** (StandIn --computer delete OR AD cleanup)

## Before LSASS Access

- [ ] **PPL status known?** (Seatbelt LSASettings or direct reg query)
- [ ] **Credential Guard status?** (If enabled, LSASS dump won't yield plaintext creds)
- [ ] **Best tool for detected EDR?**
  - CrowdStrike Falcon: use nanodump (indirect syscalls + PssCaptureSnapshot)
  - Defender only: Apollo mimikatz or SharpKatz may work
  - SentinelOne: nanodump with --spoof-callstack + --werfault
  - No EDR: any approach works

## Before Lateral Movement

- [ ] **NTLM signing known?** (blocks relay attacks — check ntlm-disable-check.md)
- [ ] **Destination is in scope?**
- [ ] **Movement method appropriate for network monitoring level?**
  - Monitored: WinRM (least detectable) > WMI > SMB service
  - Unmonitored: any method

## Before Persistence

- [ ] **Duration: is persistence actually needed?** (Each artifact = more forensic evidence)
- [ ] **Cleanup commitment confirmed?** (Persistence is reversible but requires action)
- [ ] **Mechanism appropriate?**
  - Short-term (<24h): registry Run key (easy cleanup)
  - Medium-term (<1 week): scheduled task (moderate cleanup)
  - Long-term: WMI subscription or service (complex cleanup — need commitment)

## After Any Operation

- [ ] **Tool output reviewed** — any sensitive data to exfiltrate or delete?
- [ ] **Artifacts cleaned** — registry keys, scheduled tasks, shadow credentials, machine accounts
- [ ] **Tool binaries removed** — if uploaded to disk, delete after use
- [ ] **Temporary files cleaned** — LSASS dump files, output files in C:\Windows\Temp

## Noise Level Reference

| Operation | Noise level | Primary signals |
|-----------|-------------|----------------|
| LDAP enumeration | LOW | LDAP query volume |
| SharpHound DCOnly+Stealth | LOW-MEDIUM | LDAP queries to DC |
| SharpHound All | MEDIUM | LDAP + SAMR + SMB |
| LSASS dump (nanodump) | MEDIUM | Handle acquisition pattern |
| LSASS dump (comsvcs) | HIGH | Well-known pattern |
| DCSync | HIGH | Event 4662, network replication RPC |
| GPO modification | HIGH | Event 5136, SYSVOL write |
| Service creation | HIGH | Event 7045 |
| WMI subscription | HIGH | Event 5861 |
| Shadow credentials | HIGH | Event 5136 (msDS-KeyCredentialLink) |
