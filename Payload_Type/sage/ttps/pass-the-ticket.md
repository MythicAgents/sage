---
name: Pass-the-Ticket
category: kerberos
subcategories: [ticket-injection, tgt-injection, tgs-injection, kerberos-reuse]
tradecraft_tags: [kerberos, pass-the-ticket, ptt, ticket-injection, tgt, tgs, technique]
mitre_attack:
  - id: T1550.003
    name: Use Alternate Authentication Material — Pass the Ticket
source:
  url: https://attack.mitre.org/techniques/T1550/003/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Injecting a ticket into a logon session generates Kerberos events on the DC when the
  ticket is subsequently used. Tickets used from unusual source IPs (different from where
  they were issued) may trigger DC anomaly detection. MDI has pass-the-ticket detection
  using ticket-origin correlation. Ticket injection itself is client-side (no DC event).
usage_examples:
  - description: Inject a base64 ticket via Rubeus
    args: "Rubeus.exe ptt /ticket:<base64ticket>"
  - description: Inject a .kirbi file via Rubeus
    args: "Rubeus.exe ptt /ticket:ticket.kirbi"
  - description: Inject via Mimikatz
    args: "kerberos::ptt ticket.kirbi"
  - description: Inject via Apollo native ticket_cache_add
    args: "Apollo: ticket_cache_add base64ticket=<base64>"
  - description: Verify ticket was injected (list tickets)
    args: "Rubeus.exe triage"
opsec_notes: |
  Pass-the-ticket is client-side — the ticket is injected into the current logon session
  (LUID) and will be used for subsequent Kerberos authentication. No DC event until the
  ticket is actually used. Apollo's `ticket_cache_add` command is the native path; Rubeus
  `ptt` is the assembly path. Key consideration: ticket scope is per-logon-session (LUID)
  — inject into the correct session.
gotchas: |
  Injected tickets are valid only until their expiration time — TGTs default to 10 hours.
  The ticket grants access to services within the scope of what it was issued for.
  To verify injection succeeded: `Rubeus.exe triage` or `klist`. Tickets are stored
  in memory — they survive for the session lifetime but are lost on process termination
  unless saved to disk (.kirbi file). Clock skew > 5 minutes = ticket rejection.
related_ttps: [rubeus, mimikatz, certify, whisker, constrained-delegation-abuse]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Pass-the-Ticket

The technique of injecting a Kerberos ticket (TGT or TGS) into a Windows logon session
to authenticate as the ticket's subject without knowing their password. The injected
ticket is stored in the Kerberos ticket cache for the specified logon session and used
transparently by all subsequent Kerberos authentications from that session.

## Implementation Paths in Sage

| Tool | Command | Notes |
|------|---------|-------|
| Apollo native | `ticket_cache_add base64ticket=<b64>` | Preferred for Apollo agents |
| Rubeus | `ptt /ticket:<base64 or .kirbi>` | Most common; flexible |
| Mimikatz | `kerberos::ptt ticket.kirbi` | Windows-side; classic |

## Ticket Types

| Type | Grants | Validity |
|------|--------|---------|
| TGT (krbtgt-encrypted) | Request any service ticket | 10 hours default |
| TGS (service-encrypted) | Access specific service | 10 hours default |
| Silver Ticket (forged TGS) | Access specific service without DC | Until expiry |
| Golden Ticket (forged TGT) | Request any service ticket | Configured on forge |

## Ticket Sources

Tickets are obtained via:
- Rubeus asktgt (NT hash / AES key / certificate)
- Rubeus monitor / dump (from running logon sessions)
- Mimikatz sekurlsa::tickets /export
- After delegation abuse (S4U2proxy results)
- After unconstrained delegation capture

## Verifying Injection

```
Rubeus.exe triage                  # List all accessible sessions and tickets
Rubeus.exe describe /ticket:<b64>  # Decode and describe a specific ticket
klist                              # Built-in Windows ticket list (current session)
```

## Session Isolation

Each Windows logon session (LUID) has its own Kerberos ticket cache. Injecting into
the current LUID affects only the current session. Apollo's `ticket_cache_add` injects
into the Apollo agent's session; use `/luid:X` in Rubeus to target a specific session.
