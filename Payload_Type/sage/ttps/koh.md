---
name: Koh
category: credential-access
subcategories: [token-capture, logon-session-harvesting, sspi-abuse, bof-and-dotnet]
tradecraft_tags: [token, logon-session, sspi, koh, kerberos-harvest, ghostpack, bof, dotnet, athena, apollo]
mitre_attack:
  - id: T1134.001
    name: Access Token Manipulation — Token Impersonation/Theft
  - id: T1558
    name: Steal or Forge Kerberos Tickets
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
  Koh registers an SSPI security package (LSA notification package) — this requires
  writing a DLL path to the LSA packages registry key (HKLM\SYSTEM\CurrentControlSet\
  Control\Lsa\Security Packages), which is an LSA protection modification event
  detectable by Sysmon Event 12/13 and behavioral EDR. The registered package intercepts
  all logon sessions — a significant hook in LSASS's authentication pipeline.
usage_examples:
  - description: Deploy Koh server (SYSTEM required, needs reboot or LSA restart to take effect)
    args: "Koh.exe capture"
  - description: Deploy via BOF (Athena, no LSA restart needed — in-process hooking)
    args: "execute-bof Koh.x64.o"
  - description: List captured logon sessions from a Koh deployment
    args: "Koh.exe list"
  - description: Impersonate a specific captured session
    args: "Koh.exe impersonate <luid>"
  - description: Dump all captured tokens for the specified user
    args: "Koh.exe filter <username>"
opsec_notes: |
  Koh uses the SSPI security package registration mechanism to receive notifications
  for every new logon — capturing the token before Windows can release it. This is
  a persistence-flavored credential capture: Koh keeps running and captures tokens
  as users log in over time. The server component (Koh.exe) is persistent until removed.
  The BOF variant (Koh.x64.o) runs in-process without the registry modification.
  
  SYSTEM access is required because LSA package registration is a privileged operation.
  The LSA package approach requires an LSA restart (typically reboot on modern Windows)
  while the BOF approach hooks in-process immediately.
gotchas: |
  Requires SYSTEM (not just local admin). The LSA package registration approach
  (Koh.exe capture) requires an LSA restart to activate — on modern Windows with
  protected LSA or PPL, this may not work or may be blocked. The BOF approach
  (Koh.x64.o via Athena) is in-process and more reliable. Koh captures tokens as users
  NEW log in — it doesn't retroactively capture existing sessions. Plan deployment
  timing: before anticipated DA logon events (morning logins, weekend recovery, etc.).
related_ttps: [outflank-remote-ops-bofs, sharp-token-handler-bof, sharpsploit-tokens,
               rubeus, safetykatz]
alternatives: [steal-token-apollo, rubeus-monitor, outflank-bof-tokenstalker]
common_args:
  capture:
    description: Start Koh server to capture incoming logon sessions
    typical_values: [flag-only]
  list:
    description: List all currently captured logon sessions and LUIDs
    typical_values: [flag-only]
  impersonate:
    description: Impersonate a specific captured session by LUID
    typical_values: ["<LUID>"]
  filter:
    description: Filter captured sessions by username
    typical_values: ["administrator", "jon.snow"]
  release:
    description: Release a captured session
    typical_values: ["<LUID>"]
  groups:
    description: List group membership for a captured session
    typical_values: ["<LUID>"]
last_updated: 2026-05-29
---

# Koh

GhostPack's "Token Stealer" — a unique credential capture tool that uses SSPI security
package hooks to intercept Windows logon sessions and hold their tokens indefinitely.
Rather than stealing tokens from running processes (steal_token), Koh captures them
at the SSPI layer as users authenticate, keeping them alive for later impersonation.

## Why Koh Is Different from steal_token

```
steal_token (Apollo):
  - Steal from an EXISTING process
  - Token belongs to a process that's already running
  - Process dies → token may become unavailable

Koh:
  - Intercepts logon sessions at the SSPI layer (BEFORE a process holds them)
  - The token is held by Koh itself, independent of any user process
  - User logs out → Koh still holds the token
  - Ideal for capturing DA tokens during morning login waves and using them later
```

## Operational Pattern

```
# Scenario: DA users log in every morning 8:00-9:00 AM

1. Deploy Koh (SYSTEM, before 8 AM):
   Apollo: run SafetyKatz 'privilege::debug' then execute Koh.exe capture
   OR: Athena: execute-bof Koh.x64.o

2. Wait for DA users to authenticate (no action needed)

3. Morning: list captured tokens:
   Koh.exe list
   → [LUID 0x1234] NORTH\administrator (Domain Admins, Enterprise Admins)

4. Impersonate:
   Koh.exe impersonate 0x1234
   → Now running with administrator's token in current process

5. DCSync or other DA operations using the captured token
```

## BOF vs Server Mode

| Mode | Binary | Activation | Requirements |
|------|--------|-----------|-------------|
| Server (LSA package) | Koh.exe capture | Requires LSA restart (reboot on PPL systems) | SYSTEM + LSA write |
| BOF (in-process hook) | Koh.x64.o | Immediate (no reboot) | SYSTEM + Athena |

## Apollo-specific note
Apollo has no BOF runner — use Koh.exe server mode (requires SYSTEM + LSA restart).
Athena's execute-bof is the preferred path for the in-process BOF variant (Koh.x64.o).
