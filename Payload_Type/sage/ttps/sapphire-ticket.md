---
name: Sapphire Ticket
category: kerberos
subcategories: [ticket-modification, pac-modification, impersonation-without-s4u]
tradecraft_tags: [sapphire-ticket, kerberos, pac-modification, pkinit, impersonation, rubeus]
mitre_attack:
  - id: T1558.001
    name: Steal or Forge Kerberos Tickets — Golden Ticket
source:
  url: https://techcommunity.microsoft.com/t5/security-compliance-and-identity/new-attack-path-sapphire-ticket/ba-p/3724916
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Sapphire Ticket uses legitimate PKINIT AS-REQ and S4U2self operations — these are
  normal Kerberos flows that generate standard events. The resulting ticket comes from
  legitimate DC interactions and is valid by all standard checks. Detection requires
  behavioral analysis of the combination of operations. As of 2023 this remains one of
  the hardest-to-detect ticket techniques.
usage_examples:
  - description: Sapphire Ticket via Rubeus (requires certificate + krbtgt key)
    args: "Rubeus.exe asktgt /user:administrator /certificate:<base64-pfx> /password:<pfxpw> /domain:DOMAIN /enctype:aes /ptt"
  - description: Diamond variation using PKINIT TGT
    args: "(Sapphire ticket uses PKINIT TGT + S4U2self to get service tickets for the impersonated user without S4U2proxy)"
opsec_notes: |
  Sapphire Ticket leverages PKINIT authentication to obtain a TGT, then uses S4U2self
  with that TGT to get service tickets that impersonate a target user. Because both
  PKINIT and S4U2self are legitimate Kerberos flows, detection is extremely difficult.
  Requires: a certificate for a high-privilege account (from ADCS abuse) and an account
  that can perform S4U2self (constrained delegation with protocol transition).
gotchas: |
  Sapphire Ticket is a variant that combines PKINIT (from ADCS) with S4U2self. It's
  documented as a post-PKINIT technique — after Whisker/Certify/Certipy obtains a
  PKINIT-valid certificate, the resulting TGT can be used with S4U2self to impersonate
  users WITHOUT requiring the S4U account to have explicit constrained delegation.
  This bypasses the "need constrained delegation configured" requirement. Relatively new
  technique (2022-2023).
related_ttps: [certify, whisker, rubeus, diamond-ticket, golden-ticket]
alternatives: [diamond-ticket, golden-ticket, constrained-delegation-abuse]
common_args: {}
last_updated: 2026-05-29
---

# Sapphire Ticket

An advanced Kerberos technique combining PKINIT certificate authentication with
S4U2self to obtain service tickets that impersonate high-value users without relying
on constrained delegation configuration. After obtaining a PKINIT-valid certificate
(from ADCS abuse), the resulting TGT enables S4U2self impersonation tickets that
bypass standard S4U detection.

## Technique Summary

```
Standard Kerberos flow: TGT → S4U2self (requires constrained delegation)
Sapphire Ticket flow: Certificate → PKINIT TGT → S4U2self (bypasses delegation requirement)
```

The PKINIT TGT confers different privilege attributes than a password-based TGT,
enabling S4U2self operations that wouldn't otherwise be permitted.

## Context

Sapphire Ticket was described in 2022-2023 as a post-exploitation technique for
maintaining access after ADCS compromise. It represents the leading edge of Kerberos
ticket technique evolution. For most engagements, Golden/Diamond tickets are sufficient;
Sapphire Ticket is for environments with very mature detection capable of catching
standard forging techniques.

## How Sage uses this

This is an advanced technique documented for awareness. Sage's primary ADCS chain
(Certify → Rubeus PKINIT → UnPAC-the-hash) doesn't need Sapphire Ticket for most
operations. This technique is relevant when standard ticket techniques are detected
and a more covert approach is needed.
