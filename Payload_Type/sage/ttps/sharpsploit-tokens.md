---
name: SharpSploit Token Manipulation
category: privilege-escalation
subcategories: [token-manipulation, impersonation, logon-session]
tradecraft_tags: [token, impersonation, make-token, steal-token, logon, privilege-escalation, ghostpack]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
  - id: T1134.003
    name: Access Token Manipulation — Make and Impersonate Token
source:
  url: https://github.com/cobbr/SharpSploit
  license: BSD-3-Clause
  maintained: false
binary_type: .net-assembly
binary_filename: SharpSploit.dll
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Token impersonation events (Event 4674, 4673) when elevated operations are performed.
  Sysmon tracks process access events when tokens are stolen from other processes.
  Apollo's native make_token and steal_token commands have equivalent functionality
  with potentially better evasion characteristics.
usage_examples:
  - description: Create a new logon session with domain credentials (equivalent to make_token)
    args: "(via SharpSploit.Credentials.Tokens.MakeToken) MakeToken('username', 'domain', 'password')"
  - description: Steal a token from another process
    args: "(via SharpSploit.Credentials.Tokens.ImpersonateUser) ImpersonateUser(pid)"
opsec_notes: |
  Apollo ships native `make_token`, `steal_token`, and `rev2self` commands that cover
  all of SharpSploit's token manipulation. Use Apollo's native commands — they are
  better integrated and don't require loading an additional assembly.
gotchas: |
  SharpSploit is not actively maintained. Apollo's native token manipulation commands
  are the preferred path. SharpSploit is documented here primarily as a reference for
  its token manipulation concepts; operators should use Apollo native commands instead.
  MakeToken creates a network-only logon session (type 9) — operations inside the
  agent won't be affected, but outbound network access uses the new credential.
related_ttps: [mimikatz, sharpup, seatbelt]
alternatives: [apollo-make-token, apollo-steal-token, mimikatz-pth]
common_args: {}
last_updated: 2026-05-29
---

# SharpSploit Token Manipulation

SharpSploit's .NET token manipulation primitives (MakeToken, ImpersonateUser). These
implement the same techniques as Apollo's native `make_token` and `steal_token` commands.
Documented for reference; in Apollo engagements, always use Apollo's native commands
rather than loading SharpSploit.

## Typical use cases
- Create a network logon session with alternate credentials (make_token pattern)
- Steal a token from another process for lateral movement (steal_token pattern)

## How Sage uses this
In practice, Sage uses Apollo's native `make_token`, `steal_token`, and `rev2self`
commands — these are equivalent to SharpSploit's token manipulation without the
overhead of loading an extra assembly.

| Operation | Apollo Native | SharpSploit |
|-----------|--------------|-------------|
| Create logon session | `make_token` | SharpSploit.MakeToken |
| Steal process token | `steal_token` | SharpSploit.ImpersonateUser |
| Revert to primary token | `rev2self` | SharpSploit.RevertToSelf |
