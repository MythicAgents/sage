# BloodHound: file-based credentials under a Mythic install

The [README's BloodHound section](../../README.md#bloodhound) covers the recommended path — resolving
`BLOODHOUND_*` credentials through the Mythic chat configuration or user secrets. This page documents the
alternative: persisting a `.env` file for the BloodHound MCP server, and the local-development setup.

## Why the baked-in checkout is not enough on its own

The container bakes its own MCP checkout at `/opt/bloodhound_mcp` so the first connect needs no network. **That
directory is inside the image, not the bind mount** — Mythic mounts only `<mythic>/InstalledServices/sage` onto
`/Mythic`. A `.env` written into `/opt/bloodhound_mcp` with `docker exec` therefore survives a restart but is
destroyed by any image rebuild.

## A `.env` that persists across rebuilds

Put your own MCP checkout inside the mounted service directory and point Sage at it:

```bash
# On the Mythic host — <mythic> is your Mythic installation directory
git clone https://github.com/mwnickerson/bloodhound_mcp.git <mythic>/InstalledServices/sage/bloodhound_mcp
printf 'BLOODHOUND_URL=%s\nBLOODHOUND_TOKEN_ID=%s\nBLOODHOUND_TOKEN_KEY=%s\n' "$BH_URL" "$BH_TOKEN_ID" "$BH_TOKEN_KEY" > <mythic>/InstalledServices/sage/bloodhound_mcp/.env
```

Then set `SAGE_BLOODHOUND_MCP_DIR=/Mythic/bloodhound_mcp` — the **container-side** path for that directory —
through the chat configuration or the container environment.

Trade-offs, so you can pick deliberately: this persists across rebuilds and keeps credentials in a file you
control, but the checkout is no longer pinned by the image, `uv` resolves its dependencies on first connect (so
that connect needs network), and the credentials sit in plaintext inside the service directory. The
chat-configuration route in the README avoids all three.

> The service directory is created owned by root by `mythic-cli install`, so writing the checkout or its `.env`
> there needs elevation — the same ownership note as [custom TLS certificates](../../README.md#custom-tls-certificates).

## Local development outside Mythic

There is no bind mount and no baked checkout: point `SAGE_BLOODHOUND_MCP_DIR` at your own clone and put the `.env`
beside it, exactly as above.
