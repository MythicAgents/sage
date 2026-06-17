---
name: AD Attack-Path Planning Methodology
category: methodology
subcategories: [attack-path, planning, privilege-escalation, state-driven, decision-tree]
tradecraft_tags: [attack-path, planning, solve, objective, domain-admin, privilege-escalation, escalation, dcsync, gpo-abuse, golden-ticket, trust-traversal, cross-forest, execution-context, kerberos, bloodhound, methodology]
mitre_attack:
  - id: TA0004
    name: Privilege Escalation
  - id: TA0008
    name: Lateral Movement
source:
  url: ""
  license: none
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: varies
network_required: true
detection_signal: |
  A multi-hop escalation chain emits one detection signal per primitive: GPO writes (directory
  changes / scheduled-task creation on covered hosts), DCSync (DRSUAPI replication from a non-DC),
  ticket forging/injection (anomalous TGT lifetimes / encryption types), and LAPS reads (confidential
  LDAP attribute access). Plan the chain to minimise count and noise of these, not just to reach the goal.
opsec_notes: |
  Prefer in-memory post-exploitation primitives; avoid disk artifacts and offline cracking where the
  same effect is reachable in-memory. Run self-exiting tools fork&run. Avoid a persistent C2 callback on
  a hardened/EDR-enabled Domain Controller — use a one-shot SYSTEM action for the durable privilege
  change, then operate remotely from a less-monitored foothold as the privileged principal.
gotchas: |
  REFERENCE METHODOLOGY, not a tool and not a script. It is environment-agnostic: derive every concrete
  host, domain, principal, group, GPO, and edge from the LIVE BloodHound graph and the observed
  engagement state — never from this document or from memory. The observed state always wins over any
  prior. This document teaches HOW to plan a path; it never names the path.
related_ttps: [sharpgpoabuse, laps-abuse, golden-ticket, ad-failure-triage, windows-execution-context, lateral-movement-decision, bloodhound-attack-path-loop]
alternatives: []
common_args: {}
last_updated: 2026-06-17
---

# AD Attack-Path Planning Methodology

How to drive a multi-hop Active Directory privilege-escalation objective (e.g. "reach Domain Admin",
"compromise <domain>", "advance the engagement") from **observed state**, not from a memorized path.
Every concrete name — host, domain, user, group, GPO, edge — comes from the live BloodHound graph and
engagement state at run time. This document is a method, not an answer key.

## Core principle: state-driven, graph-derived

