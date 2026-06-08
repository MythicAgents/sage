---
name: Windows Execution Context - Logon Sessions, Access Tokens, and Kerberos Tickets
category: tradecraft-fundamentals
subcategories:
  - execution-context
  - windows-logon-session
  - access-token
  - impersonation
  - kerberos-context
  - sacrificial-logon-session
  - fork-and-run
  - in-process
  - ticket-injection
tradecraft_tags:
  - execution-context
  - access-token
  - primary-token
  - impersonation-token
  - make-token
  - make_token
  - steal-token
  - steal_token
  - rev2self
  - logon-session
  - luid
  - newcredentials
  - netonly
  - runas-netonly
  - createnetonly
  - create-netonly
  - sacrificial-process
  - sacraficial-logon-session
  - fork-and-run
  - in-process
  - kerberos-cache
  - ticket-cache
  - ticket-store
  - pass-the-ticket
  - overpass-the-hash
  - pass-the-hash
  - dcsync-context
  - laps-context
  - cross-realm
  - cross-forest
mitre_attack:
  - id: T1134
    name: Access Token Manipulation
  - id: T1134.001
    name: Access Token Manipulation - Token Impersonation/Theft
  - id: T1134.002
    name: Access Token Manipulation - Create Process with Token
  - id: T1134.003
    name: Access Token Manipulation - Make and Impersonate Token
  - id: T1550.002
    name: Use Alternate Authentication Material - Pass the Hash
  - id: T1550.003
    name: Use Alternate Authentication Material - Pass the Ticket
source:
  url: https://learn.microsoft.com/en-us/windows/win32/secauthz/access-tokens
  license: none
  maintained: true
source_references:
  - https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-logonuserw
  - https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw
  - https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-impersonateloggedonuser
  - https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-reverttoself
  - https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-duplicatetokenex
  - https://learn.microsoft.com/en-us/windows/win32/api/ntsecapi/nf-ntsecapi-lsacallauthenticationpackage
  - https://learn.microsoft.com/en-us/windows/win32/api/ntsecapi/ne-ntsecapi-kerb_protocol_message_type
  - https://learn.microsoft.com/en-us/windows/win32/api/ntsecapi/ns-ntsecapi-kerb_query_tkt_cache_request
  - https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/klist
  - https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4624
  - https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4768
  - https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4769
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Creating a NewCredentials/NetOnly context produces a local 4624 type 9 logon session on the
  host where the context is created. Kerberos ticket injection is local LSA state until the
  ticket is used; use of a TGT normally produces 4769 TGS requests on the DC, while requesting a
  fresh TGT produces 4768. Token theft does not create a new logon session but can be visible as
  process/token handle access. The strongest analytic signal is an identity mismatch: a process
  whose local token is one account but whose outbound network authentication is another account.
usage_examples:
  - description: In-process network operation with explicit credentials
    args: "LogonUser(NewCredentials) -> ImpersonateLoggedOnUser -> run SSPI/LDAP/SMB action on same thread -> RevertToSelf"
  - description: In-process ticket operation
    args: "select current LUID -> submit KRB_CRED/TGT/TGS to that LUID -> run network action from same context -> purge/revert"
  - description: Fork-and-run with a sacrificial logon session
    args: "create NetOnly sacrificial LUID -> inject credential or ticket into that LUID -> spawn/execute tool inside that context -> destroy context"
  - description: Convert harvested key material into reusable network identity
    args: "NT hash/AES key -> request TGT -> isolate in sacrificial LUID -> use for LDAP/SMB/RPC/WinRM -> repeat"
opsec_notes: |
  Prefer a dedicated sacrificial logon session per identity and per objective. Do not inject high-value
  TGTs into the operator's interactive session or the long-lived agent session unless the action must be
  in-process. Keep ticket lifetime, LUID, process PID, domain, username, source credential, and cleanup
  status as first-class state. Avoid IP-based targets when Kerberos is required; use SPNs/FQDNs so SSPI
  does not silently fall back to NTLM. Revert or purge immediately after the objective.
gotchas: |
  The common failure is credential material in one LUID and the tool running in another. A ticket in the
  agent's logon session is not automatically visible to a fork-and-run child with its own logon session,
  and a ticket staged for a sacrificial child is not visible to an in-process BOF or assembly. Thread
  impersonation is also thread-scoped: if the C2 dispatches the next task on a different worker thread,
  the task may run as the process primary token unless the task wrapper reapplies impersonation.
