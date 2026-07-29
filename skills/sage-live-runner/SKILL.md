---
name: sage-live-runner
description: Repo-local Sage native Mythic v4 chat execution and inspection workflow. Use for strict one-shot GOAD solves, chat readiness probes, channel/request monitoring, or legacy payload-task diagnosis.
---

# Sage Live Runner

Use from `/home/john/dev/sage`. Sage is a Mythic v4 chat container. A normal solve does not require a Sage
payload or Sage callback.

## Native Chat

Check the running chat container and reusable API token:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py inspect
```

Prepare the visible empty channel used by the next run:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py prepare
```

Run the stock strict one-shot objective through a fresh locked AI channel:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py run --autonomous --prompt 'From the current foothold, achieve administrative control of essos.local.' --timeout 5400
```

`--output-mode full` is the default and returns the complete operator-visible message record. Automated
evaluation consumers may select `--output-mode eval` to receive only the versioned, positive-allowlist runtime
evidence projection. Eval mode does not alter what Sage sees or what Mythic stores and displays.
While a request is in flight, the runner now emits JSONL request-start/progress/terminal events on stderr so
the channel ID, request ID, and evolving status are visible immediately without waiting for the terminal JSON.
Pass `--manifest-path <path>` to write a redacted demo manifest that binds clean/resumed status, runtime identity,
range/snapshot context, callback/channel/request identity, tasks/proofs, and artifact hashes when those inputs are
available. Allocate that path with `sage-artifact-retention` and record it after the run; a decision-bearing demo
manifest must not live only in `/tmp`.

The first invocation after a full reset consumes the empty `Sage GOAD Ready` channel created by bootstrap.
Later invocations create fresh locked channels and report `chat_channel_id`, `chat_request_id`, terminal status,
and messages. A prepared channel is reused only when its stored configuration is actually `mode=auto` with
`autonomous_solve=true`; otherwise the run creates a fresh correctly configured channel. Use `--new-channel` to
bypass a prepared channel. The helper configures autonomous mode explicitly.
It resolves credentials in this order:
`MYTHIC_ADMIN_PASSWORD`, `MYTHIC_ENV_PATH`, `/home/john/dev/mythic_v4/.env`, then the legacy v3 `.env`.

Do not reuse an old channel for a seeded proof. Do not add route hints, callback IDs, hostnames, credentials, or
intermediate goals to the objective.

## Request Inspection And Transcript Export

Inspect one request without waiting, or follow it through terminal completion or a native HITL pause:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py status --request-id <id>
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py follow --latest --channel-id <channel-id>
```

`--latest` is restricted to active Sage AI channels and may be narrowed to one channel. `follow` emits the same
redacted progress heartbeats as `run` and stops at `operator_input_requested` only for an unresolved card whose
input status is exactly `pending`. Historical accepted, rejected, responded, or selected cards do not pause it.

Export a full-fidelity, ordered request transcript for offline analysis:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py transcript --request-id <id>
```

The default export is a private, manifested file under `.sage_history/<year>/<month>/transcripts/native-chat/`.
Use `--output <path>` only for an explicitly temporary or externally managed copy. The export is intentionally
operator-sensitive and is not the allowlisted evaluator projection.
Export fails closed unless the request, channel, and every returned message carry one consistent request identity.

## Supervised Canary

Create a fresh supervised, non-autonomous channel and stop at the first native approval request:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py canary --prompt 'Run pwd on callback 1.' --max-steps 20
```

The canary never reuses a prepared Auto channel and never posts an approval response. It may still perform
control-plane reads needed to construct the proposed action. Approval or rejection remains an explicit Mythic UI
action. The existing `run` command remains Auto and autonomous by default, and since 2026-07-28 it **refuses without an explicit `--autonomous` acknowledgement** (exit 2) and points at `canary`. That guard exists because this warning, and the matching one in `AGENTS.md`, were both already written and neither prevented a `run` invoked as a "read-only probe" from executing a full autonomous solve against the live range.

## BHUSA Demo Helpers

Create a locked BHUSA demo channel owned by the current operation's unique active bot account. The helper selects or creates
that bot's wildcard API token, then fixes the channel metadata to supervised mode, `autonomous_solve=false`,
`policy_mode=hybrid`, `max_steps=200`, and metadata display `expanded; max=15`:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py demo-prepare
```

When a supervised request pauses on exactly one unresolved approval card, accept that exact card through Mythic's
native `chatInputResponse` mutation:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py approve-pending --request-id <id>
```

Deny exactly one unresolved approval card:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py reject-pending --request-id <id>
```

`--response '<text>'` records operator guidance as the rejection message, and the helper submits it as Mythic
action **`respond`**, not `reject`. That distinction is load-bearing: Sage's
`resume_steer_message_for_request` reads `InputResponse.Response` only on `respond`/`select` and returns `""`
for `accept`/`reject`, so a `reject` carrying text drops it silently — the agent sees only the default
`[DENIED by operator] <tool> was not executed.` Omitting `--response` sends a bare `reject`, which is the
shape that produced the 2026-07-28 re-proposal loop. Use the bare form when deliberately exercising the
loop-guard or the post-denial path. Either form: the guarded action is never executed.

`approve-pending` and `reject-pending` share one implementation and one set of fail-closed preconditions: the
selected request must be exactly `streaming`, every returned message must match that request and channel, there
must be exactly one unresolved card, and Mythic's response must echo the exact request/message IDs. Neither
bypasses HITL; both submit the same explicit Mythic action the UI submits, and a rejection carries the same
operator weight as an approval. Mythic's own mapping is authoritative for the action strings
(`sage_chat/hitl.py`: `accept`->`accepted`, `reject`->`rejected`).
Mythic conditionally updates only a still-pending card, but the request can become terminal between this helper's
preflight and Mythic's later request-status check. That upstream race can leave the card resolved while the
approval returns failure; the helper reports the failure and does not retry.

## Readiness

The proof path requires:

- a running `sage` consuming container of type `chat`
- an active Mythic API token with wildcard scope; Mythic's chat token delegation inherits the backing token's
  scopes, so the UI-minimum `apitoken.write` and `chat-ai.write` pair cannot drive autonomous operator tools
- a live Apollo callback on CASTELBLACK as Samwell
- archived Sage/Phoenix runtime databases followed by a clean Sage restart
- a clean BloodHound database and reset GOAD range

Use the shared callback-bootstrap readiness command before a solve:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
```

`native_chat.py inspect` delegates to that same readiness contract instead of claiming readiness from only a
running chat container and token.

## Legacy Tools

`sage_task.py`, guided runners, and callback task helpers remain for historical payload-path diagnosis. They are
not valid evidence for the native-chat one-shot GOAD proof.
