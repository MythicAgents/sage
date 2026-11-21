# Codex coding-agent worker

An optional container that lets a Sage chat channel connect to Codex through the
[Agent Client Protocol](https://agentclientprotocol.com) (ACP). This release covers connection and model
configuration only: `/acp` connects, reports status and selects a model. Sage sends the coding agent no
prompt, so no coding happens and no model request is made.

```text
Sage container ── private Unix socket ──> worker: bridge ─ stdio ─> codex-acp 2.9.0 ─> Codex 0.171.0
```

The worker runs beside the Sage chat container on the same Docker host. The two containers share only one
socket directory. The worker holds the Codex configuration and API key; Sage and chat never see the key.

## Deploy

1. Build the image (pinned base image, adapter and Codex versions, with integrity-checked npm packages):

   ```bash
   docker build -t sage-codex-acp:2.9.0 Payload_Type/sage/container/codex
   ```

2. Create the socket directory inside Sage's installed service directory, owned by that directory's owner
   (Sage runs as that user) and private to it:

   ```bash
   install -d -m 0700 -o "$(stat -c %u <mythic>/InstalledServices/sage)" \
       -g "$(stat -c %g <mythic>/InstalledServices/sage)" <mythic>/InstalledServices/sage/.sage_acp
   ```

   The deploy sync and `.gitignore` both exclude `.sage_acp`, so redeploying Sage keeps it.

3. Put the API key alone in a file readable only by that user, outside any repository, and copy
   `config.toml.example` to `config.toml` beside `docker-compose.example.yml` to set defaults such as the
   model. Never put the key in `config.toml`, in the image, in chat or in Sage's environment.

4. Start the worker with `docker-compose.example.yml` (copy and edit it), setting `SAGE_ACP_SOCKET_DIR`,
   `SAGE_CODEX_UID`, `SAGE_CODEX_GID` and `SAGE_CODEX_API_KEY_FILE`. It runs non-root with a read-only root
   filesystem, all capabilities dropped, no Docker socket, no published port, and an init process to reap
   child processes. Codex needs outbound access to its model provider; for a configuration-only check, run
   the worker with `network_mode: none`. With networking disabled, connecting takes about 15 seconds while
   Codex retries its model-list refresh.

5. Set `SAGE_ACP_SOCKET=/Mythic/.sage_acp/acp.sock` in Sage's `Payload_Type/sage/.env` (the installed copy)
   and restart Sage. Unix socket paths are limited to 107 bytes.

## Use

| Command | Effect |
|---|---|
| `/acp` or `/acp status` | Connection state, ACP version, agent version, observed authentication state, effective and saved model |
| `/acp connect codex` | Connect this channel and open a read-only session; reapplies the channel's saved model |
| `/acp models` | List the models the connected agent advertises, with their exact IDs |
| `/acp model <id>` | Select one advertised model for this channel (exact ID) |
| `/acp disconnect` | Close this channel's connection and clear its saved selection |

Each channel has its own connection and session; Watcher has no `/acp`. Only the profile and model selection
survive a Sage restart: after a restart, or a worker restart, the channel shows `disconnected` until someone
runs `/acp connect codex` again. Nothing reconnects on its own. `/stop` also closes the channel's connection
and keeps its saved selection. Sage never logs the shared Codex installation out.

`authenticated` means the agent holds a credential. It does not prove that the provider accepts it; status
always reports provider access as unverified, because Sage makes no model request.

## Operate the worker

Open a shell in the running worker with ordinary Docker administration (the container name is the one in
your compose file):

```bash
docker exec -it sage-codex /bin/sh
```

This starts a new shell in the container as its non-root user. It does not attach to any Sage connection, and
it does not see the API key, which the bridge passes only to each adapter process. Things to know inside:

| Path | Contents |
|---|---|
| `/config/config.toml` | The Codex configuration template, read-only (mounted from the host) |
| `/run/secrets/codex_api_key` | The API-key secret, read-only (mounted from the host) |
| `/state/conn-<n>-*/` | One connection's working directory and `CODEX_HOME`, removed when it closes |
| `/transport/acp.sock` | The socket Sage connects to (mode 0600) |
| `/workspace` | The ACP session's empty, read-only working directory |

To change the configuration or replace the key, edit the mounted host files, then recreate the worker
(`docker compose up -d --force-recreate sage-codex`) and reconnect channels with `/acp connect codex`.
Changes made inside the running container do not persist and do not reach adapters already running.

`docker logs sage-codex` shows one JSON line per event: connections, each relayed message's direction,
method and id (never its parameters), adapter exits and cleanup. Each connection ends by closing the
adapter's input, then TERM and KILL to its process group within five seconds. At most four connections run
at once; a fifth receives a busy error.