related_ttps:
  - pass-the-ticket
  - pass-the-hash
  - overpass-the-hash
  - rubeus
  - mimikatz
  - sharpsploit-tokens
  - sharptoken
  - sharp-token-handler-bof
  - koh-token-capture
  - laps-abuse
  - constrained-delegation-abuse
  - golden-ticket
  - diamond-ticket
alternatives: []
common_args: {}
last_updated: "2026-06-08"
---

# Windows Execution Context - Logon Sessions, Access Tokens, and Kerberos Tickets

This is the tradecraft model Sage should use whenever it has elevated access, extracts credential
material for another principal, and needs to place that material into memory so a later LDAP, SMB,
RPC, WinRM, DCSync, or ADCS action authenticates as that principal.

The core rule is simple: every Windows action has a **token context** and a **Kerberos cache context**.
If either one is attached to the wrong logon session, the operation runs as the wrong identity even
when the credential material is valid.

## Mental model

| Object | What it answers | Scope | Why Sage cares |
|--------|-----------------|-------|----------------|
| Access token | "Who am I locally?" | Process primary token or thread impersonation token | Controls local object access, privileges, integrity, group SIDs, and whether a process can be created as a user. |
| Logon session / LUID | "Which LSA session owns my auth state?" | Host-local LSA object | Kerberos tickets and NewCredentials state are scoped here. |
| Kerberos cache | "What tickets can this LUID present?" | Per logon session | PTT only works for operations that execute from the LUID that received the ticket. |
| Network credentials | "What identity should SSPI use remotely?" | NewCredentials/NetOnly logon session or Kerberos cache | Lets a process keep the same local identity while authenticating outbound as another identity. |

Windows access tokens contain the user SID, group SIDs, privileges, token type, impersonation
level, and a logon SID/logon session association. A process has a primary token. A thread can also
carry an impersonation token; while impersonating, access checks for that thread use the
impersonated identity until `RevertToSelf` or thread exit.

Kerberos tickets live in LSASS and are addressed by logon session LUID. Built-in `klist` exposes
that model directly with `tickets`, `tgt`, `purge`, and `sessions`, and with `-lh`/`-li` to select
a LUID. Programmatic ticket cache operations use LSA calls to the Kerberos authentication package
with messages such as query, retrieve, purge, and submit ticket.

## Two execution modes

| Mode | Examples | Token used | Kerberos cache used | Best use |
|------|----------|------------|---------------------|----------|
| In-process | Native agent command, BOF, in-process .NET, reflective DLL, custom LDAP/SMB code | Agent process primary token plus active thread impersonation token | The LUID of the current process/thread context | Short operations that return cleanly and need minimal process creation. |
| Fork-and-run | Spawned sacrificial process, execute-assembly, execute-PE, remote helper process | Child primary token chosen at process creation | Child LUID, or a ticket store injected into that child | Tools that call `ExitProcess`, long-running tools, noisy code isolation, or per-identity isolation. |

Do not treat these as interchangeable. In-process PTT into the agent LUID does not prepare a
fork-and-run child that has a different LUID. A ticket injected into a sacrificial child does not
help an in-process LDAP library running inside the agent.

## Access token primitives

| Primitive | Windows mechanism | Result | Tradecraft use |
|-----------|-------------------|--------|----------------|
| Make token with password | `LogonUserW` with an interactive, network, cleartext, or NewCredentials logon type | New token/logon session if the call succeeds | Use real credentials directly, or create a NewCredentials session for outbound-only auth. |
| Make NetOnly/NewCredentials token | `LogonUserW(LOGON32_LOGON_NEW_CREDENTIALS)` | Clones current local token and specifies different outbound credentials | Canonical sacrificial LUID for network actions and ticket isolation. |
| Create NetOnly process | `CreateProcessWithLogonW(LOGON_NETCREDENTIALS_ONLY)` | New process using caller's local token plus new LSA logon session for network creds | Tool-agnostic way to make a keeper/sacrificial process. |
| Operator NetOnly baseline | `runas /netonly` | Same operational concept: local identity stays the same, outbound credentials change | Useful for reproducing or validating the primitive outside a C2. |
| Steal token | `OpenProcessToken` + `DuplicateTokenEx` | Impersonation or primary token copied from another process | Use an existing logged-on user's security context without knowing a password. |
| Spawn as token | `DuplicateTokenEx(TokenPrimary)` + `CreateProcessAsUser` or `CreateProcessWithTokenW` | Child process primary token is the selected identity | Required when a fork-and-run child must truly run as the stolen/made identity. |
| Revert | `RevertToSelf` | Thread drops impersonation and returns to process primary token | Mandatory cleanup after cross-identity actions. |

