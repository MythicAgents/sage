---
name: PassTheCert
category: adcs
subcategories: [certificate-auth, ldap-auth, privilege-escalation]
tradecraft_tags: [adcs, certificate, ldap, schannel, passtheticket-equivalent, dotnet]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/AlmondOffSec/PassTheCert
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: PassTheCert.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  PassTheCert uses certificate-based authentication (TLS client auth) to the LDAP/S
  service on the DC, then performs LDAP operations. The certificate authentication
  event appears in Windows Security logs (Event 4768 with certificate auth, Kerberos
  pre-auth type 15 or LDAP TLS auth event). LDAP attribute writes (Event 5136) are
  the downstream signal for RBCD/shadow-cred operations.
usage_examples:
  - description: Add shadow credential to user account via cert-based LDAP auth
    args: "--server DC01.north.sevenkingdoms.local --cert-path admin.pfx --cert-password P@ss --action shadowCredentials --target jon.snow"
  - description: Grant DCSync rights to an account
    args: "--server DC01.north.sevenkingdoms.local --cert-path admin.pfx --cert-password P@ss --action dcsync --target attacker"
  - description: Add user to group
    args: "--server DC01.north.sevenkingdoms.local --cert-path admin.pfx --cert-password P@ss --action addGroupMember --group 'Domain Admins' --target attacker"
  - description: Set RBCD on a machine account
    args: "--server DC01.north.sevenkingdoms.local --cert-path admin.pfx --cert-password P@ss --action rbcd --target VICTIM$ --sid S-1-5-21-...-COMPUTERSID"
opsec_notes: |
  PassTheCert authenticates via TLS client certificate to LDAP — this is unusual for
  most domain accounts and may be detected by LDAP audit logging. The LDAP operations
  (attribute writes) generate Event 5136. Prefer using Rubeus PKINIT to get a normal
  TGT and then using that TGT for LDAP ops (more normal traffic pattern) over direct
  certificate-based LDAP auth if detection is a concern.
gotchas: |
  Requires a valid certificate for an account that has the necessary LDAP rights for
  the target action. DCSync grant requires the granting account to have WriteDACL on the
  domain object. Shadow credential add requires write to msDS-KeyCredentialLink. LDAP
  signing enforcement (LDAP channel binding) may block PassTheCert on modern DCs —
  check `LdapEnforceChannelBinding` registry value. Use LDAPS (port 636) for TLS-based auth.
related_ttps: [certify, forgecert, rubeus, pkinittools, whisker]
alternatives: [rubeus-asktgt-ptt, certipy, impacket-passthecert]
common_args:
  --server:
    description: DC FQDN or IP to connect to
    typical_values: ["DC01.north.sevenkingdoms.local"]
    required: true
  --cert-path:
    description: Path to PFX certificate file
    typical_values: ["admin.pfx"]
    required: true
  --cert-password:
    description: PFX password
    typical_values: ["P@ss123"]
  --action:
    description: LDAP operation to perform
    typical_values: [shadowCredentials, dcsync, addGroupMember, rbcd, setOwner, setGenericAll]
    required: true
  --target:
    description: Target account or object for the action
    typical_values: ["jon.snow", "administrator", "VICTIM$"]
    required: true
last_updated: 2026-05-29
---

# PassTheCert

Abuses certificate-based LDAP authentication (TLS client certificates, Schannel) to
perform privileged LDAP operations on behalf of an account whose certificate you hold.
Rather than converting a certificate to a Kerberos TGT (Rubeus PKINIT), PassTheCert
authenticates directly to LDAP/S using the certificate, then performs actions like adding
shadow credentials, granting DCSync rights, or setting RBCD — all via the certificate's
identity without requiring a Kerberos ticket.

## Typical use cases
- Grant DCSync rights to an attacker-controlled account using a DA-level certificate
- Add shadow credentials to an account using cert-based LDAP write
- Perform RBCD operations with certificate auth when PKINIT is blocked
- Add domain group membership directly via certificate-authenticated LDAP

## How Sage uses this
PassTheCert appears in ADCS exploitation chains when direct LDAP operations are needed
from a certificate. The typical scenario: ESC1/ForgeCert produces a DA-level certificate →
PassTheCert grants DCSync rights to attacker-controlled account → Apollo `dcsync` or
Mimikatz DCSync to pull krbtgt hash.

## Output
Text confirmation of LDAP operation success or failure.
