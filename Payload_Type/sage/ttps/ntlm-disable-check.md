---
name: NTLM Restriction and Relay Defense Check
category: recon
subcategories: [ntlm-checks, relay-defense, ldap-signing, channel-binding]
tradecraft_tags: [ntlm, relay-defense, ldap-signing, channel-binding, epa, configuration-check]
mitre_attack: []
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
  These are registry reads and LDAP queries — very low detection signal.
usage_examples:
  - description: Check NTLM restriction level on DCs
    args: "reg query HKLM\\SYSTEM\\CurrentControlSet\\Services\\Netlogon\\Parameters /v RestrictNTLMInDomain"
  - description: Check LDAP signing enforcement
    args: "reg query HKLM\\SYSTEM\\CurrentControlSet\\Services\\NTDS\\Parameters /v LDAPServerIntegrity"
  - description: Check LDAP channel binding
    args: "reg query HKLM\\SYSTEM\\CurrentControlSet\\Services\\NTDS\\Parameters /v LdapEnforceChannelBinding"
  - description: Check SMB signing
    args: "reg query HKLM\\SYSTEM\\CurrentControlSet\\Services\\LanManServer\\Parameters /v RequireSecuritySignature"
  - description: Check via PowerView
    args: "Get-DomainController | Get-DomainObject -Properties ms-DS-MachineAccountQuota,lockoutThreshold"
opsec_notes: |
  Registry reads are extremely low-signal. These checks take seconds and determine
  whether NTLM relay attacks (ntlmrelayx, Responder) are viable in the environment.
  Check these BEFORE setting up relay infrastructure to avoid wasted effort.
gotchas: |
  These values are per-host (DCs may have different settings than member servers).
  LDAP signing enforced blocks SMB-to-LDAP relay. Channel binding enforced blocks
  TLS-based relay. SMB signing enforced blocks SMB-to-SMB relay. Even one block
  can prevent a specific relay chain — check all.
related_ttps: [ntlmrelayx, responder, coercer, petitpotam]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# NTLM Restriction and Relay Defense Check

Pre-relay reconnaissance to determine if NTLM relay attacks are viable in the target
environment. Check these registry values before investing time in relay infrastructure.

## Critical Checks Before NTLM Relay

### LDAP Signing (blocks SMB→LDAP relay)

```reg
reg query "HKLM\SYSTEM\CurrentControlSet\Services\NTDS\Parameters" /v LDAPServerIntegrity
```
| Value | Meaning | Relay viable? |
|-------|---------|--------------|
| 0 = None | LDAP signing not required | YES |
| 1 = Negotiate | Signing negotiated (clients may skip) | PARTIAL |
| 2 = Required | LDAP signing required | NO (blocks relay) |

### LDAP Channel Binding (blocks TLS relay)

```reg
reg query "HKLM\SYSTEM\CurrentControlSet\Services\NTDS\Parameters" /v LdapEnforceChannelBinding
```
| Value | Meaning | LDAPS relay viable? |
|-------|---------|-------------------|
| 0 = Never | Channel binding not enforced | YES |
| 1 = When supported | Enforced when supported by client | PARTIAL |
| 2 = Always | Always enforced | NO |

### SMB Signing (blocks SMB→SMB relay)

```reg
reg query "HKLM\SYSTEM\CurrentControlSet\Services\LanManServer\Parameters" /v RequireSecuritySignature
```
| Value | Meaning | SMB relay viable? |
|-------|---------|-----------------|
| 0 = Not required | SMB signing optional | YES |
| 1 = Required | SMB signing required | NO |

### Relay Attack Matrix

| Attack | Blocked by |
|--------|-----------|
| ntlmrelayx → LDAP | LDAP signing (value 2) |
| ntlmrelayx → LDAPS | Channel binding (value 2) |
| ntlmrelayx → SMB | SMB signing (value 1) |
| ntlmrelayx → ADCS web enrollment | Nothing (HTTP endpoint, no signing) |
| ntlmrelayx → MSSQL | Nothing by default |

## Quick Check via Seatbelt

```
Seatbelt.exe NTLMSettings LDAPSigningSettings SMBSigningSettings
```

## Implication for Sage

If LDAP signing = 2 AND channel binding = 2:
→ Standard NTLM relay to LDAP is blocked
→ Consider: ADCS web enrollment relay (ESC8) — HTTP, not affected
→ Consider: KrbRelay / SharpKrbRelay (Kerberos relay, not NTLM relay)
→ Consider: MSSQL relay if SQL Server is accessible