NewCredentials/NetOnly is the important primitive. It creates a logon session where the local
identity remains the caller, but outbound network authentication can use another identity. With
`CreateProcessWithLogonW(LOGON_NETCREDENTIALS_ONLY)`, Windows explicitly creates a new LSA logon
session, uses the specified credentials as default network credentials, and does not validate the
credentials at creation time. This is why "junk creds + real Kerberos ticket" works as a clean
sacrificial container: the junk values exist only to force an isolated LUID; the real network
identity comes from the injected ticket.

## Kerberos primitives

| Primitive | Input | Output | Where it must land |
|-----------|-------|--------|--------------------|
| Pass-the-ticket | KRB_CRED / `.kirbi` / base64 TGT or TGS | Ticket inserted into a Kerberos cache | The LUID that will perform the network action. |
| Overpass-the-hash/key | NT hash, AES128, AES256, or cert-derived key | Fresh TGT from KDC | Usually inject into a sacrificial LUID, not the agent LUID. |
| Pass-the-hash | NT hash | NTLM network authentication or a pth-created logon session | Use only when Kerberos cannot be used or the service requires NTLM. |
| TGT use | Valid TGT | Service ticket request to KDC | Requires SPN/FQDN and a reachable DC. |
| TGS use | Service ticket | Access to one service SPN | Only works for that service identity unless the ticket is transformed by a separate Kerberos abuse path. |
| Purge | Target LUID | Ticket cache emptied | Cleanup and retry isolation. |

PTT is local state until used. A TGT in a cache does not touch the DC until a service ticket is
requested. A fresh AS-REQ for a TGT is visible as a Kerberos authentication ticket request; a TGS
request is visible on the DC when the cache asks for a service ticket.

Prefer AES keys over RC4/NT hash when requesting TGTs. RC4 still works in many lab and legacy
domains, but AES better matches modern domain behavior and avoids unnecessary RC4 downgrade signal.

**OPSEC — prefer ticket injection over credential injection.** Placing an already-obtained TGT into a clean
NetOnly LUID (pass-the-ticket) uses documented LSA submit calls and does **not** touch LSASS process memory.
Injecting a key/hash into a logon session by patching LSASS (sekurlsa::pth-style overpass-the-hash) is
materially noisier and EDR-flagged. So the quiet path is: obtain the TGT out-of-band (forge it, or send an
AS-REQ with the recovered key — both avoid LSASS), then PTT it into a sacrificial LUID — rather than patching
LSASS to seed the session. Reserve LSASS-touching credential injection for cases where no ticket path exists.

## Sacrificial logon sessions

A sacrificial logon session is an intentionally created LUID used only to hold alternate credentials
or Kerberos tickets. Its purpose is isolation:

- Do not pollute the agent's long-lived LUID with high-value tickets.
- Do not overwrite or purge the current user's real ticket cache.
- Keep one identity per LUID so Sage can reason about which user/domain will authenticate.
- Destroy the LUID/process after the objective.

### Pattern A - In-process sacrificial context

Use this when the actual network operation is implemented by the agent, a BOF, or code that runs in
the agent process and returns cleanly.

```
1. Capture baseline:
   - current token user
   - current LUID
   - current Kerberos cache

2. Create or select context:
   - for plaintext creds: LogonUser(NewCredentials, real user/pass)
   - for ticket-only creds: LogonUser(NewCredentials, fake user/pass) to create an empty LUID
   - for existing user session: duplicate/impersonate the user's token

3. Apply the context on the thread that will perform the network action:
   - ImpersonateLoggedOnUser(token)
   - verify token and LUID from the same worker thread

4. Populate auth material:
   - password path: let SSPI use NewCredentials on outbound auth
   - ticket path: submit TGT/TGS to this LUID's Kerberos cache
   - key/hash path: request TGT, then submit it to this LUID

5. Execute exactly one objective:
   - LDAP read/write
   - SMB file/service action
   - RPC/DCSync
   - WinRM/HTTP service access

6. Cleanup:
   - purge tickets from the LUID if possible
   - RevertToSelf
   - close token handles
```

