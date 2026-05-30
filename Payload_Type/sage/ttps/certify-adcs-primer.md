---
name: ADCS Attack Primer
category: adcs
subcategories: [adcs-overview, pki-primer, esc-overview, adcs-attack-surface]
tradecraft_tags: [adcs, pki, certificate-services, esc, primer, overview, specterops, certified-pre-owned]
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
  Reference document only. Detection per specific ESC in individual TTP files.
usage_examples:
  - description: Complete ADCS attack decision tree
    args: "(operator reference)"
opsec_notes: |
  ADCS attacks are among the stealthiest domain compromise paths — certificate-based
  authentication is normal behavior, and forged certificates are hard to distinguish
  from legitimate ones.
gotchas: |
  ADCS must be deployed in the environment. Verify with Certify find or Certipy find.
related_ttps: [certify, certify-v2, certipy, adcs-esc4, adcs-esc6, adcs-esc8,
               rubeus, pkinittools, whisker, forgecert, passthecert]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# ADCS Attack Primer

Reference guide for Active Directory Certificate Services (ADCS) attacks. Based on
SpecterOps' "Certified Pre-Owned" whitepaper (Will Schroeder + Lee Christensen, 2022).

## Why ADCS Is High-Value

1. **Universal**: ADCS is present in ~60-80% of enterprise environments
2. **Stealthy**: Certificate authentication looks identical to legitimate use
3. **Persistent**: Certificates outlast password changes
4. **Crack-free**: PKINIT + UnPAC eliminates need for offline hash cracking

## ADCS Attack Surface Map

```
          [Domain CA]
         /     |      \
        /      |       \
   [Templates] [CA ACLs] [Web Enrollment]
       |            |           |
   ESC1-4,6,9  ESC5,7     ESC8,ESC11
   (template)  (CA)       (web endpoint)
```

## ESC Quick Reference

| ESC | Category | Condition | Impact | Tool |
|-----|----------|-----------|--------|------|
| ESC1 | Template | ENROLLEE_SUPPLIES_SUBJECT | Any user → any cert | Certify, Certipy |
| ESC2 | Template | Any Purpose EKU | Cert for any auth | Certify |
| ESC3 | Template | Enrollment Agent | Cert on behalf of | Certify |
| ESC4 | Template ACL | Write rights on template | Convert to ESC1 | Certify + PowerView |
| ESC5 | Object ACL | Write on CA/PKI objects | CA-level control | Certipy |
| ESC6 | CA flag | EDITF_ATTRIBUTESUBJECTALTNAME2 | Any template = ESC1 | Certify |
| ESC7 | CA ACL | Manage CA/Certificates rights | Issue any cert | Certipy |
| ESC8 | Web enrollment | HTTP NTLM relay to ADCS | DC cert via relay | ntlmrelayx + Certipy |
| ESC9 | Template | No Security Extension | SAN bypass | Certipy v4+ |
| ESC10 | Domain | Weak cert mapping | Alt auth path | Certipy v4+ |
| ESC13 | Template | Issuance Policy OID | Group membership gain | Certify v2 |
| ESC14 | Template | DNS SAN requirement | DNS principal auth | Certify v2 |

## Attack Flow Decision Tree

```
Step 1: Does ADCS exist?
  Certify.exe find (or Certipy find)
  → If no templates/CAs found: ADCS not deployed; skip

Step 2: Any vulnerable templates (ESC1-8)?
  Certify.exe find /vulnerable
  → ESC1 found: FASTEST PATH
    Certify request /altname:administrator → Rubeus PKINIT → UnPAC hash → PTH

Step 3: No ESC1/3/6/8?
  Check ESC4 (template write access):
  BloodHound: ACL edge to certificate template objects?
  → Modify template + ESC1 exploit

Step 4: Check ESC5/7 (CA-level):
  Need Manage CA or Manage Certificates right
  Certipy find → check CA permissions

Step 5: Check ESC8 (web enrollment relay):
  Is http://CA/certsrv accessible without HTTPS enforcement?
  ntlmrelayx + coercion → DC cert → PKINIT

Step 6: Check newer ESCs (13-16):
  Certify v2 find /vulnerable
```

## The PKINIT → UnPAC Chain (No Cracking Required)

```
1. Certificate obtained (any ESC path)
2. Rubeus.exe asktgt /user:TARGET /certificate:<b64-pfx> /getcredentials /show /ptt
3. Output: NT hash for TARGET (from PAC_CREDENTIAL_INFO decryption)
4. No offline cracking needed
5. Use NT hash: Apollo pth → PTH or Rubeus asktgt (OPtH) → Kerberos
```

This is Sage's preferred path: any ADCS vulnerability → cert → hash → domain access.

## Source

"Certified Pre-Owned" — Will Schroeder (@harmj0y) & Lee Christensen (@tifkin_)
SpecterOps, 2022: https://specterops.io/wp-content/uploads/sites/3/2022/06/Certified_Pre-Owned.pdf
