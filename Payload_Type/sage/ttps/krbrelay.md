---
name: KrbRelay
category: coercion-relay
subcategories: [kerberos-relay, rbcd, privilege-escalation, local-pe]
tradecraft_tags: [kerberos, relay, rbcd, rbcd-local, privilege-escalation, coercion]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://github.com/cube0x0/KrbRelay
  license: MIT
  maintained: false
binary_type: .net-assembly
binary_filename: KrbRelay.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  KrbRelay involves COM object activation (DCOM calls visible in Sysmon), Kerberos
  ticket requests, and LDAP writes to the DC (msDS-AllowedToActOnBehalfOfOtherIdentity
  for RBCD). Event 5136 captures the LDAP attribute write. MDI monitors for RBCD
  attribute writes from unusual sources. Sysmon event 3 may capture local loopback
  connections made during relay.
usage_examples:
  - description: Local privesc via RBCD — relay loopback auth to LDAP, add RBCD, get admin shell
    args: "-spn ldap/dc01.north.sevenkingdoms.local -clsid 90f18417-f0f1-484e-9d3c-59dceee5dbd8 -rbcd S-1-5-21-...-1234"
  - description: Relay to LDAP for shadow credentials (add KeyCredential to own machine$)
    args: "-spn ldap/dc01.north.sevenkingdoms.local -clsid 90f18417-f0f1-484e-9d3c-59dceee5dbd8 -shadowcred"
  - description: Relay to LDAP for adding the machine account to local admin
    args: "-spn ldap/dc01.north.sevenkingdoms.local -clsid 90f18417-f0f1-484e-9d3c-59dceee5dbd8 -addgroupmember 'Domain Admins' HOSTNAME$"
opsec_notes: |
  KrbRelay is a powerful local privilege escalation primitive that does not require
  any external network listener — it runs entirely from the target host. The LDAP write
  to the DC is the noisiest operation (Event 5136). The DCOM activation used to trigger
  local coercion generates COM-related events. MDI detection for RBCD writes is the
  primary detection path. KrbRelay is no longer actively maintained; SharpKrbRelay is
  the recommended successor.
gotchas: |
  The specific CLSID used must be one that runs as SYSTEM and supports DCOM activation.
  Cube0x0's repo includes a CLSID discovery tool. Some CLSIDs are version-specific —
  test against the exact target OS version. Requires domain membership (for Kerberos
  ticket issuance). KrbRelay's RBCD path requires creating/having a machine account
  with an SPN — typically combined with StandIn or Powermad to create the account first,
  then KrbRelay to add it to the RBCD attribute of the local machine. RBCD self-write
  requires SeImpersonatePrivilege is NOT the only requirement — the machine object's
  RBCD must be writable by the account (default: domain users can't write RBCD on computer
  objects they don't control). Check whether the machine's RBCD attribute is writable first.
related_ttps: [sharpkrbrelay, standin, rubeus, whisker, coercer]
alternatives: [sharpkrbrelay, krbrelayx, ntlmrelayx]
common_args:
  -spn:
    name: -spn
    description: Target SPN to relay the Kerberos ticket to (e.g. ldap/dc01.domain)
    typical_values: ["ldap/dc01.north.sevenkingdoms.local", "cifs/dc01.domain"]
    required: true
  -clsid:
    name: -clsid
    description: CLSID of a DCOM object that runs as SYSTEM (to trigger coercion)
    typical_values: ["90f18417-f0f1-484e-9d3c-59dceee5dbd8"]
    required: true
  -rbcd:
    name: -rbcd
    description: Perform RBCD attack — SID of machine account to write to RBCD attribute
    typical_values: ["S-1-5-21-...-COMPUTERSID"]
  -shadowcred:
    name: -shadowcred
    description: Add a shadow credential (key credential) to the machine account via relay
    typical_values: [flag-only]
  -addgroupmember:
    name: -addgroupmember
    description: Add a principal to an AD group via the relayed auth
    typical_values: ["'Domain Admins' HOSTNAME$"]
last_updated: 2026-05-29
---

# KrbRelay

A Kerberos relay tool by cube0x0 that abuses Windows' loopback Kerberos service
ticket handling to relay machine account authentication to LDAP on the domain controller.
Unlike NTLM relay, KrbRelay works with Kerberos authentication — it intercepts a
SYSTEM-context Kerberos service ticket (triggered via DCOM coercion) and forwards it
to the DC's LDAP service to perform RBCD writes, shadow credential injection, or group
membership changes. This is a local privilege escalation primitive: an unprivileged
user can gain SYSTEM via the RBCD path without any external infrastructure.

## Typical use cases
- Local privilege escalation: user → SYSTEM on a domain-joined machine via RBCD relay
- Add shadow credentials to the machine account for persistent access
- RBCD setup without external NTLM relay infrastructure
- Grant group membership via relayed LDAP auth

## How Sage uses this
KrbRelay is the Windows-native local privesc path in the Trust Walker chain when the
attacker has a domain user shell on a machine but needs SYSTEM. The chain:
1. KrbRelay triggers a DCOM-based loopback coercion, capturing the machine account's
   Kerberos service ticket for LDAP/DC
2. KrbRelay relays that ticket to the DC's LDAP to write RBCD (a second attacker-controlled
   machine account gets delegation rights on the victim machine)
3. Rubeus S4U chain impersonates Administrator to CIFS/victim, achieving SYSTEM
The advantage: all of this runs locally on the victim machine without external infrastructure.

## Output
Console output showing DCOM activation status, relay attempt, and LDAP write result.
Success message: `[+] RBCD set` or `[+] Shadow credentials added`.

## Full Reference

> Captured against KrbRelay v1.0, 2026-05-29. Source: https://github.com/cube0x0/KrbRelay README.
> Note: project is not actively maintained; SharpKrbRelay is the maintained successor.

### Primary flags

| Flag | Description |
|------|-------------|
| `-spn X` | Target SPN (ldap/dc, cifs/dc, etc.) |
| `-clsid X` | CLSID of SYSTEM-running DCOM object to trigger coercion |
| `-rbcd X` | SID of principal to write into RBCD attribute |
| `-shadowcred` | Add shadow credential to machine account |
| `-addgroupmember X` | Add principal to AD group |
| `-addowner X` | Set owner on AD object |
| `-kcd X` | Perform KCD (constrained delegation write) |
| `-console` | Keep console open after relay |
| `-port X` | Local port for relay listener |

### CLSID discovery

KrbRelay requires a CLSID that:
- Launches via COM activation
- Runs as SYSTEM (LocalSystem or NetworkService context)
- Can be activated by a non-elevated user

The repo includes `CheckPort.exe` for local port discovery and a CLSID lookup tool.
Common working CLSIDs vary by Windows version — test in lab first.

### Source for this reference

- https://github.com/cube0x0/KrbRelay (README)
- cube0x0 blog: https://cube0x0.github.io/Relaying-for-gMSA/
- Version: v1.0 as of 2026-05-29 (unmaintained; use SharpKrbRelay for newer Windows)
