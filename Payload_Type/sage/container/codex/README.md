# Codex coding-agent worker

An optional container that lets a Sage chat channel connect to Codex through the
[Agent Client Protocol](https://agentclientprotocol.com) (ACP). This release covers connection and model
configuration only: `/acp` connects, reports status and selects a model. Sage sends the coding agent no
prompt, so no coding happens and no model request is made.

**To set it up, follow [SETUP.md](SETUP.md).** This page describes how it works.

```text
Sage container ── private Unix socket ──> worker: bridge ─ stdio ─> codex-acp 2.9.0 ─> Codex 0.171.0
```

## Files

| File | Purpose |
|---|---|
| `.env` | The settings a Mythic user edits in place: API key, default model, worker user, networking |
| `docker-compose.yml` | Builds and runs the worker from `.env`; `docker compose up -d --build` in this directory |
| `Dockerfile`, `package.json`, `package-lock.json` | The image: digest-pinned Node base, adapter 2.9.0 and Codex 0.171.0 installed from the lock with integrity checks |
| `bridge.mjs` | The socket bridge described below |

The deploy sync never overwrites `.env` (a first deploy copies the shipped, credential-free one), and neither image
build includes it.

## How it works

The worker runs beside the Sage chat container on the same Docker host. The two containers share only Sage's
`.sage_acp` directory, which holds the socket (mode 0600, owned by the worker's user, which matches Sage's).

For each connection the bridge starts one Codex adapter in its own process group, with its own working directory and
Codex home under `/state` (a tmpfs). Without a mounted `/config/config.toml` template, the bridge writes a Codex
configuration holding only `CODEX_MODEL`, when that is a valid model ID. The API key comes from a mounted
`/run/secrets/codex_api_key` file if present, otherwise from `CODEX_API_KEY`, and only the adapter process receives
it. The bridge relays ACP messages unchanged and logs each message's direction, method and id, never its parameters.
A message over 1 MiB ends the connection. At most four connections run at once; a fifth receives a busy error. When a
connection ends, the bridge closes the adapter's input, then sends TERM and KILL to its whole process group within
five seconds, and removes its state.

Sage's side (`/acp`) negotiates ACP protocol 1, advertises no filesystem or terminal capability, passes no MCP servers,
opens a read-only session and refuses any permission request. Each chat has its own connection; only the chosen
profile and model are saved, so after a restart a chat shows `disconnected` until someone reconnects. Nothing
reconnects on its own, and Sage never logs the shared Codex installation out.

## Security notes

- The worker runs non-root, with a read-only root filesystem, all capabilities dropped, no published port and no
  Docker socket. Sage does not start it: starting containers would require the host Docker socket, which is
  equivalent to root on the host.
- The key in `.env` is readable by anyone with Mythic access to Sage's files and, inside the worker, by anyone with
  Docker access (`docker inspect`, `docker exec`). Docker Compose cannot give a read-only service an
  environment-sourced secret, so the key is an environment variable. A deployer who prefers a file can mount one at
  `/run/secrets/codex_api_key`; the bridge uses it first.
- With networking enabled, Codex contacts its provider while creating a session (model list, connection setup),
  although Sage sends no prompt. `SAGE_CODEX_NETWORK=none` blocks that for a connection-only check.
- `authenticated` means Codex holds a key; it does not prove the provider accepts it.
