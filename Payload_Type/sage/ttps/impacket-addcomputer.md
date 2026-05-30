---
name: impacket-addcomputer
category: acl-abuse
subcategories: [machine-account-creation, rbcd, computer-object]
tradecraft_tags: [impacket, machine-account, python, rbcd, linux-side, computer-creation]
mitre_attack:
  - id: T1098
    name: Account Manipulation
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: addcomputer.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Machine account creation generates Event 4741 (computer account created) on the DC.
  LDAP write operation from non-Windows client is slightly different protocol pattern
  but generates the same AD audit event.
usage_examples:
  - description: Create a machine account from Linux
    args: "addcomputer.py -computer-name mypc01 -computer-pass 'P@ssw0rd1!' north.sevenkingdoms.local/jon.snow:Password123"
  - description: Create with LDAPS
    args: "addcomputer.py -computer-name mypc01 -computer-pass 'P@ssw0rd1!' -ldap-port 636 north.sevenkingdoms.local/jon.snow:Password123"
opsec_notes: |
  Python-only — infrastructure side. For Windows-side machine account creation from within
  Apollo, use StandIn or Powermad. addcomputer.py is the Linux alternative for the same
  operation.
gotchas: |
  Python-only. Machine account quota (MAQ) must be > 0. Same prerequisites as StandIn.
  After creating, use impacket getST.py or Certipy for the RBCD chain from Linux.
related_ttps: [standin, powermad, impacket-gettst, ntlmrelayx]
alternatives: [standin, powermad]
common_args:
  -computer-name:
    description: Machine account name (without $ suffix)
    typical_values: ["mypc01"]
    required: true
  -computer-pass:
    description: Password for the new computer account
    typical_values: ["P@ssw0rd1!"]
    required: true
  target:
    description: DOMAIN/user:pass
    typical_values: ["north.sevenkingdoms.local/jon.snow:Password123"]
    required: true
last_updated: 2026-05-29
---

# impacket-addcomputer

impacket's `addcomputer.py` — Python-side machine account creation for RBCD setup from
Linux infrastructure. The Linux equivalent of StandIn's `--computer` flag.

## Typical use cases
- Create machine accounts for RBCD from Linux attack infrastructure
- Part of the Linux-side RBCD exploitation chain (addcomputer → ACL write → getST)

## How Sage uses this
Infrastructure-side Python tool. StandIn is preferred for Apollo-based Windows agents.
addcomputer.py is used in Linux-side RBCD chains.
