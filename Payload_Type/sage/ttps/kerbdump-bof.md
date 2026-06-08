---
name: KerbDump BOF
category: credential-access
subcategories: [kerberos-tickets, lsass-alternative, in-process]
tradecraft_tags: [bof, kerberos, ticket-dump, lsass-alternative, no-lsass-open, athena]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://github.com/0x00Check/KerbDump
  license: MIT
  maintained: true
binary_type: bof
binary_filename: KerbDump.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  KerbDump extracts Kerberos tickets from the current logon session using the Kerberos
  provider's exported functions — it does NOT open LSASS. The Kerberos API calls
  (LsaCallAuthenticationPackage) are used by legitimate applications and generate
  minimal telemetry. This is KerbDump's primary advantage over Mimikatz
  kerberos::list or Rubeus dump.
usage_examples:
  - description: Dump all Kerberos tickets from the current logon session
    args: "execute-bof KerbDump.x64.o"
  - description: Dump from a specific LUID (alternate logon session)
    args: "execute-bof KerbDump.x64.o <LUID>"
  - description: Equivalent Rubeus command (higher noise — opens LSASS)
    args: "Rubeus.exe dump /nowrap"
opsec_notes: |
  KerbDump is the in-process, no-LSASS-open alternative to Rubeus dump and
  Mimikatz kerberos::list /export. It uses the Kerberos SSP's own API to
  retrieve tickets, avoiding the LSASS process access that triggers EDR
  ObjectAccess alerts. The tickets dumped are base64 encoded and ready for
  Rubeus ptt or Pass-the-Ticket chains. Unlike Mimikatz `/export`, KerbDump's
  normal output is stdout/base64 for the C2 to capture; Mimikatz writes `.kirbi`
  files into the current working directory.
gotchas: |
  Apollo has no BOF runner — requires Athena's execute-bof. KerbDump retrieves
  tickets from the calling session's LUID (or a specified LUID) — it doesn't dump
  tickets from other processes unless you can call it from that session context.
  For tickets in other sessions (e.g. a DA's session), use Rubeus dump /luid:X
  (which does open LSASS) or steal_token first to get into the target session.
  Output tickets are base64 .kirbi files, directly usable with Rubeus ptt.
related_ttps: [rubeus, nanodump, pass-the-ticket, trustedsec-bofs]
alternatives: [rubeus-dump, mimikatz-kerberos-list]
common_args:
  LUID:
    description: Optional target logon session LUID (hex or decimal)
    typical_values: ["0x3e4", "996"]
last_updated: 2026-06-08
---

# KerbDump BOF

A BOF that dumps Kerberos tickets from logon sessions using the Kerberos authentication
package's exported API (`LsaCallAuthenticationPackage` with the `KerbRetrieveEncodedTicketMessage`
message type) — no LSASS handle opening required. This makes it fundamentally stealthier
than Rubeus `dump` or Mimikatz `kerberos::list /export`, both of which open LSASS.
Mimikatz `/export` also creates `.kirbi` files in the current working directory, so
using it from a user's Desktop leaves obvious artifacts.

## The Stealth Distinction

| Tool | Method | LSASS access? | Detection signal |
|------|--------|--------------|-----------------|
| Rubeus dump | OpenProcess(LSASS) + read | YES | High — Sysmon Event 10, EDR ObjectAccess |
| Mimikatz kerberos::list | OpenProcess(LSASS) | YES | High |
| KerbDump BOF | LsaCallAuthenticationPackage | NO | Low — legitimate Kerberos API |

## Typical use cases
- Dump Kerberos tickets from the current session without touching LSASS
- Harvest TGTs or TGSs for pass-the-ticket without triggering LSASS alerts
- Stealth alternative to Rubeus dump in EDR-monitored environments

## How Sage uses this
With Athena, KerbDump is the preferred ticket harvesting method in environments where
LSASS access is monitored. After gaining access to a high-privilege logon session
(via token steal, or if already running in that session), KerbDump extracts tickets
for pass-the-ticket without the LSASS footprint.

## Apollo-specific note
BOF — requires Athena. For Apollo: use Rubeus dump (accepts elevated noise) or
Apollo's `ticket_cache_list` command.