- **Classify intent first.** A conversational / non-objective message (greeting, status question, "what
  can you do") is answered and closed — it does NOT cascade into offensive operations. Only an explicit
  objective / path-advancement request authorizes a multi-hop solve, scoped to that stated objective.
- **Graph-first recon is the default opening move** for any path objective: collect on the foothold,
  ingest into BloodHound, verify the graph populated for the *target* domain specifically, then analyze
  traversable edges. (`bloodhound-attack-path-loop`)
- **The plan is a PRIOR, not a script.** Observed state wins on any conflict. Before every hop, consult
  the engagement state: SKIP any hop whose effect is already achieved; do NOT attempt a hop whose
  preconditions aren't met from your current footholds.
- **Idempotence / check-effect-before-hop.** Hop effects and graph data persist across runs and sessions.
  Before each offensive hop, query the graph or do a cheap in-place enumeration for the effect; if it
  already holds, SKIP the primitive and advance. Never re-run a successful primitive.

## The solve loop

```
collect → ingest → query the graph → select the next viable hop (from observed state + invariants)
   → execute ONE in-memory primitive → verify the effect → reason over the new state
   → re-collect / re-query as needed → continue until the objective is reached
     OR no traversable path remains → then report.
```

You own the execution loop: after one action succeeds OR fails, immediately take the next viable action
toward the same sub-goal. Do not end the turn merely to write a progress summary.

## Invariants (rules, not steps)

- **OPSEC scope:** in-memory primitives only; no offline cracking or avoidable disk artifacts; self-exiting
  tools run fork&run.
- **Beaconless on hardened DCs:** do not drop or depend on a persistent callback on an EDR-enabled Domain
  Controller. Use a DC-scoped one-shot SYSTEM action (e.g. a GPO edge) to make a *durable* privilege change
  on the DC, then run all interactive work (DCSync, ticket forging, cross-realm hops) REMOTELY from a
  less-monitored foothold as that privileged principal. Recover credentials where they are LSASS-resident
  (a foothold you already hold), never via a DC implant.
- **Stay on the stated objective:** no unrelated targets, no destructive actions. An explicit operator
  stop/inhibit always outranks an autonomous solve.
- **Group-scope dead-ends:** a GLOBAL group cannot hold a foreign-forest member — do not try to add a
  foreign principal to another forest's Domain Admins. Cross a forest with a legitimate foreign-group
  membership or the domain-local Administrators group, not by foreign global-group injection.
- **Per-realm targeting:** any DN and DC must both belong to the realm you are escalating in. Mismatched
  realm/DN/DC is a targeting bug, not an access bug (see error codes below).

## Execution context — the #1 cause of silent failure

A tool runs under the *agent's* identity unless you DELIBERATELY set the token + Kerberos context. To act
AS another identity, work at the level of Windows primitives — never assume a specific C2's command names:

1. Create a DEDICATED sacrificial logon session — a clean NetOnly / NewCredentials LUID with junk creds
   (does not touch LSASS).
2. Import the target identity's ticket artifact into that LUID via the payload-native ticket store/cache
   primitive — NOT a Kerberos tool's `/ptt` flag.
3. Impersonate that context and run the action — in-process for a clean-returning tool (LDAP/LAPS read,
   native DCSync, a BOF), or fork&run under that identity for a self-exiting tool.
4. Revert + purge afterward.

**Discover THIS callback's primitives** (`get_all_commands_for_payloadtype` + the agent's
`mythic_agents/<agent>.md` capability file) to find its create-token / ticket-inject / impersonate /
inline-exec commands. Never hardcode a C2's verbs. Without a correct context, a ticket-only DCSync returns
no replication rights (stalls at `[rpc]`) and a confidential LDAP read (LAPS `ms-mcs-admpwd`) comes back
EMPTY — that is a WRONG-CONTEXT signal, not "no data." Prefer a REAL user TGT (not a forged ticket) when
foreign-group / PAC membership must survive a trust. NEVER forge or asktgt with a PLACEHOLDER key
(`/aes256:REPLACE_ME`, `/rc4:`) — that means the real secret has not been recovered in the right context yet.
Detail: `windows-execution-context`, `ad-failure-triage`.

## Privilege-escalation primitive selection

Pick the abuse primitive from the controlled object's SCOPE — do not assume it. For a controlled GPO,
first resolve from the graph what it is linked to, which computers it covers, and **whether a Domain
Controller is in that scope** (a GPO linked at the domain root or the DC OU covers the DC). When the scope
includes a DC, a SYSTEM computer-task runs ON the DC, whose context is domain-privileged → make a DURABLE
change there (e.g. add a controlled principal to Domain Admins). Do NOT attempt a DS-Replication self-grant
on the domain head unless your CURRENT identity already holds WriteDACL on the Domain object — a non-DA
user, and SYSTEM on a *member* host, do not, so it returns Access-denied regardless of how cleanly the task
is delivered. (`sharpgpoabuse`)

## Mandatory waits after a GPO-based grant

A privilege granted via GPO is NOT usable immediately. Two waits are mandatory before the dependent hop:

1. **GP refresh.** The SYSTEM task fires on the TARGET's Group-Policy refresh cycle, not at write time.
   A DC refreshes ~every 5 minutes by default (`GroupPolicyRefreshTimeDC`; member hosts ~90 min ±30).
   Enumerate the real interval if reachable; absent value = default 5 for a DC. After writing the task,
   wait the refresh interval, then poll the effect (e.g. group-membership check) until it shows your
   principal — that re-read is VERIFICATION, not a re-attempt. Do not run the dependent hop until confirmed.
