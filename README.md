# Sage

Sage is a native Mythic v4 chat capability that gives operators an AI/LangGraph interface to Mythic callbacks,
payloads, tasks, credentials, files, and BloodHound. Sage runs on the control plane; it is not an implant and does
not create a Sage payload callback.

Sage can answer scoped operator questions or run an explicitly configured autonomous objective. Target-facing
activity always executes through a live Mythic payload callback. The Sage process may query Mythic and BloodHound
control-plane data, but it must not connect directly to target LDAP, SMB, Kerberos, WinRM, RPC, HTTP, or similar
services, and Sage-local attack artifacts are not admissible proof of an objective.

## Repository layout

This is a monorepo with distinct release boundaries:

```text
Payload_Type/sage/    Shipped Mythic chat capability and runtime
docs/                 Public architecture and development documentation
skills/               Operator and developer workflows
ludus/                Range definitions and provisioning
Plans/                Private, temporary planning and handoff material
```

Evaluation and development packages currently retained beneath `Payload_Type/sage/` are excluded from the
container image. Product code cannot import range definitions, evaluation packages, skills, or private plans.
See [Repository Boundaries](docs/architecture/REPOSITORY_BOUNDARIES.md) for the complete contract and migration
strategy.

## Runtime architecture

The runtime entry point is `Payload_Type/sage/main.py`. Importing `sage_chat` registers the `SageChat` container
with Mythic. Each Mythic chat channel owns a reusable LangGraph `Model` session, while each request gets its own
streaming emitter and terminal lifecycle.

The principal runtime packages are:

| Path | Responsibility |
|---|---|
| `Payload_Type/sage/sage_chat/` | Native chat registration, configuration, sessions, streaming, HITL, and slash commands |
| `Payload_Type/sage/ai/langgraph/model.py` | Agent topology and autonomous execution kernel |
| `Payload_Type/sage/ai/langgraph/mythic_tools.py` | Mythic API and capability execution surface |
| `Payload_Type/sage/ai/langgraph/capabilities.py` | Generic capability candidates and structured verifiers |
| `Payload_Type/sage/ai/langgraph/engagement_state.py` | Observed predicates, effects, and execution state |
| `Payload_Type/sage/ai/langgraph/graph_reconciler.py` | BloodHound facts projected into engagement state |
| `Payload_Type/sage/ai/langgraph/access_reconciler.py` | Mythic callback and liveness facts projected into footholds |
| `Payload_Type/sage/ai/trajectory/` | Runtime failure recording and advisory repair metadata |
| `Payload_Type/sage/prompts/` | Externalized agent prompts |
| `Payload_Type/sage/ttps/` | Tradecraft references and pinned tool metadata |

The autonomous loop follows a generic observe → model capabilities → select → execute → verify → repair cycle.
GOAD is a development benchmark, not a strategy source; domain names, hosts, accounts, and attack paths do not
belong in the product runtime.

## Requirements

- A working Mythic v4 installation with native chat-container support.
- Python 3.13 and a repository virtual environment.
- Model-provider credentials or an OpenAI-compatible endpoint.
- For BloodHound-backed graph analysis, BloodHound CE plus a compatible BloodHound MCP checkout.
- For target activity, a live supported Mythic payload callback.

Create the development environment from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r Payload_Type/sage/requirements.txt
```

Do not put credentials in tracked files. Use process environment variables, a local gitignored `.env`, Mythic
user secrets, or a secret manager.

## Model configuration

Native chat resolves configuration in this order:

1. Mythic channel configuration.
2. Mythic user secrets.
3. Sage process environment.
4. Safe defaults where one exists.

The main settings are:

| Setting | Purpose |
|---|---|
| `provider` | `openai`, `bedrock`, `anthropic`, or `ollama` |
| `model` | Provider model identifier |
| `API_ENDPOINT` | Optional OpenAI-compatible or provider endpoint |
| `API_KEY` | Provider API key when required |
| `mode` | `conversation` (default, read/response only), `supervised`, or `auto` |
| `autonomous_solve` | Force the autonomous execution kernel for the channel |
| `policy_mode` | `hybrid`, `symbolic`, or `llm` capability selection |
| `max_steps` | Global model-step ceiling; `0` means unlimited |

Bedrock additionally uses the standard AWS credential and region variables exposed in the channel configuration.
Never infer the effective backend from the model name alone; record the provider and route for live runs.

## BloodHound MCP

Set the MCP checkout directory before starting Sage. This is Sage **runtime** config — the process reads it
to auto-connect BloodHound at startup — so its home is the Sage runtime env file, not the operator-tooling
`.env` at the repository root:

```bash
echo 'SAGE_BLOODHOUND_MCP_DIR=/path/to/bloodhound_mcp' >> Payload_Type/sage/.env
```

The container image bakes its own `ENV SAGE_BLOODHOUND_MCP_DIR=/opt/bloodhound_mcp`; the line above is the
local-development equivalent. Exporting it in your shell also works for a single session.

The default launcher uses `uv --directory "$SAGE_BLOODHOUND_MCP_DIR" run main.py` for the stdio MCP server.
The BloodHound MCP environment is responsible for its own BloodHound URL and API-token configuration.

Autonomous sessions fail closed if BloodHound cannot connect with the graph tools required by the execution
kernel. Ordinary supervised chat remains available in a degraded, fail-soft state so an operator can diagnose or
configure the integration.

## Start Sage locally

The supported development workflow runs Sage in a local `sage` tmux session while Mythic remains Docker-backed.
Create the session once:

```bash
tmux new-session -d -s sage
```

Then use the canonical launcher from the repository root:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR=/path/to/bloodhound_mcp
```

