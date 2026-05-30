---
name: Koh Token Capture — Operational Deep Dive
category: credential-access
subcategories: [koh-deep-dive, sspi-hooks, logon-session-capture, long-term-token-harvest]
tradecraft_tags: [koh, token, sspi, logon-session, long-term, harvest, ghostpack, bof]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
source:
  url: https://github.com/GhostPack/Koh
  license: BSD-3-Clause
  maintained: true
binary_type: multi
binary_filename: Koh.exe / Koh.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: system
network_required: false
detection_signal: |
  See koh.md for detection notes. This file provides operational deep-dive context.
usage_examples:
  - description: See koh.md
    args: "(see koh.md)"
opsec_notes: |
  Reference to koh.md for primary OPSEC. This file provides additional operational context.
gotchas: |
  See koh.md for gotchas.
related_ttps: [koh, outflank-remote-ops-bofs, sharp-token-handler-bof, rubeus]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Koh Token Capture — Operational Deep Dive

> See `koh.md` for the schema entry. This file provides extended operational context
> for Koh's use in long-duration engagements.

## The SSPI Hook Mechanism

Koh registers as an SSPI security package (LSA notification package) or hooks in-process:

```
Normal Windows logon flow:
  1. User types credentials
  2. LSA (Local Security Authority) validates
  3. Logon session created with token
  4. Token handed to application
  5. Session released on logoff

With Koh:
  1. User types credentials
  2. LSA validates + notifies registered SSPI packages (including Koh)
  3. Koh intercepts the token at notification time
  4. Koh holds the token in its own process memory
  5. Even after user logoff, the token remains available to Koh
```

## Operational Timing

Koh is most valuable in scenarios where:
1. Domain Admins log in on a predictable schedule (morning login)
2. There's a period between "deploy Koh" and "collect tokens"
3. Multiple high-value users authenticate to the same host over time

```
Monday 7:00 AM: Deploy Koh (requires SYSTEM achieved previously)
Monday 8:30 AM: DA users start their workday → Koh silently captures tokens
Monday 9:00 AM: Koh.exe list → shows captured DA tokens
Monday 9:01 AM: Koh.exe impersonate <LUID> → DCSync → krbtgt hash
```

## Koh vs Rubeus Monitor

| Method | What it captures | When |
|--------|-----------------|------|
| Rubeus monitor | TGTs forwarded to the machine | Only from Kerberos delegation |
| Koh | Any logon session token (NTLM, Kerberos, smart card) | Any authentication |

Rubeus monitor requires unconstrained delegation. Koh works on any machine
where SYSTEM access is achieved and users authenticate.

## Group Membership via Koh

Koh can display the group memberships of captured sessions:
```
Koh.exe groups <LUID>
→ Shows exact group memberships in the captured token
→ Helps prioritize which session to impersonate
```

## Cleanup

Koh server (LSA package mode) persists until:
1. Manually removed: `Koh.exe uninstall`
2. LSA restart (reboot)

BOF mode (Koh.x64.o) persists until the agent process dies.
