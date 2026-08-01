---
name: sage-focused-capability-tests
description: Repo-local focused Sage capability and adapter validation workflow. Use when Codex, Claude Code, or an operator needs to smoke-test one generic capability, validate a Mythic capability adapter path, reproduce a focused ADCS/DCSync/GPO/LAPS/Kerberos behavior, or run a narrow live diagnostic before the full offline suite.
---

# Sage Focused Capability Tests

Use these scripts before burning a full autonomous run. They are live or semi-live diagnostics and should be run
only when the relevant callback/lab state exists.

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
