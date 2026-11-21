# Optional Codex coding-agent worker for Sage's `/acp` connection.
#
# The image carries the pinned Codex ACP adapter (and the Codex release it packages) behind a private Unix-socket
# bridge. It holds no credential and no configuration: the deployer mounts the socket directory, a read-only Codex
# configuration template and an API-key secret file at run time (see README.md and docker-compose.example.yml).
#
#   docker build -t sage-codex-acp:2.9.0 Payload_Type/sage/container/codex

# node:22-bookworm-slim, pinned by digest so the build resolves to the same base every time.
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

LABEL org.opencontainers.image.title="sage-codex-acp" \
      org.opencontainers.image.description="Codex ACP adapter behind a private Unix-socket bridge for Sage" \
      io.sage.acp.adapter="@agentclientprotocol/codex-acp@2.9.0" \
      io.sage.acp.codex="0.171.0" \
      io.sage.acp.protocol="1"

WORKDIR /opt/codex-acp
COPY package.json package-lock.json ./
# `npm ci` installs exactly the lock (adapter 2.9.0, Codex 0.171.0 and its platform binary) and verifies each
# package's integrity hash.
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force \
    && node -e "const v=require('/opt/codex-acp/node_modules/@agentclientprotocol/codex-acp/package.json').version; if (v !== '2.9.0') { throw new Error('adapter ' + v); }" \
    && node -e "const v=require('/opt/codex-acp/node_modules/@openai/codex/package.json').version; if (v !== '0.171.0') { throw new Error('codex ' + v); }"
COPY bridge.mjs ./bridge.mjs

# /workspace is the ACP session's empty, read-only working directory. /transport (socket), /state (per-connection
# state, a tmpfs) and /config (read-only template) are mounted by the deployer.
RUN mkdir -p /workspace /transport /state /config && chmod 0555 /workspace

# Non-root by default; the deployer runs it as the owner of the socket directory (`user:`).
USER node
ENTRYPOINT ["node", "/opt/codex-acp/bridge.mjs"]
