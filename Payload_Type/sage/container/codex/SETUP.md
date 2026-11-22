# Set up the Codex worker for `/acp`

This creates a Codex container next to the Sage container on the same Docker host, so a Sage chat can connect to it with
`/acp connect codex`. Sage does not start this container: you do, once, with Docker Compose, and Docker keeps it
running after that. This release connects and selects models only; Sage sends Codex no prompt.

Every command below is one line. `<mythic>` means your Mythic install directory (the one with `mythic-cli`).

You need: Sage running as Mythic's `sage` container, a shell on the Docker host, and an OpenAI API key.

## 1. Fill in the worker settings (in Mythic)

Open Sage's files in Mythic and edit `container/codex/.env`:

| Setting | Value |
|---|---|
| `CODEX_API_KEY` | Your OpenAI API key (it can be the same key Sage uses) |
| `CODEX_MODEL` | Optional default model, such as `gpt-6.2-sol`; empty uses Codex's default |
| `SAGE_CODEX_UID`, `SAGE_CODEX_GID` | The owner of Sage's service directory; `1000` unless step 3's check says otherwise |
| `SAGE_CODEX_NETWORK` | `bridge` (normal). `none` blocks all network access, for a connection-only check |

Anyone who can read Sage's files can read this key, just as they can read the key in Sage's own `.env`.

## 2. Point Sage at the worker (in Mythic)

In the same place, edit Sage's own `.env` (top level of Sage's files) and set this line, removing the leading `#`:

```
SAGE_ACP_SOCKET=/Mythic/.sage_acp/acp.sock
```

## 3. Create the socket directory (host shell, once)

Check the owner of Sage's service directory; the two numbers must match `SAGE_CODEX_UID` and `SAGE_CODEX_GID`:

```
stat -c '%u %g' <mythic>/InstalledServices/sage
```

Then create the private directory the two containers share (replace `1000 1000` if the check printed other numbers):

```
sudo install -d -m 0700 -o 1000 -g 1000 <mythic>/InstalledServices/sage/.sage_acp
```

A Sage redeploy keeps this directory.

## 4. Start the worker (host shell)

```
cd <mythic>/InstalledServices/sage/container/codex && docker compose up -d --build
```

The first build downloads about 700 MB. When it is ready, its log shows `"event":"listening"`:

```
docker logs sage-codex
```

## 5. Restart Sage (host shell)

Sage reads its `.env` only when it starts:

```
docker restart sage
```

## 6. Connect from a Sage chat

| Type | You should see |
|---|---|
| `/acp connect codex` | Transport `connected`, ACP v1, the authentication state, and "provider access unverified" |
| `/acp models` | The model IDs Codex offers, with the current one marked |
| `/acp model <id>` | That model selected for this chat (use the exact ID from the list) |
| `/acp` | The chat's connection status |
| `/acp disconnect` | The connection closed and the chat's saved choice cleared |

Each chat has its own connection and model. After a Sage or worker restart, a chat shows `disconnected` until you run
`/acp connect codex` again; it keeps its saved model. `/stop` also closes the chat's connection.

## Change the key or model

Edit `container/codex/.env` in Mythic, then recreate the worker and reconnect each chat with `/acp connect codex`:

```
cd <mythic>/InstalledServices/sage/container/codex && docker compose up -d --force-recreate
```

## Look inside the worker

Open a shell in it (as its non-root user):

```
docker exec -it sage-codex /bin/sh
```

There is no long-running Codex session to attach to: each `/acp connect` starts its own Codex process, with its files
under `/state/conn-*/`, deleted when the chat disconnects. The key is in the container's environment. To watch
connections, with each message's method but never its content:

```
docker logs -f sage-codex
```

## Stop or remove the worker

```
cd <mythic>/InstalledServices/sage/container/codex && docker compose down
```

## If something is wrong

| `/acp` says | What to do |
|---|---|
| Transport `unconfigured` | `SAGE_ACP_SOCKET` is not set: step 2, then step 5 |
| `worker is unavailable (FileNotFoundError …)` | The worker is not running or the directory is wrong: `docker ps`, then steps 3 and 4 |
| `worker is unavailable (PermissionError …)` | The worker's user does not match Sage's: steps 1 and 3, then "Change the key or model" |
| `docker logs sage-codex` shows `"event":"server_error"` | The socket directory is missing or owned by someone else: step 3, then step 4 |
| `unauthenticated`, "No usable API-key configuration" | `CODEX_API_KEY` is empty: step 1, then "Change the key or model" |
| Connecting takes about 20 seconds | Normal with `SAGE_CODEX_NETWORK=none` (Codex retries a model-list refresh) |

`authenticated` means Codex holds a key, not that the provider accepts it; Sage never sends a model request here.

## Sage running from a checkout instead of a container

Use the same compose file from `Payload_Type/sage/container/codex` in the checkout, create
`Payload_Type/sage/.sage_acp` in step 3, and set `SAGE_ACP_SOCKET` to that directory's absolute path plus
`/acp.sock`. The path must be 107 bytes or shorter.
