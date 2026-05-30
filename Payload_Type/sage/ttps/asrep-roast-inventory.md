---
name: AS-REP Roasting Inventory (no-crack variant)
category: recon
subcategories: [asrep-roasting, preauth-disabled, account-discovery]
tradecraft_tags: [asrep-roast, pre-auth, kerberos, inventory, no-crack, account-classification]
mitre_attack:
  - id: T1558.004
    name: Steal or Forge Kerberos Tickets — AS-REP Roasting
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
  AS-REQ without pre-authentication for an account that has DONT_REQ_PREAUTH set
  generates Event 4768 with pre-auth type 0. MDI and SIEM Kerberos analytics detect
  mass AS-REQ without pre-auth patterns.
usage_examples:
  - description: Find accounts with pre-authentication disabled (no hash request)
    args: "asreproast /ldaponly"
  - description: List AS-REP-roastable accounts with attributes
    args: "asreproast /stats"
  - description: PowerView enumeration (no Rubeus needed, LDAP only)
    args: "Get-DomainUser -PreauthNotRequired"
opsec_notes: |
  `/ldaponly` is purely LDAP enumeration — it does NOT request AS-REP hashes, so
  there are no AS-REQ-without-preauth events. This is the inventory-only approach.
  AS-REP-roastable accounts are often service accounts with misconfigured settings —
  valuable targets for alternative exploitation (ADCS, delegation, shadow credentials).
gotchas: |
  This file documents the INVENTORY use — NOT the hash-capture use. AS-REP hashes
  (like kerberoast hashes) require offline cracking — not a Sage capability. Use
  /ldaponly to discover accounts, then pursue non-cracking paths against those accounts.
  DONT_REQ_PREAUTH accounts are often oversights — report to operator for remediation
  tracking as well as exploitation.
related_ttps: [rubeus, certify, sharphound, powerview, rubeus-kerberoast-nocrack]
alternatives: [powerview-preauthdisabled, getuserspns]
common_args:
  /ldaponly:
    description: LDAP-only enumeration (no AS-REP hash requests)
    typical_values: [flag-only]
  /stats:
    description: Show statistics about AS-REP-roastable accounts
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# AS-REP Roasting Inventory (no-crack variant)

The Rubeus asreproast command in INVENTORY mode (`/ldaponly`). Accounts with
`DONT_REQ_PREAUTH` set are AS-REP-roastable — they respond to unauthenticated AS-REQ
requests with an encrypted blob. The inventory variant discovers these accounts via
LDAP only (no actual AS-REQ, no hash capture) for use in alternative exploitation paths
that don't require offline cracking.

## Typical use cases
- Inventory accounts with DONT_REQ_PREAUTH for non-cracking exploitation (ADCS, shadow creds)
- Identify security misconfigurations for reporting and/or exploitation

## How Sage uses this
Same philosophy as kerberoast inventory — find accounts, then exploit via certificate or
delegation paths. Accounts with pre-auth disabled are often service accounts with write
access to resources, making them good candidates for shadow credential attacks.

## Important distinction
- `rubeus asreproast /ldaponly` → inventory, no hashes, no cracking → SAFE for Sage
- `rubeus asreproast` → requests AS-REP hashes for offline cracking → NOT Sage's workflow
