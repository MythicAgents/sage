---
name: sage-chat-sessions
description: Repo-local workflow to create and drive multi-turn conversation and supervised Sage native-chat channels from the CLI — create a channel, send the first and subsequent prompts into it (idle-gated), and inspect its contents. The assisted-mode counterpart to sage-live-runner's auto one-shot runner.
---

# Sage Chat Sessions

Use from the repository root. Create and drive **conversation** and **supervised** Sage native-chat
channels from the CLI, as a multi-turn engagement in one channel. This is the assisted-mode
counterpart to `sage-live-runner`'s auto one-shot runner: that skill can create a supervised channel
and post its first prompt, but has no verb to post a *subsequent* prompt into an existing channel and
no conversation-mode creator. A real supervised or conversation engagement is multi-turn in one
channel so the Engagement State Ledger accumulates across turns.

This wrapper reuses `skills/sage-live-runner/scripts/native_chat.py`'s own Mythic primitives
(channel creation, `CreateSageMessage`, request snapshot, latest-request query) — it adds no new
mutation logic. Auto mode is deliberately not offered here; use `native_chat.py run --autonomous`.

Prerequisite: a running Sage serving Mythic (tmux or container) and a live foothold, i.e. the same readiness the reset
skills establish. Credentials resolve exactly as `native_chat.py` does (`MYTHIC_ADMIN_PASSWORD`,
then `MYTHIC_ENV_PATH`).

## Commands

Create a channel (conversation or supervised):

```bash
.venv/bin/python skills/sage-chat-sessions/scripts/chat_sessions.py create \
  --mode supervised --name "Supervised Trust Walker" --max-steps 200 --policy-mode hybrid
.venv/bin/python skills/sage-chat-sessions/scripts/chat_sessions.py create \
  --mode conversation --name "Recon Q and A"
```

**BloodHound settings follow the deployment mode** (`SAGE_DEPLOYMENT_MODE`, see `sage-goad-reset`). A local
tmux Sage's BloodHound MCP loads its own `.env`, so the channel gets none. A `container` or `published` Sage runs
the MCP baked into the image, with no credentials, so `create` writes `BLOODHOUND_URL`, `BLOODHOUND_TOKEN_ID` and
`BLOODHOUND_TOKEN_KEY` from `<SAGE_BLOODHOUND_MCP_DIR>/.env` (resolved like Sage: process env, then
`Payload_Type/sage/.env.local`, then `Payload_Type/sage/.env`; `BLOODHOUND_URL` may also be built from the MCP's
`BLOODHOUND_SCHEME`/`DOMAIN`/`PORT`). A `localhost` URL works because Mythic runs the container with host
networking. `--bloodhound-env FILE` names another file (for an operator with no host checkout of the MCP);
`--no-bloodhound` writes none. The output names the keys written, never their values, and `create` fails if the
server did not store them. Verify the connection with `send --prompt '/bloodhound'` before the first real prompt.

`create` reads the server-stored `ai_metadata.config.mode` back after creation and fails if it does
not match the requested mode, so a reported channel is one the kernel will actually run in that lane.
Supervised defaults to `max_steps=200` and `policy_mode=hybrid`; conversation defaults to 50 steps
(conversation does not task, so the cap is nominal).

Send the first — or any subsequent — prompt into an existing channel:

```bash
.venv/bin/python skills/sage-chat-sessions/scripts/chat_sessions.py send \
  --channel-id 4 --prompt 'Collect BloodHound data through our callback; collection only.'
```

`send` refuses when the channel's latest request is still processing (any non-terminal status),
printing that status, so you do not post a second turn into an in-flight one. Pass `--force` to post
anyway. It returns the new `chat_request_id`.

Inspect a channel's latest (or a specific) request — status plus the message transcript:

```bash
.venv/bin/python skills/sage-chat-sessions/scripts/chat_sessions.py inspect --channel-id 4
.venv/bin/python skills/sage-chat-sessions/scripts/chat_sessions.py inspect --request-id 14
```

List every active Sage AI channel with its mode and latest request status:

```bash
.venv/bin/python skills/sage-chat-sessions/scripts/chat_sessions.py list
```

## Boundaries

- **Approvals stay separate.** A supervised guarded action pauses for HITL. This tool does not
  approve or deny — use `native_chat.py approve-pending` / `reject-pending`, or the Mythic UI. It
  never bypasses HITL.
- **No auto / autonomous mode.** `create` only builds conversation or supervised channels
  (`autonomous_solve` stays false). Autonomous solves belong to `native_chat.py run --autonomous`.
- **One engagement per channel.** Keep a multi-turn session in one channel so its Engagement State Ledger
  accumulates; `inspect` between turns to read what the last turn proved before sending the next.
- **Live tasking is still gated** by the same turn-authority, scope, and approval machinery the
  kernel enforces; a scoped driver token (`SAGE_DRIVER_APITOKEN` + `SAGE_BACKING_APITOKEN_ID`) is
  honored via the reused `native_chat.py` login path.
