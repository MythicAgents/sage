---
name: Rubeus Kerberoast Inventory (no-crack variant)
category: recon
subcategories: [kerberoast-inventory, spn-enumeration, account-discovery]
tradecraft_tags: [kerberoast, spn, inventory, no-crack, account-classification, rubeus]
mitre_attack:
  - id: T1558.003
    name: Steal or Forge Kerberos Tickets — Kerberoasting
source:
  url: https://github.com/GhostPack/Rubeus
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Rubeus.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  TGS requests for all SPN accounts appear in DC Kerberos logs (Event 4769).
  Mass TGS request in a short timeframe is a strong kerberoast indicator in MDI
  analytics. /ldaponly flag reduces network noise by only doing LDAP enumeration.
usage_examples:
  - description: Enumerate kerberoastable accounts (LDAP-only, no TGS request)
    args: "kerberoast /ldaponly"
  - description: List kerberoastable accounts with SPN and account attributes
    args: "kerberoast /stats"
  - description: Request AES-encrypted TGS (for accounts where AES is available — cannot be cracked as RC4)
    args: "kerberoast /aes /nowrap"
opsec_notes: |
  `/ldaponly` is purely LDAP enumeration — it does NOT request TGS tickets, so there
  are no Event 4769 TGS request events. This gives an inventory of kerberoastable
  accounts with zero kerberoast detection signal. The resulting account list can
  inform delegation or ADCS exploitation (target the kerberoastable service account
  via ESC1/3 instead of cracking).
gotchas: |
  This file documents the INVENTORY use of Rubeus kerberoast — NOT the hash-capture
  use. Sage uses /ldaponly to discover kerberoastable accounts and then pursues
  alternative exploitation paths (ADCS, delegation abuse) rather than requesting
  and attempting to crack TGS hashes. Full kerberoast (without /ldaponly) requires
  offline cracking — not a Sage capability.
related_ttps: [rubeus, certify, sharphound, getuserspns]
alternatives: [sharphound-spntargets, getuserspns-enumerate-only, powerview-get-domainuser-spn]
common_args:
  /ldaponly:
    description: LDAP-only SPN enumeration (no TGS tickets requested)
    typical_values: [flag-only]
  /stats:
    description: Show statistics about kerberoastable accounts
    typical_values: [flag-only]
  /aes:
    description: Request AES-encrypted TGS (if RC4-only cracking is desired, do NOT use this)
    typical_values: [flag-only]
  /nowrap:
    description: Single-line output
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Rubeus Kerberoast Inventory (no-crack variant)

The Rubeus kerberoast command used in INVENTORY mode — `/ldaponly` enumerates SPN-mapped
accounts via LDAP only (no TGS ticket requests, no hashcat input). This gives Sage a
catalog of kerberoastable service accounts that can be targeted via alternative paths
(ADCS certificate request, delegation abuse, shadow credentials) without requiring offline
hash cracking.

This is NOT a hash-cracking technique — it is reconnaissance for alternative exploitation
paths targeting those same service accounts.

## Typical use cases
- Inventory all kerberoastable accounts for attack path planning (zero detection signal with /ldaponly)
- Identify which service accounts have SPNs to target via ADCS or delegation abuse
- Enumerate account attributes (adminCount, encryption type) to prioritize targets

## How Sage uses this
When SharpHound is available, kerberoastable accounts are already in BloodHound. When
Sage needs a fresh or targeted SPN inventory, `rubeus kerberoast /ldaponly` provides
it without requesting any TGS tickets. The resulting account list informs:
1. ADCS ESC1/3 targeting — if the service account can enroll in a vulnerable template
2. Constrained delegation chain targeting — if the service account has delegation configured
3. Shadow credentials — if an attacker has GenericWrite on the service account

## Output
Text listing of SPN-mapped accounts with their SPN values, encryption types, and
domain info. No ticket bytes. No hashcat-format output.

## Important distinction
- `rubeus kerberoast /ldaponly` → inventory, no tickets, no cracking → SAFE for Sage
- `rubeus kerberoast` → requests TGS tickets for offline cracking → NOT Sage's workflow
