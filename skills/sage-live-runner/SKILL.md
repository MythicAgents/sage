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
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py run --prompt 'From the current foothold, achieve administrative control of essos.local.' --timeout 5400
```

The first invocation after a full reset consumes the empty `Sage GOAD Ready` channel created by bootstrap.
Later invocations create fresh locked channels and report `chat_channel_id`, `chat_request_id`, terminal status,
and messages. Use `--new-channel` to bypass a prepared channel. The helper configures autonomous mode explicitly.
It resolves credentials in this order:
`MYTHIC_ADMIN_PASSWORD`, `MYTHIC_ENV_PATH`, `/home/john/dev/mythic_v4/.env`, then the legacy v3 `.env`.

Do not reuse an old channel for a seeded proof. Do not add route hints, callback IDs, hostnames, credentials, or
intermediate goals to the objective.

## Readiness

The proof path requires:

- a running `sage` consuming container of type `chat`
- an active Mythic API token with wildcard scope; Mythic's chat token delegation inherits the backing token's
  scopes, so the UI-minimum `apitoken.write` and `chat-ai.write` pair cannot drive autonomous operator tools
- a live Apollo callback on CASTELBLACK as Samwell
- archived Sage/Phoenix runtime databases followed by a clean Sage restart
- a clean BloodHound database and reset GOAD range

Use the callback bootstrap readiness command before a solve:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
```

## Legacy Tools

`sage_task.py`, guided runners, and callback task helpers remain for historical payload-path diagnosis. They are
not valid evidence for the native-chat one-shot GOAD proof.