Important implementation detail: impersonation is thread-scoped. A C2 that queues work across
multiple worker threads must reapply the impersonation token around each outbound operation. Do not
set impersonation in task 1 and assume task 2 runs on the same thread.

### Pattern B - Fork-and-run sacrificial process

Use this when the tool may exit the process, is long-running, needs a different bitness/runtime, or
should be isolated from the agent.

```
1. Create a hidden/suspended NetOnly keeper process:
   - CreateProcessWithLogonW(user, domain, password, LOGON_NETCREDENTIALS_ONLY, ...)
   - for ticket-only usage, use fake credentials to force a fresh LUID

2. Identify the keeper's LUID:
   - open the process token
   - read TokenStatistics.AuthenticationId
   - record PID, LUID, username label, domain, creation time

3. Populate the keeper LUID:
   - submit a TGT/TGS to that LUID, or
   - inject from inside the keeper context, or
   - use the C2's per-child ticket injection mechanism

4. Execute child work in that context:
   - spawn the target tool as the keeper identity, or
   - inject/run inside the keeper, or
   - configure fork-and-run to copy the impersonated token and stage tickets

5. Validate from inside the child:
   - list tickets for that LUID
   - perform a low-noise access check against the intended SPN

6. Cleanup:
   - purge keeper LUID tickets
   - terminate keeper process
   - remove any temporary pipes/files/handles
```

Plain `CreateProcess` normally uses the process primary token. If the current thread is merely
impersonating, robust fork-and-run code should duplicate the impersonation token as a primary token
and call `CreateProcessAsUser` or `CreateProcessWithTokenW`, or use `CreateProcessWithLogonW` for
NetOnly. Do not assume a thread impersonation token automatically becomes the child primary token.

Two Windows edge cases matter in agents:

- `CreateProcessWithLogonW` is not reliable from LocalSystem because the LocalSystem token lacks the
  logon SID that API expects. From SYSTEM, prefer `LogonUser` -> `DuplicateTokenEx(TokenPrimary)` ->
  `CreateProcessAsUser`, or perform the NetOnly creation from a normal user logon context.
- Submitting or querying tickets for an arbitrary non-current LUID can require elevated/trusted LSA
  access. The lower-friction pattern is to inject from inside the sacrificial context, impersonate
  that context before submitting, or use the C2's per-child injection path.

### Pattern C - Per-child ticket store

Some C2s maintain tickets in agent memory and inject them into each sacrificial child at launch.
The tool-agnostic logic is:

```
1. Create a NewCredentials token/session.
2. Mark that token as the active execution context.
3. Stage one or more tickets in the agent's protected ticket store.
4. On fork-and-run, create child under that execution context.
5. Before tool entrypoint, submit staged tickets into the child/session LUID.
6. Run tool.
7. Tear down or leave the store entry only if Sage expects another child for the same identity.
```

This is safer than injecting tickets into the agent LUID, but Sage must model it explicitly:
`ticket_store` is not the same thing as `ticket_cache`. One is future child state; the other is the
current LSA cache.

## Credential material to context mapping

| Sage has | Best conversion | Preferred context | Notes |
|----------|-----------------|-------------------|-------|
| Cleartext domain password | NewCredentials token, or request TGT with password | Dedicated sacrificial LUID | Password path can use NTLM or Kerberos via SSPI; TGT path is cleaner for Kerberos-only chains. |
| NT hash | Request RC4 TGT if Kerberos RC4 allowed; otherwise NTLM PTH | Sacrificial LUID | Prefer converting to Kerberos TGT over raw NTLM lateral movement when possible. |
| AES128/AES256 key | Request AES TGT | Sacrificial LUID | Best reusable material after LSASS/DCSync. |
| Certificate/PFX | PKINIT TGT; optionally UnPAC NT hash if needed | Sacrificial LUID | Good for ADCS chains and shadow credentials. |
| Existing TGT | PTT into selected LUID | In-process or fork-and-run LUID, matching execution mode | Do not inject into the agent cache unless the action is in-process. |
| Existing TGS | PTT into selected LUID | LUID that will access the matching service | Only useful for the SPN/service in the ticket. |
| Process token | Impersonate for in-process; duplicate to primary for child | Depends on target operation | Check token logon type and impersonation level; network-only tokens often do not delegate. |
| Machine account context | Use as computer account over network | Usually in-process SYSTEM or service context | Useful for RBCD, ADCS machine template abuse, and local admin paths; not equivalent to user DA. |

