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

One tier, no exclusions — a green run means the tree is green. The four rejected successor-portfolio suites this
once excluded now live append-only under `.sage_history/evaluation/architecture-policy/`. A trailing `supported`
argument is still accepted and ignored. See `docs/development/TEST_TIERS.md`.

## Bundled Scripts

- `build_capability_smoke.py`
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
