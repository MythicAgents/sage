---
name: Diamond Ticket
category: kerberos
subcategories: [ticket-modification, tgt-modification, dc-validated]
tradecraft_tags: [diamond-ticket, kerberos, tgt-modification, dc-validated, undetectable, rubeus]
mitre_attack:
  - id: T1558.001
    name: Steal or Forge Kerberos Tickets — Golden Ticket
source:
  url: https://www.trustedsec.com/blog/a-diamond-in-the-ruff/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Diamond Ticket is specifically designed to evade Golden Ticket detection. The ticket
  IS validated by the KDC (it's a real TGT with a real PAC, just modified). Detection
  requires deep inspection of ticket content vs. expected LDAP user attributes — very
  few solutions do this effectively. The AS-REQ/AS-REP exchange is normal.
usage_examples:
  - description: Request a Diamond Ticket (Rubeus diamond command)
    args: "Rubeus.exe diamond /user:lowprivuser /password:Password123 /krbkey:<krbtgt-aes256-key> /enctype:aes /ticketuser:Administrator /ticketuserid:500 /groups:512 /ptt"
  - description: Diamond Ticket with NT hash
    args: "Rubeus.exe diamond /user:lowprivuser /rc4:<user-hash> /krbkey:<krbtgt-hash> /ticketuser:Administrator /ticketuserid:500 /groups:512 /ptt"
opsec_notes: |
  Diamond Ticket obtains a REAL TGT from the KDC (as a low-privilege user) then modifies
  it to claim higher-privilege group memberships — using the krbtgt key to re-sign.
  Unlike Golden Ticket (entirely forged), Diamond Ticket has real KDC-validated components.
  MDI Golden Ticket detection looks for PAC content that doesn't match LDAP user attributes;
  Diamond Ticket addresses this by starting with a real ticket and modifying only the groups.
gotchas: |
  Diamond Ticket requires the krbtgt's AES256 key (or NT hash for RC4 variant) — same
  as Golden Ticket. The krbtgt AES key is in LSASS on DCs and obtainable via DCSync.
  The technique was described in TrustedSec research (2022). Rubeus' `diamond` command
  implements it. Requires Rubeus v2.x+.
related_ttps: [rubeus, mimikatz, golden-ticket, impacket-secretsdump, constrained-delegation-abuse]
alternatives: [golden-ticket, sapphire-ticket]
common_args:
  /user:
    description: Real low-privilege domain user to request the initial TGT for
    typical_values: ["lowprivuser"]
    required: true
  /password:
    description: Real user's password
    typical_values: ["Password123"]
  /rc4:
    description: Real user's NT hash (instead of password)
    typical_values: ["<nthash>"]
  /krbkey:
    description: krbtgt AES256 or NT hash for ticket re-signing
    typical_values: ["<krbtgt-aes256-or-nthash>"]
    required: true
  /ticketuser:
    description: Username to put in the modified ticket's PAC
    typical_values: ["Administrator"]
    required: true
  /ticketuserid:
    description: User RID for the impersonated account (500 = Administrator)
    typical_values: [500]
    required: true
  /groups:
    description: Comma-separated group RIDs to claim in the modified ticket
    typical_values: ["512,519,518,516,520"]
  /ptt:
    description: Inject the resulting ticket
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Diamond Ticket

A stealthier alternative to Golden Ticket that obtains a real TGT from the KDC and
then modifies the PAC to claim elevated group memberships before re-signing with the
krbtgt key. Unlike Golden Ticket (entirely fabricated), Diamond Ticket starts with a
legitimately-issued TGT — making it harder for detection systems that compare TGT
content against expected DC-side attributes.

## Diamond Ticket vs Golden Ticket

| Property | Golden Ticket | Diamond Ticket |
|----------|--------------|----------------|
| KDC interaction | None | AS-REQ/AS-REP (normal) |
| Base ticket | Entirely forged | Real, KDC-issued |
| PAC source | Fabricated | Real, then modified |
| MDI Golden Ticket detection | HIGH risk | LOW risk |
| krbtgt requirement | NT hash or AES | AES256 key preferred |

## The Technique

```
1. Request a real TGT as a low-privilege user (via AS-REQ)
2. Decrypt the TGT PAC using the krbtgt key
3. Modify the PAC: add Admin RID (500), Domain Admins (512), Enterprise Admins (519)
4. Re-encrypt and re-sign with the krbtgt key
5. Inject the modified TGT
```

Rubeus' `diamond` command automates all steps.

## When to Use

Use Diamond Ticket instead of Golden Ticket when:
- MDI Golden Ticket detection is active in the environment
- Long-term persistence with stealth is required
- The krbtgt AES256 key is available (preferred over RC4 for this technique)