## In-process management details

Use in-process when Sage owns the network operation or can call a library safely inside the agent.
Good examples are LDAP queries, SMB checks, native DCSync code, ticket cache inspection, and BOFs
that return to the caller.

In-process checklist:

1. Select the identity and desired LUID.
2. If using another access token, impersonate on the same thread that will do network I/O.
3. If using Kerberos, inject or request tickets into the current/selected LUID.
4. Force Kerberos when required by using FQDN/SPN targets, not raw IP addresses.
5. Run one network action.
6. Record result, ticket expiry, and any new credential material.
7. Purge/revert before the next unrelated task.

Failure modes:

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `whoami` shows the original agent user | NewCredentials preserves local identity | Validate with remote access or ticket cache, not only local `whoami`. |
| LDAP confidential attribute is empty | Query ran from wrong LUID or wrong PAC | Recreate sacrificial context and inject the real user's TGT. |
| DCSync stalls or returns access denied | RPC authenticated as foothold user, not DA-equivalent ticket | Put DA-equivalent TGT in the LUID used by the DCSync code. |
| Kerberos falls back to NTLM | Target was IP/alias, SPN missing, DNS/DC issue | Use FQDN and correct service SPN; verify TGS request. |
| Next task runs as wrong user | Missing `RevertToSelf` | Revert and purge after every cross-identity action. |
| Ticket exists but tool cannot use it | Tool is fork-and-run in another LUID | Inject into child LUID or switch to in-process execution. |

## Fork-and-run management details

Use fork-and-run when the payload is not safe to load into the agent, calls `ExitProcess`, has heavy
runtime requirements, or needs a short-lived credential container.

Fork-and-run checklist:

1. Decide whether the child needs a real primary token or only alternate network credentials.
2. For real token: duplicate/convert to a primary token and create process with that token.
3. For outbound-only identity: create a NetOnly process/session.
4. For Kerberos-only identity: use junk NetOnly creds to create a clean LUID, then inject the ticket.
5. Record PID and LUID before running the actual tool.
6. Run a minimal verification action from inside the child.
7. Execute the objective.
8. Purge tickets and kill the sacrificial process.

Keep a keeper process only while it has a useful ticket. A persistent keeper is helpful when several
children must reuse the same identity, but it also lengthens the detection window and increases the
chance Sage loses track of which LUID owns which credential.

## Kerberos and LUID rules

- A LUID is host-local. The same numeric LUID on another host is unrelated.
- A ticket cache belongs to a LUID, not to a tool.
- A TGT lets the cache request TGS tickets for services; a TGS is already service-specific.
- Ticket injection is local; ticket use is remote and creates DC/service evidence.
- A forged or injected ticket must have valid time bounds and match domain crypto policy.
- Cross-forest operations depend on referrals and PAC/group interpretation across the trust.
- Real user TGTs are more reliable than forged tickets when the objective depends on foreign group
  membership, SID filtering behavior, or claims that must survive trust evaluation.
- Clock skew greater than the domain tolerance causes Kerberos failures that look like bad creds.

## GOAD-oriented operational loop

Sage's loop for GOAD-style ranges should be context-first:

```
1. Escalate locally.
2. Harvest credential material:
   - LSASS logon sessions
   - local secrets
   - DPAPI/browser material
   - tickets
   - DCSync output when replication rights exist

3. Normalize each credential into an identity object:
   - account name/domain/SID
   - material type: password, NT hash, AES key, cert, TGT, TGS, token
   - source host and source LUID
   - expiry and renew time for tickets
   - likely reachable services/domains

4. Build a dedicated execution context:
   - in-process context for native/BOF network operations
   - fork-and-run sacrificial LUID for external tools
   - one identity per LUID

5. Validate with a low-noise action:
   - request/list a TGS for the intended SPN
   - read a harmless LDAP attribute
   - list a known SMB share
   - query current DC or domain policy

6. Perform the objective:
   - read LAPS/gMSA/ADCS data
   - modify ACL/RBCD/group membership
   - DCSync target account
   - move laterally
   - harvest next host

7. Capture new material and repeat.
8. Cleanup context before switching identities.
```

