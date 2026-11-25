---
name: sage-focused-capability-tests
description: Repo-local focused Sage capability, adapter, and notification validation workflow. Use when Codex, Claude Code, or an operator needs to smoke-test one generic capability, validate a Mythic capability adapter path, reproduce a focused ADCS/DCSync/GPO/LAPS/Kerberos behavior, test the fixed-content Slack findings hook, or run a narrow live diagnostic before the full offline suite.
---

# Sage Focused Capability Tests

Use these scripts before burning a full autonomous run. Capability scripts are live or semi-live diagnostics and
should be run only when the relevant callback/lab state exists. The Slack notification probe does not need a
callback or lab, but it sends a real message and therefore requires explicit `--send` acknowledgement.

## Pattern

1. Rediscover callbacks with `$sage-live-runner`.
2. Run the narrow script for the capability under investigation.
3. Inspect verifier output and ledger/state changes.
4. Run focused unit tests and then:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

One tier, no exclusions — a green run means the tree is green. It runs in parallel by default (a parallel pass,
then the `serial`-marked tests in one process), with workers capped by free memory so it can run beside Mythic and Sage; `--serial` runs everything in one process. The four rejected successor-portfolio suites this
once excluded now live append-only under `.sage_history/evaluation/architecture-policy/`. A trailing `supported`
argument is still accepted and ignored. See `docs/development/TEST_TIERS.md`.

## Bundled Scripts

- `build_capability_smoke.py`
- `check_supervised_step_budget.py --output-dir <dir> [--run]` — three assembled supervised steps, each driven to
  one step bound (tool calls, active time, consecutive errors) by an inert share probe with synthetic evidence; only
  the worker's final call after the bound may reach a provider. Default is offline readiness: a local stand-in
  answers behind the bounded transport and no socket may open (`readiness.json`, status `dry_run_ready`). `--run`
  sends those three calls to the route in the `--config` file (default `Payload_Type/sage/.env.local`), refusing it
  unless its model and endpoint hash match `--expected-model` and `--expected-endpoint-sha256` (defaults: the route
  the script binds), with at most 3 requests, 150,000 total tokens, 1,024 output tokens per call, 180 s and no
  retries.
  It writes `set.json` (wire requests, outputs, usage, per-check verdict) and refuses a second set in the same
  directory. It is paid provider activity: run it only with the operator's authorization. It proves the final
  call's provider integration, not Mythic or range behaviour.
- `check_openai_responses.py readiness|run|verify --run-file F` — the OpenAI Responses and Daybreak live check in
  one fresh conversation channel. `readiness` (read-only) needs `--route-file R`: an owner-only file with
  `API_ENDPOINT`, `model` and `SAGE_OPENAI_SAFETY_IDENTIFIER` and no API key (the deployment's `API_KEY` user secret
  or container environment supplies it); it checks Mythic, the deployed source identity, adapter versions, the
  offline Responses tests and a readable `--phoenix-db`. `run` (paid provider activity; operator authorization
  required, `--authorized-by`) sends a deliberate Chat Completions request, corrects the channel to Responses + Red
  and retries, then a read-only `list_callbacks` prompt and a no-tools recall, stopping at 16 provider calls,
  1,048,576 tokens, 4,096 output tokens per call, 15 minutes or any target task. `verify` applies the pass rules
  written in the script to Mythic's rows and the live Phoenix trace (read in place): the tool step passes on a
  call produced and consumed plus one final-answer row (calling agent and cards are observations), and P7 reads
  the deployed controls from Mythic's stored model definition. One attempt per run file.
- `check_model_switch.py readiness|run|verify --run-file F --phoenix-db P` — the in-place model switch live check
  in one fresh conversation channel. Its route file adds `model_b` to the keys above. `readiness` (read-only) also
  checks that Mythic's stored Sage definition declares `/model` and whether the logged-in operator owns an API key
  the channel can use (`apitoken.write`, `chat-ai.write`, `callback.read`). `run` (paid provider activity and
  Mythic writes; operator authorization required, `--authorized-by`) creates that key if missing, so the channel's
  key owner is its creator (Mythic applies its settings rule to that owner), then runs a wrong first model, a
  Responses correction, `/model`, a function-call step, a settings switch to model B, a wrong `/model` and a switch
  back, stopping at 16 provider calls, 1,048,576 tokens, 4,096 output tokens per call, 20 minutes or any target
  task. `verify` applies the per-step pass rules written in the script once. One attempt per run file.
- `check_dependency_advisories.py readiness|run|verify --run-file F --phoenix-db P` — the model-SDK upgrade live
  check on the deployed container's default provider client (no capture, metering or retry policy). Its route file
  is owner-only with `API_ENDPOINT` and `MODEL` and no API key. `readiness` (read-only) checks deployment parity,
  that the container's packages equal this branch's `constraints.txt`, Phoenix and its `/healthz`, the task
  baseline, and the credential by name and status (one active provider block in the installed `.env.local`, a
  keyed verified-TLS `GET /models` from inside the container, no shadowing user secret). `run` (paid provider
  activity; operator authorization required, `--authorized-by`, and `--plan` for `sage-line check-attempt`) sends
  two Conversation prompts and one Supervised prompt, rejecting any approval card, and stops at 20 provider calls,
  300,000 tokens, 20 minutes or any target task. `verify` applies the pass rules written in the script once; a set
  with no provider tool call did not run. One attempt per run file.
