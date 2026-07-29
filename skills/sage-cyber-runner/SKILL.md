---
name: sage-cyber-runner
description: Route authorized Sage cybersecurity implementation, control-plane integration, or live-range execution to a separate Codex process pinned to gpt-5.5-cyber-preview. Use when GPT-5.6 supervises Sage cyber work but native custom-agent selection is unavailable or reports agent_role=null; also use for the offline profile/model smoke test. Require a validated contract and allowlisted control-plane endpoints; never use merely to retry a refusal or bypass safeguards.
---

# Sage Cyber Runner

Use the deterministic runner instead of treating a matching subagent task name as proof that the
`sage_cyber_executor` profile loaded. The runner applies that profile's developer instructions as Codex
configuration, pins `gpt-5.5-cyber-preview`, disables nested agents, and returns a structured handoff to the
GPT-5.6 supervisor.

## Smoke Test

Run from the repository root. No Codex-session restart is required.

```bash
.venv/bin/python skills/sage-cyber-runner/scripts/run_cyber.py smoke
```

Accept the smoke only when the JSON result has `ok: true` and every named check is `true`. The smoke is offline
and read-only.

## Execute A Contract

1. Create a JSON contract matching `references/contract.schema.json`.
2. Prepare it to obtain the canonical SHA-256 and scope summary.
3. For writes, network, or live activity, obtain explicit operator approval of that hash.
4. Run the exact approved hash with the narrowest applicable sandbox.
5. Review the returned `final_message`; the GPT-5.6 supervisor owns verification and evidence admission.

```bash
.venv/bin/python skills/sage-cyber-runner/scripts/run_cyber.py prepare --contract /path/to/contract.json --sandbox read-only
.venv/bin/python skills/sage-cyber-runner/scripts/run_cyber.py run --contract /path/to/contract.json --sandbox read-only
```

Offline work defaults to `read-only`. Use `--sandbox workspace-write` only when the contract sets
`workspace_write_authorized` to `true` and names the permitted file scope.

## Network And Live Modes

For control-plane network access, set `network_activity_authorized` to `true`, set
`workspace_write_authorized` to `true`, use `--sandbox workspace-write`, and enumerate exact hosts under
`network_endpoints`. The runner enables Codex sandbox networking and generates a destination allowlist. Use exact
IP literals for private endpoints; wildcard domain rules do not authorize private addresses.

For live activity, additionally set `live_activity_authorized` to `true` and provide every field in
`live_run_contract`. Allowlist only Mythic, BloodHound, Ludus, model-provider, repository-service, or other
control-plane endpoints. Target-facing LDAP, SMB, Kerberos, WinRM, RPC, HTTP, and similar operations must execute
through authorized Mythic payload tasks, never directly from the worker process.

Headless workers cannot ask the operator questions. For any write, network, or live contract, pass the exact hash
that the operator approved with `--approval-sha256`. The runner recomputes it and fails closed on drift. A changed
endpoint, callback, capability, or scope requires a new hash and approval.

When Codex is running an active `/goal`, set `authorization_mode` to `active-goal` and populate `goal_scope` from
the current goal. The goal command is standing authorization for bounded work inside that objective, so the runner
does not require `--approval-sha256` or ask per-call questions. It must still stop on out-of-goal scope, an unlisted
endpoint, a missing live binding, or a destructive action not implied by the goal.

## Routing Rules

- Route eligible work before the model call based on its authorization contract.
- Never invoke the runner merely because another model refused a request.
- Never place credentials, passwords, tokens, private keys, or raw secrets in a contract.
- Preserve one-writer discipline; do not run a workspace-writing worker alongside overlapping edits.
- Stop on an unlisted endpoint, direct target-service connection, contract drift, or live-run binding mismatch.
- Treat `requested_model` as evidence of the runner's hard pin, not as a claim that account policy no longer
  applies.

## Output

The runner emits JSON containing the requested model, profile name and hash, sandbox/network mode, thread ID,
Codex exit code, usage, validation errors, and the worker's final handoff. It suppresses the full model event
stream so profile instructions and task content are not echoed unnecessarily. The worker remains
`--ephemeral`, while each validated contract and structured final handoff is written privately and manifested
under `.sage_history/`. A persistence failure fails the runner rather than silently leaving the contract or
handoff temp-only.