The launcher preserves the effective environment of an existing Sage process, applies explicit `KEY=VALUE`
overrides, verifies the virtual environment before stopping anything, and relaunches inside the same tmux session.
It must not print secret values in status output.

Inspect native chat readiness without submitting an objective:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py inspect --runtime-dbs-archived
```

`--runtime-dbs-archived` is an operator attestation that the active Sage and Phoenix databases were archived
before this Sage process started; it does not delete or ignore them. The comprehensive demo readiness command is documented in
`skills/sage-goad-reset/SKILL.md`. Its default operation is read-only and reports each readiness component instead
of collapsing partial checks into a misleading global success.

## Use Sage in Mythic

After the container registers, create a Mythic chat channel using the `Sage` model. Configure provider, model,
mode, policy, and credentials in the channel or user-secret views. Mythic renders Sage output as markdown and
renders tool executions as updating, collapsible cards.

Supervised mode is appropriate for interactive work and guarded actions. Autonomous mode is intended for an
explicit objective with a prepared callback and verified graph integration.

For scripted native chat, use the maintained runner:

```bash
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py prepare
.venv/bin/python skills/sage-live-runner/scripts/native_chat.py run --prompt 'Describe the scoped objective here.' --timeout 1800
```

The runner reports channel and request identity immediately, then emits progress until Mythic records a terminal
request. Operator prompts usually need full visibility; native chat always emits rich tool cards.

## Runtime state and clean resets

`Payload_Type/sage/sage.db` stores LangGraph checkpoint state.
`Payload_Type/sage/.phoenix/phoenix.db` stores observability history. Treat both as retained trajectory data.

Never delete runtime databases to obtain a clean run. Stop Sage and archive active databases first:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_stop.sh
.venv/bin/python skills/sage-goad-reset/scripts/archive_runtime_dbs.py
```

The archive helper moves active databases to timestamped retained files. The complete lab reset order—including
Mythic, range, clock, BloodHound, Sage, payload, callback, and final readiness gates—is maintained in
`skills/sage-goad-reset/SKILL.md` and `skills/sage-callback-bootstrap/SKILL.md`.

## Verification

Run focused tests for the subsystem being changed, then the offline suite:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

One tier, no exclusions — a green run means the tree is green. The rejected successor-portfolio suites that were
once excluded are retained evaluation evidence and now live under `.sage_history/`.

See [Test Tiers](docs/development/TEST_TIERS.md) for the details. Changes to autonomous execution also require
focused controller/runtime tests and the architecture budget check:

```bash
python3 skills/sage-architecture-governor/scripts/check_arch_budget.py --changed
```

Live range execution is a separate verification lifecycle. A successful live solve does not replace offline
tests, and an evaluation claim requires its own prospective contract and evidence gates.

Project-local Codex specialist profiles are optional development infrastructure. If publishing them, keep their
configuration, profile files, fallback runner, and routing documentation together as described in
[Optional Codex Agent Profiles](docs/development/CODEX_AGENT_PROFILES.md).

## Operator workflows

Reusable operational tooling lives under `skills/`; `skills/README.md` is the index. The most common workflows
are:

- `sage-goad-reset`: archive state, reset and verify the lab, and run comprehensive readiness checks.
- `sage-callback-bootstrap`: verify native chat, create the Apollo payload, establish the retained foothold, and
  verify callback identity and clocks.
- `sage-live-runner`: prepare, run, monitor, and inspect native Mythic chat requests.
- `sage-focused-capability-tests`: narrow capability diagnostics plus explicit offline test tiers.
- `sage-trace-analysis`: inspect Phoenix, Mythic output, and engagement ledgers.
- `sage-trajectory-learning`: export and replay redacted transition data.

Range-specific operators, prompts, credentials, and generated evidence remain development inputs. They are not
part of the shipped Sage capability.

## Security and evidence rules

- Target-facing actions execute through Mythic payload tasks only.
- Objective proof comes from Mythic task output or artifacts, Mythic credential-store state, or BloodHound facts
  derived from payload-collected artifacts.
- `read_credentials` can place raw secrets in model context and traces; use it deliberately.
- Never log `.env` contents, process environments, RabbitMQ credentials, model API keys, or raw credential
  material merely to prove readiness.
- Re-discover live callback identities after every reset.
- A configured or provisioned range is not automatically callback-ready.
- A live evaluation attempt with a setup or measurement defect is burned, not retroactively repaired.

## Development guidance

Read `AGENTS.md` before making changes, then `skills/README.md` for the operator-tool index. Verify important
claims against source, tests, and live state rather than against prose — this documentation has been stale before.

High-risk changes to prompts, agent topology, tool surfaces, capability planning, trajectory behavior, evaluation
drivers, or autonomous execution require the `sage-architecture-governor` workflow and explicit approval before
editing. Do not commit unless the repository owner explicitly asks; the normal workflow leaves review and commits
to the maintainer.