2. **Fresh TGT (Kerberos PAC staleness) — no password needed.** Once membership is confirmed, your EXISTING
   ticket still predates the change and its PAC lacks the new group SID — so the dependent action fails even
   though you ARE a member. Refresh without any credential: PURGE the stale tickets, then trigger a fresh
   auth (e.g. touch the DC's share, or a delegation request) so LSASS mints a new TGT/TGS carrying the new
   SID. THEN run the dependent action. Do not re-run it without purging first.

**Error-code discipline (DCSync / replication):**
- `8439` (0x20f7 / DS_DRA_BAD_DN) = wrong realm / DN / DC TARGETING → fix the target.
- `8453` (0x2105 / DS_DRA_ACCESS_DENIED) = the identity you acted AS lacks DS-Replication rights → act as a
  principal that HOLDS them, or self-grant ONLY if your current context already holds WriteDACL on the head.

## Trust traversal

- **Child → parent (intra-forest):** climb via an ExtraSIDs golden ticket forged from the CHILD krbtgt you
  already hold — do NOT try to DCSync the parent first (no rights there yet) and NEVER forge with an
  empty/placeholder key. No SID filtering applies WITHIN a forest, so the injected parent SID is honored.
  Resolve numeric SIDs (`S-1-5-21-…`, not GUID-shaped) from the graph; do not handcraft the Kerberos
  command or modify builder-returned SID/key/domain fields. (`golden-ticket`)
- **Cross-forest trust:** SID filtering is ON across a forest trust — do NOT use SID history or a
  cross-forest golden ticket (foreign ExtraSIDs are stripped; this is the #1 silent-failure hop). Cross with
  a LEGITIMATE foreign-group membership, authenticating AS a real member using that member's REAL ticket
  (recover the member's key, obtain their real TGT/service ticket, import into an isolated context, then act).
  (`laps-abuse` for the confidential-read pattern.)

## Failure triage & anti-cycle

On ANY failure — especially a SILENT one (empty LAPS attribute, DCSync that returns no hash, a task that
reports `success` with no useful output) — diagnose against `ad-failure-triage` and fix the CONTEXT
(re-establish identity + ticket), then re-run ONCE. Never re-issue the same command, permute LUIDs, or use a
placeholder key. If the SAME sub-goal fails ~2× with the SAME blocker via the SAME method: STOP that method
— switch to a materially DIFFERENT primitive toward the same effect, or, if you have none, surface a
CONCRETE named blocker. Repeating an unchanged plan is not progress.

## Full Reference

### Detailed cross-forest confidential-read sequence (generic)

When the next edge is "read a confidential attribute (e.g. LAPS `ms-mcs-admpwd`) on a host in a foreign
forest, reachable only as a member of a cross-forest group":

1. From your position of control in the source forest, DCSync the real member principal for its REAL AES
   key (you must already hold DA/replication in the source forest).
2. Obtain that principal's REAL TGT / service-ticket artifact WITHOUT injecting it into the current process.
3. Create an isolated Kerberos logon context (sacrificial LUID) and import the ticket artifact with the
   payload-native ticket store/cache primitive.
4. Read the confidential attribute IN that ticket context. A read that returns the object WITH the
   expiration attribute (e.g. `ms-mcs-admpwdexpirationtime`) but WITHOUT the secret (`ms-mcs-admpwd`) =
   WRONG CONTEXT, not "no data" — fix the identity; do not permute ticket-cache LUIDs blindly.

### Why a forged ticket fails the cross-forest hop

A golden/forged ticket does not carry the principal's *authentic* foreign-group membership, and SID
filtering strips foreign ExtraSIDs on the forest trust — so the foreign group's rights (the thing you need)
are absent. Only a REAL ticket for a REAL member, whose PAC genuinely contains the membership, survives the
trust. This is why the methodology insists on recovering the member's real key rather than forging.

### Builder-mediated forging (when a command-builder capability exists)

If the harness exposes a capability builder (e.g. `build_capability_commands` for `forge-golden-ticket` /
`ensure-kerberos-context`), prefer it: it resolves numeric SIDs and the verified krbtgt key from graph
facts instead of re-running discovery. Issue the returned sequence verbatim — forge the artifact, establish
the isolated logon context, import the ticket, verify visibility — without adding tool-level `/ptt` flags or
editing returned SID/key/domain fields. On an `invalid_*_sid` return, re-query the graph for the numeric
domain SID and retry with provenance, rather than handcrafting the command.
