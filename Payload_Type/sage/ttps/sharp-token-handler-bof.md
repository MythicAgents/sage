---
name: Token Handling BOFs (CS-Remote-OPs Variants)
category: privilege-escalation
subcategories: [token-manipulation, impersonation, bof, in-process-token]
tradecraft_tags: [token, impersonation, bof, make-token, steal-token, logon-session, athena]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
  - id: T1134.003
    name: Access Token Manipulation — Make and Impersonate Token
source:
  url: https://github.com/rookuu/BOFs
  license: Unknown
  maintained: true
binary_type: bof
binary_filename: (various token BOFs)
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Token manipulation events generate Windows Security Event Log 4624 (new logon) for
  LogonUser-based tokens, and handle access patterns visible via ObRegisterCallbacks
  for OpenProcess+DuplicateHandle paths. In-process via BOF produces fewer process
  creation events than standalone tools.
usage_examples:
  - description: Make a network logon token for a known user/password (BOF variant)
    args: "execute-bof make_token.x64.o DOMAIN USERNAME PASSWORD"
  - description: Steal the primary token from a running process
    args: "execute-bof steal_token.x64.o <PID>"
  - description: Revert to original token
    args: "execute-bof rev2self.x64.o"
  - description: List logon sessions and their tokens
    args: "execute-bof list_tokens.x64.o"
opsec_notes: |
  BOF-based token manipulation performs the same operations as Apollo's native
  make_token / steal_token / rev2self commands but as BOFs. The advantage: with
  Athena, all token operations happen in-process without process creation.
  Apollo's native token commands already do this well — BOF token tools are
  primarily useful for C2 frameworks that lack native token commands (Cobalt Strike
  before built-in token commands, Havoc, etc.).
gotchas: |
  Apollo has native make_token, steal_token, and rev2self commands — the BOF
  equivalents are not needed for Apollo operators. For Athena operators, these BOFs
  provide equivalent functionality to Apollo's native commands. For Cobalt Strike
  operators, these BOFs fill the gap if CS's built-in token commands don't cover
  a specific edge case.
related_ttps: [outflank-remote-ops-bofs, trustedsec-bofs, runascs, mimikatz]
alternatives: [apollo-native-make-token, apollo-native-steal-token]
common_args:
  make_token:
    description: Create a new logon session with specified credentials
    typical_values: ["NORTH USERNAME PASSWORD"]
  steal_token:
    description: Steal primary token from target PID
    typical_values: ["<PID>"]
  rev2self:
    description: Revert to original agent token
    typical_values: [flag-only]
  list_tokens:
    description: List accessible logon sessions and tokens
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# Token Handling BOFs

A collection of BOFs implementing Windows token manipulation primitives: make_token
(LogonUser-based network credential session), steal_token (duplicate process token),
rev2self (revert), and list_tokens (enumerate sessions). These provide the same
functionality as Apollo's native token commands in BOF form for non-Apollo C2 frameworks.

## Token Operations Comparison

| Operation | Apollo native | BOF variant | Notes |
|-----------|--------------|-------------|-------|
| Create network credential session | `make_token` | `make_token.x64.o` | LogonUser type 9 (network credentials) |
| Steal process token | `steal_token` | `steal_token.x64.o` | DuplicateHandle on target PID |
| Revert token | `rev2self` | `rev2self.x64.o` | RevertToSelf() |
| List sessions | `ticket_cache_list` (Kerberos) | `list_tokens.x64.o` | Enumerate all accessible LUIDs |

## Token Type Reference

| LogonType | What it creates | Network access |
|-----------|----------------|----------------|
| Type 2 (Interactive) | Full token, loads profile | No (local only) |
| Type 3 (Network) | Network credentials only | Yes (remote resources) |
| Type 9 (NewCredentials) | Inherit local, new network creds | Yes (overrides for network) |

make_token and Apollo's `make_token` use Type 9 — the current process continues with
its existing local token, but any outbound network authentication uses the new credentials.

## How Sage uses this

For Apollo: use Apollo's native `make_token` / `steal_token` / `rev2self` — no BOF needed.
For Athena: these BOFs extend Athena's token capabilities to match Apollo's native set.
