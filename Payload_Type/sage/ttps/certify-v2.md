---
name: Certify v2 (ESC13–ESC16)
category: adcs
subcategories: [certify-v2, esc13, esc14, esc15, esc16, issuance-policy]
tradecraft_tags: [adcs, certify, esc13, esc14, esc15, esc16, ghostpack, newer-escs]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/GhostPack/Certify
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Certify.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Same as certify.md. Certify v2.0 (2025) adds detection/exploitation for newer ESC
  categories. Detection is identical to the base Certify file.
usage_examples:
  - description: Find all ESC13 vulnerabilities (Issuance Policy OID → group mapping abuse)
    args: "Certify.exe find /vulnerable"
  - description: Request cert exploiting ESC13 (issuance policy grants group membership)
    args: "Certify.exe request /ca:CA /template:PolicyTemplate /policy:<OID>"
  - description: ESC15 — Application Policy OID abuse
    args: "Certify.exe find /vulnerable"
opsec_notes: |
  Certify v2.0 was released in 2025 and adds ESC13-ESC16 detection. If the target
  environment has patched ESC1-8 but not the newer ESCs, Certify v2 surfaces them.
  The detection and exploitation approach is the same as Certify v1 — LDAP queries
  to PKI container, certificate enrollment requests.
gotchas: |
  This file supplements certify.md with the newer ESC categories. Certify v2.0 must
  be used (not older Certify v1.x) for ESC13-16 detection. If the version of Certify.exe
  you have doesn't detect these, update to v2.0 from the GhostPack repo.
related_ttps: [certify, certipy, adcs-esc4, adcs-esc6, adcs-esc8, rubeus, whisker]
alternatives: [certipy]
common_args: {}
last_updated: 2026-05-29
---

# Certify v2 — Newer ESC Categories (ESC13–ESC16)

Certify v2.0 (released 2025, GhostPack) adds detection and exploitation for newly
discovered ADCS vulnerability classes beyond the original ESC1-8.

## New ESC Categories (v2.0)

### ESC13 — Issuance Policy OID Group Membership Abuse

A certificate template that uses an Application Policy or Issuance Policy OID can be
configured to automatically add the holder to a specific AD group upon enrollment.
If a low-privilege user can enroll in a template with a privileged policy OID, they
gain that group's membership via certificate.

```
Identification:
  Certify.exe find /vulnerable → look for "ESC13" or policy OID mappings

Exploitation:
  Certify.exe request /ca:CA /template:PolicyTemplate
  → Certificate issued with the Issuance Policy OID
  → Certificate holder gains membership in the mapped privileged group
  → Rubeus PKINIT with the cert → TGT with the group's privileges
```

### ESC14 — CT_FLAG_SUBJECT_ALT_REQUIRE_DNS Abuse

Certificate templates configured to include the DNS name from the Subject Alternative
Name extension, combined with specific template settings, allow enrollment with arbitrary
DNS names — enabling authentication as server principal names.

### ESC15 — Weak Certificate Mapping + Application Policy

Similar to ESC10 but exploiting Application Policy OIDs in certificate templates
for cross-context authentication abuse.

### ESC16 — CA-Level Application Policy Misconfiguration

CA-level application policy settings that enable alternate authentication paths
not covered by ESC6's EDITF_ATTRIBUTESUBJECTALTNAME2 check.

## Detection with Certify v2

```
Certify.exe find /vulnerable
```
Certify v2 automatically detects and classifies ESC1-16. The output section headers
indicate which ESC category was found.

## Why ESC13+ Matters in 2026

After SpecterOps published "Certified Pre-Owned" (2022) and organizations patched
ESC1-8, a new generation of misconfigured templates involving Policy OIDs and newer
certificate features emerged as exploitation targets. ESC13 in particular is common
in organizations that use certificate-based authentication for specific privileged access
(PAW workstations, jump servers) — the policy OID mechanism is often misconfigured.

## Using Certify v2 with Apollo

Certify v2.0 is fully compatible with Apollo's inline_assembly. The upgrade from v1
to v2 is transparent — same commands, same LDAP enumeration, extended output format.
