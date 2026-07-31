# Sage

**Sage is a chat container for [Mythic](https://github.com/its-a-feature/Mythic).** It is not a standalone
tool — it installs into a Mythic server the same way a payload type or C2 profile does, and it has no function
outside one.

> **Requires Mythic v4.0.0 or later.** Native chat containers do not exist before v4.0.0. As of this writing
> that release lives on Mythic's [`Mythic-v4.0.0`](https://github.com/its-a-feature/Mythic/tree/Mythic-v4.0.0)
> branch and has not been merged to the default branch, so you must install Mythic from that branch. Once it
> merges, a normal Mythic installation will satisfy this requirement and this note becomes obsolete.

Sage gives operators an AI/LangGraph interface to Mythic callbacks, payloads, tasks, credentials, files, and
BloodHound. It runs on the control plane; it is not an implant and does not create a Sage payload callback.

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

- **Mythic v4.0.0 or later**, currently the [`Mythic-v4.0.0`](https://github.com/its-a-feature/Mythic/tree/Mythic-v4.0.0)
  branch. Earlier Mythic versions have no native chat-container support and cannot run Sage at all.
- Python 3.13 and a repository virtual environment.
- Model-provider credentials or an OpenAI-compatible endpoint.
- For BloodHound-backed graph analysis, BloodHound CE plus a compatible BloodHound MCP checkout.
- For target activity, a live supported Mythic payload callback.

Create the development environment from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r Payload_Type/sage/requirements-dev.txt -c Payload_Type/sage/constraints.txt
```

Three files, three jobs:

| File | Role |
|---|---|
| `requirements.txt` | **Intent** — the packages Sage depends on directly. This is what the container image installs. |
| `requirements-dev.txt` | Pulls in `requirements.txt` and adds test dependencies. Use this for development. |
| `constraints.txt` | **Resolution** — every transitive package pinned to a version a green suite was observed against. |

The `-c` flag is what makes your environment match the one the tests passed on, and match the image.
Without it pip resolves transitives fresh: a rebuild on 2026-07-29 moved 83 packages, one of which
(`mcp` 1.25.0 → 2.0.0) removed a module Sage imports and produced 24 collection errors from a manifest
nobody had edited.

Regenerate `constraints.txt` after any intentional dependency change, from a venv whose suite is green —
the header in that file carries the exact command.

Do not put credentials in tracked files. Use process environment variables, a local gitignored `.env`, Mythic
user secrets, or a secret manager.

## Configuration load order

Every native-chat setting — model settings and BloodHound credentials alike — resolves through one
chain, first non-empty wins:

1. **Mythic channel configuration** — the per-chat fields you fill in when creating a Sage chat.
2. **Mythic user secrets** — the operator's stored secrets. Preferred home for tokens and API keys.
3. **Sage process environment** — the container's environment, or your shell for local development.
4. **Safe defaults**, where one exists.

Implemented once in `Payload_Type/sage/sage_chat/config.py` (`_resolve`); adding a setting means
adding a lookup, not a new mechanism.

One caveat worth knowing before you rely on layer 3: the BloodHound MCP runs as a **subprocess**, and
the MCP stdio client only passes it a fixed safe subset of Sage's environment (`HOME`, `LOGNAME`,
`PATH`, `SHELL`, `TERM`, `USER`). A `BLOODHOUND_*` variable set on the Sage container therefore does
not reach the MCP server by inheritance — Sage resolves it through the chain above and forwards it
explicitly. See the next section for the file-based alternative.

## Model configuration

Model settings resolve through the chain above. The main settings are:

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

### BloodHound credentials

The MCP server needs to reach your BloodHound CE instance. These resolve through the standard load
order, so the usual place to put them is the Mythic chat configuration or your Mythic user secrets —
the same place your model API key already lives:

| Setting | Purpose |
|---|---|
| `BLOODHOUND_DOMAIN` | BloodHound CE host |
| `BLOODHOUND_TOKEN_ID` | API token ID |
| `BLOODHOUND_TOKEN_KEY` | API token key |
| `BLOODHOUND_PORT` | Optional; MCP default `443`, BloodHound CE web UI is commonly `8080` |
| `BLOODHOUND_SCHEME` | Optional; default `https` |

Sage forwards whatever it resolves into the MCP subprocess. Anything you leave unset is simply not
forwarded, so the server falls back to its own `.env` — which is what keeps the file-based workflow
below working unchanged.

### Configure once, not per chat

The BloodHound connection is **process-global**. The first chat that connects successfully
establishes it for the whole container, and every later chat reuses that connection — so **you only
fill these fields in once, and can leave them blank in subsequent chats**. There is no per-chat
BloodHound setup to repeat.

Three consequences worth knowing:

- **A failed connect establishes nothing.** If the first attempt fails — wrong port, unreachable
  host, missing token — the next chat tries again from scratch. You are not stuck with a bad
  connection, and you do not need to restart to retry.

- **Connect on demand with `/bloodhound`.** New chats auto-connect before the graph is built. If you
  want to retry after fixing something, run `/bloodhound` in the chat. Plain `/bloodhound` is
  idempotent — it reports an existing connection rather than replacing it, so it is also a safe way
  to ask whether BloodHound is connected.

- **Rebind without a restart using `/bloodhound force`.** To point an already-connected container at
  different credentials or a different BloodHound, run `/bloodhound force` (`reconnect` and
  `--force` also work) in a chat configured the way you want. It rebinds using that chat's resolved
  credentials, and the new connection is then the process-global one every later chat reuses.
  `/bloodhound force <directory>` also changes the MCP directory.

  One caveat: the rebind disconnects before it connects, so if the new settings are wrong you are
  left with no BloodHound connection rather than the old one. The failure message says so
  explicitly. Plain `/bloodhound` will then connect again once you have fixed the configuration.

If a connect fails, the returned message names which credentials Sage resolved and which required
ones were missing, so you do not have to read the container log to find out.

### Using a `.env` file instead, under a Mythic install

The container bakes its own MCP checkout at `/opt/bloodhound_mcp` so the first connect needs no
network. **That directory is inside the image, not the bind mount** — Mythic mounts only
`<mythic>/InstalledServices/sage` onto `/Mythic`. A `.env` written into `/opt/bloodhound_mcp` with
`docker exec` therefore survives a restart but is destroyed by any image rebuild.

For a `.env` that persists, put your own MCP checkout inside the mounted service directory and point
Sage at it:

```bash
# On the Mythic host — <mythic> is your Mythic installation directory
git clone https://github.com/mwnickerson/bloodhound_mcp.git <mythic>/InstalledServices/sage/bloodhound_mcp
printf 'BLOODHOUND_DOMAIN=%s\nBLOODHOUND_TOKEN_ID=%s\nBLOODHOUND_TOKEN_KEY=%s\n' "$BH_DOMAIN" "$BH_TOKEN_ID" "$BH_TOKEN_KEY" > <mythic>/InstalledServices/sage/bloodhound_mcp/.env
```

Then set `SAGE_BLOODHOUND_MCP_DIR=/Mythic/bloodhound_mcp` — the **container-side** path for that
directory — through the chat configuration or the container environment.

Trade-offs, so you can pick deliberately: this persists across rebuilds and keeps credentials in a
file you control, but the checkout is no longer pinned by the image, `uv` resolves its dependencies
on first connect (so that connect needs network), and the credentials sit in plaintext inside the
service directory. The chat-configuration route avoids all three.

For local development outside Mythic there is no bind mount and no baked checkout: point
`SAGE_BLOODHOUND_MCP_DIR` at your own clone and put the `.env` beside it, as above.

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

BloodHound is the exception to per-chat configuration: its connection is process-global, so fill the
`BLOODHOUND_*` fields in once and leave them blank in later chats. See
[Configure once, not per chat](#configure-once-not-per-chat).

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
