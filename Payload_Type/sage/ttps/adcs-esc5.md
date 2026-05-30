---
name: ADCS ESC5 — Object Control Vulnerability
category: adcs
subcategories: [esc5, object-control, ca-object-acl, pki-object-acl]
tradecraft_tags: [adcs, esc5, object-control, acl, ca-object, certipy, specterops]
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
  ESC5 involves ACL modification on PKI objects (CA computer, CA container objects in
  AD). Object modification generates Event 5136. CA configuration changes generate CA
  audit events. Certipy tracks ESC5 via DACL inspection on PKI infrastructure objects.
usage_examples:
  - description: Detect ESC5 vulnerabilities
    args: "certipy find -u user -p pass -dc-ip DC_IP -vulnerable"
  - description: Exploit ESC5 — control CA host → extract CA private key → ForgeCert
    args: "(gain SYSTEM on CA machine → SharpDPAPI/mimikatz to extract CA private key → ForgeCert)"
opsec_notes: |
  ESC5 covers a broader class of PKI infrastructure object vulnerabilities than ESC4
  (template ACLs). If a low-privileged principal has write access to:
  - The CA computer object
  - The CA's container in AD
  - NTAuthCertificates or AIA/CDP objects
  Then they may be able to compromise the CA itself or modify its behavior.
  ESC5 is less commonly exploitable than ESC1/3/4 but represents CA-level compromise.
gotchas: |
  ESC5 exploitation often leads to CA compromise → CA private key extraction →
  ForgeCert for unlimited domain persistence. This is a high-value but also high-signal
  operation. Only Certipy (not Certify) fully enumerates ESC5 vulnerabilities.
  The exploitation path depends on what specific object control exists.
related_ttps: [certipy, certify-v2, adcs-esc7, forgecert, pspkiaudit]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# ADCS ESC5 — Object Control Vulnerability

ESC5 covers misconfigured ACLs on PKI-related AD objects that could allow a low-privileged
principal to gain control over Certificate Authority infrastructure. Unlike ESC1-4
(template-level vulnerabilities) and ESC6-7 (CA configuration), ESC5 targets the
structural AD objects supporting the PKI infrastructure.

## ESC5 Object Categories

| Object | What control gives you |
|--------|----------------------|
| CA computer object (GenericAll/GenericWrite) | Can take over the CA host → extract private key |
| CA container in AD (WriteDACL) | Can modify CA-level ACLs → escalate to ESC7 |
| NTAuthCertificates (WriteDACL) | Can add rogue CA → forge certs trusted by domain |
| AIA/CDP objects | Can modify certificate publication paths |
| Root CA trust store | Can add rogue trusted CA |

## NTAuthCertificates Abuse (Most Impactful ESC5 Path)

NTAuthCertificates is an AD object that contains certificates of CAs trusted for
domain authentication. If an attacker can write to this object:

```
1. Generate a self-signed CA certificate
2. Add the CA cert to NTAuthCertificates (WriteDACL → GenericAll → write)
3. Use ForgeCert with the attacker's CA private key to forge certs for any user
4. PKINIT with the forged cert → TGT for any domain account

→ This is the most stealthy domain persistence: no existing CA is modified
```

## Detection

Only Certipy's `find` with full PKI inspection detects ESC5:
```
certipy find -u user -p pass -dc-ip DC_IP -text
# Look for: "ESC5" or unusual ACLs on PKI objects
```

## Relationship to Other ESCs

ESC5 often leads to ESC7 (CA ACL abuse) as the exploitation pathway — gaining write
access to CA objects allows granting ManageCA/ManageCertificates rights to the attacker,
which then enables ESC7 exploitation.
