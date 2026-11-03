# BloodHound: file-based credentials under a Mythic install

The [README's BloodHound section](../../README.md#bloodhound) covers the recommended path — resolving
`BLOODHOUND_*` credentials through the Mythic chat configuration or user secrets. This page documents the
alternative: persisting a `.env` file for the BloodHound MCP server, and the local-development setup.

## Why the baked-in checkout is not enough on its own

The container bakes its own MCP checkout at `/opt/bloodhound_mcp` so the first connect needs no network. **That
directory is inside the image, not the bind mount** — Mythic mounts only `<mythic>/InstalledServices/sage` onto
`/Mythic`. A `.env` written into `/opt/bloodhound_mcp` with `docker exec` therefore survives a restart but is
destroyed by any image rebuild.

The baked checkout is the pinned MCP commit plus Sage's ingest-status patch
(`Payload_Type/sage/patches/bloodhound-mcp-ingest-status.patch`), applied when the image is built. Sage needs that
`status` operation to verify a collection; see [Ingest readiness](../../README.md#ingest-readiness). Because the
checkout is in the image, only a rebuild changes it: syncing Sage source into `/Mythic` does not.

## A `.env` that persists across rebuilds

Put your own MCP checkout inside the mounted service directory and point Sage at it:

```bash
# On the Mythic host — <mythic> is your Mythic installation directory
git clone https://github.com/mwnickerson/bloodhound_mcp.git <mythic>/InstalledServices/sage/bloodhound_mcp
cd <mythic>/InstalledServices/sage/bloodhound_mcp
git checkout --detach 92a37dd481ce675fe552f14c9957a31dbbcd212e
git apply --check <mythic>/InstalledServices/sage/patches/bloodhound-mcp-ingest-status.patch
git apply <mythic>/InstalledServices/sage/patches/bloodhound-mcp-ingest-status.patch
printf 'BLOODHOUND_URL=%s\nBLOODHOUND_TOKEN_ID=%s\nBLOODHOUND_TOKEN_KEY=%s\n' "$BH_URL" "$BH_TOKEN_ID" "$BH_TOKEN_KEY" > .env
```

Apply the patch only to a clean checkout of that pinned commit; Sage never patches a checkout for you. Without it
the MCP connects and answers graph reads, but `/bloodhound` reports ingest as not ready and Sage refuses to collect.

Then set `SAGE_BLOODHOUND_MCP_DIR=/Mythic/bloodhound_mcp` — the **container-side** path for that directory —
through the chat configuration or the container environment.

Trade-offs, so you can pick deliberately: this persists across rebuilds and keeps credentials in a file you
control, but the checkout is no longer pinned or patched by the image (you keep it at the pinned commit with the
patch applied), `uv` resolves its dependencies on first connect (so that connect needs network), and the
credentials sit in plaintext inside the service directory. The chat-configuration route in the README avoids all
three.

> The service directory is created owned by root by `mythic-cli install`, so writing the checkout or its `.env`
> there needs elevation — the same ownership note as [custom TLS certificates](../../README.md#custom-tls-certificates).

## Local development outside Mythic

There is no bind mount and no baked checkout: point `SAGE_BLOODHOUND_MCP_DIR` at your own clone, check out the
pinned commit, apply the patch from `Payload_Type/sage/patches/`, run `uv sync --no-dev`, and put the `.env` beside
it, exactly as above. A host checkout that passes its own checks says nothing about what a serving image contains;
validate a deployment with that chat's `/bloodhound` answer.