- `check_mcp_client_phoenix.py readiness|run|verify --run-file F --phoenix-db P` — the MCP client and Phoenix
  upgrade live check, through native slash dispatch only (no provider call). `readiness` (read-only, `--channel-id`)
  checks deployment parity, that the running container is the local `sage:latest` image with this branch's
  `constraints.txt` (Strawberry 0.327.7, no `langchain-mcp-adapters`), that the channel is idle and carries the three
  BloodHound settings (presence only), BloodHound CE's HTTP status, Phoenix and its `/healthz`, and the task
  baseline. `run` (operator authorization required, `--authorized-by`, and `--plan` for `sage-line check-attempt`)
  sends `/bloodhound`, then `/mcp tools BloodHound`, in that channel inside one five-minute budget, with no retry or
  upload. `verify` applies the pass rules written in the script once. One attempt per run file.
- `check_mythic_container_rc11.py readiness|run|verify --run-file F --phoenix-db P` — the mythic-container 0.7.0rc11
  live check against a Mythic server at `Mythic-v4.0.0` `d8a94943` or later. `readiness` (read-only, `--route-file`,
  `--callback`) checks deployment parity, that the container's packages equal this branch's `constraints.txt`, that
  the lab Mythic checkout contains that commit and its running server image was built after it, that no
  `Sage Watcher` channel exists yet, that the callback is active, Phoenix, the task baseline, the route credential
  by name and status, and the Watcher route and bot token by name. `run` (paid provider activity, one target task
  and one `docker restart sage`; operator authorization required, `--authorized-by`, `--plan` for
  `sage-line check-attempt`, and `--pip-audit`) records the container's audit, then runs a Conversation prompt, a
  Supervised `whoami` whose first approval card it accepts, and in a new locked `Sage Watcher` channel `/watcher
  apply`, `/watcher status`, the restart, `/watcher status` and `/watcher scan`. It stops at 20 provider calls,
  300,000 tokens, 30 minutes or a second target task. `verify` applies the pass rules written in the script once,
  reading the channels' stored metadata from Mythic. One attempt per run file.
- `freeze_provider_wire.py --wire W [--wire W] --substitutions S` — writes the
  `provider_wire_round_trips.json` battery from recorded provider bodies, frozen with the SDK and adapter versions
  it names, and refuses to run on any other versions. Offline; the substitutions file is private.
- `check_bloodhound_ingest_contract.py --image <id> [--json out.json]` — checks a **built** Sage image's baked
  BloodHound MCP serves the ingest contract (byte upload plus exact-job `status`). It runs one throwaway container
  with `--network none`, the entrypoint bypassed, and only the checker mounted read-only; a loopback fake BloodHound
  API answers Sage's own MCP launcher, readiness probe, and decoder. Records the image ID, MCP base commit, and
  status-patch SHA-256. Exit 0 passed, 1 failed, 2 blocked (no Docker or no image — never a pass). It has no
  live-target mode and does not prove real BloodHound ingestion.
- `probe_slack_findings_webhook.py` — sends the exact fixed-content production notice through an explicitly
  configured webhook. It never accepts finding content and never prints the webhook URL. Modern Slack app
  webhooks are bound to their installed channel; select a different channel-bound URL with `--webhook-env NAME`.
  `--channel-id C0123456789` is supported only for legacy custom-integration webhooks, and HTTP success cannot
  establish that a modern webhook honored an override.
- `run_focused_account_context.py`
- `run_focused_adcs_ca_export.py`
- `run_focused_adcs_certificate_auth.py`
- `run_focused_dcsync_account.py`
- `run_focused_endpoint_protection.py`
- `run_focused_local_admin_access.py`
- `run_focused_local_admin_remote_exec.py`
- `run_focused_managed_secret_read.py`
- `run_focused_parameter_group_reference.py` — proves a `@cred:`/`@link:` task reference resolves
  for a parameter outside the `Default` group. `--mode reference` is the proof; `--mode raw-control`
  sends raw credential material instead, which a declared group causes Mythic to reject. Verdict is
  taken from decoded agent output, never from Mythic's `completed` status.
- `run_focused_parent_dcsync.py`
- `run_focused_sid_history.py`
- `run_focused_ticket_context_proof.py`
- `run_offline_suite.py`

## Slack Findings Notification Probe

Keep every webhook URL in Sage's runtime dotenv, the process environment, or a secret manager; never pass one on
the command line. These commands send the real fixed notice:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/probe_slack_findings_webhook.py --send
.venv/bin/python skills/sage-focused-capability-tests/scripts/probe_slack_findings_webhook.py --webhook-env SAGE_FINDINGS_SLACK_WEBHOOK_URL_SECURITY --send
.venv/bin/python skills/sage-focused-capability-tests/scripts/probe_slack_findings_webhook.py --channel-id C0123456789 --send
```

The second form is the supported modern multi-channel strategy: each named secret contains a webhook installed
for that destination. The third form exercises Slack's legacy payload override only. In all cases, confirm the
message in the intended Slack channel because an accepted HTTP response proves delivery acceptance, not routing.
