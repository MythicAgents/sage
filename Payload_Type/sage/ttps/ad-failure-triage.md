---
name: AD / Kerberos / RPC Failure Triage — Signature to Corrective Action
category: tradecraft-fundamentals
subcategories: [failure-triage, error-handling, diagnosis, kerberos-errors, dcsync-errors, silent-failures]
tradecraft_tags: [failure, error, troubleshoot, diagnose, why-did-it-fail, what-went-wrong, recover,
                  access-denied, rpc-error, kerberos-error, kdc-err, preauth-failed, clock-skew, skew,
                  dcsync-error, drsuapi, 8439, 8453, ds-dra-bad-dn, ds-dra-access-denied, silent-failure,
                  empty-laps, empty-attribute, placeholder-key, hash-length, principal-unknown,
                  ticket-expired, logon-failure, rpc-unavailable, next-action-on-failure]
mitre_attack: []
source:
  url: https://github.com/MythicAgents/Apollo
  license: none
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  This is an operator-side triage reference, not an offensive action. No host/DC telemetry of its own.
usage_examples:
  - description: A DCSync produced no hash and stopped at [rpc]
    args: "signature: mimikatz dcsync stalls at [rpc] -> diagnosis: wrong logon-session identity (no replication rights)"
  - description: A confidential LDAP attribute came back empty with no error
    args: "signature: object has ms-mcs-admpwdexpirationtime but no ms-mcs-admpwd -> diagnosis: wrong query identity"
  - description: Rubeus rejected a forged ticket key
    args: "signature: 'Hash must be 16, 32 or 64 characters' -> diagnosis: placeholder/empty key; dump the real one first"
opsec_notes: |
  The single most important rule: a CONTEXT failure or a SILENT failure means FIX THE CONTEXT, not retry. Do
  not re-issue the same command, permute ticket-cache LUIDs, or substitute a placeholder key. Re-establish the
  correct identity/ticket (see windows-execution-context) and re-run once.
gotchas: |
  Mythic task status `success`/`completed` means the TASK ran to completion, NOT that the OPERATION succeeded.
  Always read the actual decoded output: a "successful" DCSync with no hash, or a "successful" LDAP read with
  no confidential value, is a FAILED operation. Truncated output ending mid-handshake (e.g. "[rpc] Aut…") is a
  failure, not a partial success.
related_ttps: [windows-execution-context, dcsync, laps-abuse, pass-the-ticket, golden-ticket, rubeus, mimikatz]
alternatives: []
common_args: {}
last_updated: "2026-06-08"
---

# AD / Kerberos / RPC Failure Triage — Signature → Corrective Action

You already know what most of these codes *mean*. The value here is what to *do next* in Sage's context — and
recognizing the **silent** failures that have no error code at all. The meta-rule: **a context/silent failure
means fix the execution context and re-run ONCE — never retry the same call, permute LUIDs, or insert a
placeholder key.**

## Silent failures (NO error code — the ones that waste the most time)

| Signature | What it really means | Do this |
|-----------|----------------------|---------|
| LDAP object returns WITH `ms-mcs-admpwdexpirationtime` but WITHOUT `ms-mcs-admpwd` (or `msLAPS-Password`) | Your query identity lacks `ReadLAPSPassword` — wrong execution context, NOT "LAPS absent" | Authenticate AS a principal that holds the right (see `windows-execution-context`), then re-read. |
| mimikatz `lsadump::dcsync` prints `[DC]…[rpc] Auth…` then STOPS with no hash | The calling identity has no replication rights in that domain — wrong logon-session/token context | Establish context: `make_token`(junk) → `ticket_store_add`(DA/EA TGT) → re-run DCSync → `rev2self`. NOT a DN problem. |
| Mythic task `status: success/completed` but empty / useless output | Task COMPLETED ≠ operation SUCCEEDED | Read the decoded output; treat empty/handshake-only output as a FAILURE and diagnose it. |
| Output truncated mid-handshake (`[rpc] Aut…`, no result) | The op failed/handing after auth setup | Treat as failure; fix context, do not count it as progress. |

## Kerberos / KDC (ticket request & use)

| Signature | Meaning | Do this |
|-----------|---------|---------|
| `Hash must be 16, 32 or 64 characters` (Rubeus) | You passed a PLACEHOLDER/empty key (`REPLACE_ME`, `$(…)`, `/rc4:`) | DCSync the user's REAL key in the right context first, then pass the LITERAL value. |
| `KDC_ERR_PREAUTH_FAILED` (0x18) | Wrong key (bad NT/AES) or clock skew | Verify the key value (must be the real dumped hash); check time sync. |
| `KDC_ERR_S_PRINCIPAL_UNKNOWN` (0x7) | Wrong SPN/service or realm in the request | Fix `/service` (correct SPN) and `/dc` for the target realm. |
| `KDC_ERR_C_PRINCIPAL_UNKNOWN` | Wrong user or wrong realm for that user | Confirm the user exists in the named domain; fix `/domain`. |
| `KRB_AP_ERR_SKEW` / clock skew > 5 min | Time drift vs the DC | Sync/adjust time before retry. |
| `KDC_ERR_TGT_REVOKED` / `…TICKET_EXPIRED` | krbtgt rotated or ticket aged out | Re-forge / re-request the ticket. |

## DCSync / DRSUAPI replication

| Signature | Meaning | Do this |
|-----------|---------|---------|
| `8439 (0x20f7) DS_DRA_BAD_DN` | Wrong DN/DC/forest targeting | Make the DN and the DC both belong to the forest you're replicating (e.g. `DC=essos,DC=local` against the ESSOS DC). |
| `8453 (0x2105) DS_DRA_ACCESS_DENIED` | Missing DS-Replication rights | Grant them (StandIn/dacledit) OR run under a DA/EA ticket via the execution-context pattern, then re-run against the SAME DC. |

## RPC / network / logon

| Signature | Meaning | Do this |
|-----------|---------|---------|
| `1722 RPC server unavailable` | Host down/unreachable/firewall | Connectivity problem, NOT auth — check host/port/route; do not re-auth. |
| `1726 RPC call failed` | Transient/protocol | May retry ONCE; if it repeats, treat as a real blocker. |
| `STATUS_LOGON_FAILURE` / `1326` | Bad creds in `make_token` | Verify the password/hash you supplied. |
| `Access is denied` on `\\host\C$` | No local admin on the target / wrong token | Obtain local admin (LAPS/recovered cred) or impersonate the right identity, then retry. |

## Apollo / tooling quirks

| Signature | Meaning | Do this |
|-----------|---------|---------|
| `takes no command line arguments` / `Failed to create task` | Empty-parameter encoding | The command layer normalizes this — do not blind-retry; if it persists, fix the param form. |
| Implant dies right after running a .NET tool | A self-exiting assembly ran IN-PROCESS and called `Environment.Exit` | Run self-exiting tools FORK&RUN (sacrificial process), not in-process — see `windows-execution-context`. |

## The decision rule
On ANY failure: classify it with the tables above. If it is a **context/silent** failure → re-establish the
right token + Kerberos ticket (`windows-execution-context`) and re-run ONCE. If it is a **targeting** failure
(8439, wrong SPN) → fix the target. If it is **connectivity** (1722) → it is not auth. NEVER respond by
re-issuing the identical command, permuting LUIDs, or inserting a placeholder key — those burn the step budget
without changing the outcome.