The loop should never "just run the tool again" after an access denied. First answer:

```
Which token did the operation use?
Which LUID did the operation use?
Which tickets were present in that LUID at operation time?
Did the target service use Kerberos or NTLM?
Which account appears in DC/service logs?
```

## Tradecraft captures

### Captured password to remote access

```
1. Create NewCredentials context with real DOMAIN\user password.
2. Use FQDN/SPN target so SSPI chooses Kerberos where possible.
3. Run the network operation from the same thread/process context.
4. If repeated actions are needed, request a TGT and store only the ticket in a sacrificial LUID.
5. Revert and close token handles.
```

### Captured NT hash to Kerberos

```
1. Use the NT hash as RC4 key only if the domain accepts RC4.
2. Request a TGT for DOMAIN\user.
3. Create a fresh NetOnly sacrificial LUID with junk credentials.
4. Inject the TGT into that LUID.
5. Run LDAP/SMB/RPC from that LUID.
6. Purge/kill when done.
```

### Captured AES key to Kerberos

```
1. Request an AES TGT for DOMAIN\user.
2. Create sacrificial LUID.
3. Inject TGT into sacrificial LUID.
4. Validate by requesting a TGS for the exact service SPN.
5. Execute objective.
```

### Captured TGT to fork-and-run

```
1. Do not inject into the agent LUID unless the tool runs in-process.
2. Create a NetOnly sacrificial process with fake credentials.
3. Resolve and record the process LUID.
4. Inject the TGT into that LUID.
5. Launch or inject the target tool in that process context.
6. Destroy the process after use.
```

### Captured user token to network action

```
1. Inspect target token owner, logon type, integrity, groups, and impersonation level.
2. For in-process work, impersonate the token on the worker thread.
3. For fork-and-run, duplicate it as a primary token and create the child with that token.
4. If the token lacks usable network credentials, pair it with a Kerberos ticket in a sacrificial LUID.
5. Revert/close handles.
```

### Cross-forest LAPS or confidential LDAP read

```
1. Use the real user's TGT when foreign group membership/PAC evaluation matters.
2. Put that TGT in the LUID that will run the LDAP read.
3. Target the DC/FQDN in the resource forest.
4. Let Kerberos referral flow happen from the same LUID.
5. Treat an empty confidential attribute as a context failure before trying alternate queries.
```

### DCSync from a non-DC foothold

```
1. Confirm the identity has replication rights or equivalent DA/EA rights.
2. Put that identity's TGT or network creds in a dedicated LUID.
3. Run the replication RPC from that LUID.
4. If the tool shows RPC bind/access failure, inspect the authenticating account in logs/context.
5. Store recovered AES/NTLM as new identity material and repeat from step 3 of the GOAD loop.
```

## State Sage should track

| Field | Purpose |
|-------|---------|
| `context_id` | Stable handle for one usable execution context. |
| `mode` | `in_process`, `fork_run_keeper`, `fork_run_per_child`, or `stolen_token`. |
| `pid` | Keeper/child process ID if applicable. |
| `luid` | Logon session that owns Kerberos/network auth state. |
| `local_token_user` | What local access checks see. |
| `network_user` | What outbound authentication should present. |
| `domain_fqdn` | Kerberos realm/domain for TGT/TGS handling. |
| `material_type` | Password, NT hash, AES key, cert, TGT, TGS, token. |
| `ticket_expiry` | Avoid using stale tickets and explain sudden failures. |
| `target_spns` | Services this context has validated. |
| `created_from` | Host, process, dump, DCSync, or ticket source. |
| `cleanup_required` | Purge tickets, revert thread, close handles, kill process. |

This state prevents the most expensive class of mistakes: Sage has valid credentials but cannot
explain where they were placed or which process actually used them.

## Cleanup

Minimum cleanup after every cross-identity objective:

```
1. Purge tickets from the sacrificial LUID when possible.
2. Revert thread impersonation.
3. Close duplicated token handles.
4. Terminate sacrificial keeper process if no longer needed.
5. Clear staged ticket-store entries.
6. Mark context_id destroyed so no later task reuses stale auth state.
```

If cleanup fails, do not silently continue. A failed `RevertToSelf` means subsequent operations may
continue under the wrong identity; a failed purge means high-value tickets may remain usable in LSASS
until expiry or process/logon-session teardown.
