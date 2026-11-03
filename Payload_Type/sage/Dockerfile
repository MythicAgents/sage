FROM python:3.14-slim-bookworm AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      g++ \
      && rm -rf /var/lib/apt/lists/*

# constraints.txt pins every transitive dependency to the versions a green suite was observed
# against, so the image resolves identically to a developer venv instead of taking whatever PyPI
# serves on build day. It only constrains — it installs nothing, so the dev-only entries in it are
# not selected here.
COPY requirements.txt constraints.txt ./

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -c constraints.txt

FROM python:3.14-slim-bookworm

COPY --from=builder /install /usr/local

# --- BloodHound MCP used by native chat graph analysis and autonomous execution ---
# Placed before the app COPY so these network-heavy layers cache independently of
# app-code changes. uv runs the stdio MCP server; git fetches it at build time.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv
# uv's managed Python must be readable by the unprivileged user Sage runs as (see sage_entrypoint.py), so
# keep it out of root's private home. uv's cache stays under HOME, which the entrypoint makes writable.
# UV_NO_SYNC tells Sage to launch the MCP with `uv run --no-sync`, using the environment synced below as-is.
# Without it, every first MCP connect re-synced (downloading dev dependencies) into a venv the unprivileged
# user cannot write, and BloodHound never connected. The variable itself does not reach the MCP process: the
# stdio client passes it only a fixed safe environment, so Sage turns it into the flag (ai/mcp.py).
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_NO_SYNC=1
# Clone the BloodHound MCP and pre-sync its deps so first connect needs no network.
# Override only with another prospectively verified immutable commit:
# --build-arg BLOODHOUND_MCP_REF=<full-commit> --build-arg BLOODHOUND_MCP_REPO=<url>
#
# Sage's ingest contract has two halves. Upload: mythic_tools.py calls `file_upload` with
# `info_type="upload_bytes"`, which the exact commit below carries. Status: Sage then reads
# `file_upload(info_type="status", job_id=...)` to learn when BloodHound finished ingesting. No upstream
# commit has that operation, so Sage ships it as patches/bloodhound-mcp-ingest-status.patch and applies it
# here. The effective MCP is therefore this base commit PLUS that patch; the patch stays at
# /opt/sage-patches so its SHA-256 can be recorded beside the base commit. A patch that does not apply fails
# the build: an override ref needs a new patch decision, never a skipped one. Syncing /Mythic source cannot
# change /opt/bloodhound_mcp; changing either half needs an image rebuild.
ARG BLOODHOUND_MCP_REPO=https://github.com/mwnickerson/bloodhound_mcp.git
ARG BLOODHOUND_MCP_REF=92a37dd481ce675fe552f14c9957a31dbbcd212e
COPY patches/bloodhound-mcp-ingest-status.patch /opt/sage-patches/bloodhound-mcp-ingest-status.patch
RUN git init /opt/bloodhound_mcp && \
    cd /opt/bloodhound_mcp && \
    git remote add origin ${BLOODHOUND_MCP_REPO} && \
    git fetch --depth 1 origin ${BLOODHOUND_MCP_REF} && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${BLOODHOUND_MCP_REF}" && \
    git apply --check --whitespace=error /opt/sage-patches/bloodhound-mcp-ingest-status.patch && \
    git apply --whitespace=error /opt/sage-patches/bloodhound-mcp-ingest-status.patch && \
    uv sync --no-dev
# Tell Sage where the baked-in BloodHound MCP lives. Native chat connects it in the serving event loop before
# model initialization; autonomous sessions additionally require exact tool admission. Local development can
# override SAGE_BLOODHOUND_MCP_DIR with another checkout.
ENV SAGE_BLOODHOUND_MCP_DIR=/opt/bloodhound_mcp
# Connection is configured at RUNTIME (never bake the token). Required env, injected by the
# operator via Mythic container env / build params:
#   BLOODHOUND_URL, BLOODHOUND_TOKEN_ID, BLOODHOUND_TOKEN_KEY
#   BLOODHOUND_URL is one address (scheme://host:port), e.g. http://localhost:8080 for a
#   stock BloodHound CE. Sage expands it into the three variables the MCP server reads.
# Sage connects via its MCP subsystem (stdio):
#   command="uv", args=["--directory", "/opt/bloodhound_mcp", "run", "main.py"]
# See Payload_Type/sage/ttps/bloodhound-mcp.md for the full standup + connection guidance.

# Restrict LangGraph checkpoint deserialization to its known-safe type allowlist. LangGraph's own
# default is false, and false means any Python callable stored in checkpoint data is imported and
# executed on load — so an attacker able to write sage.db gets execution inside the Sage process.
# Set here as well as in .env so the shipped image is hardened even if that file is replaced; a
# value Mythic injects still wins over this, which keeps a per-deployment override possible.
ENV LANGGRAPH_STRICT_MSGPACK=true

# Build provenance: CI bakes the exact commit and the release version read from the repo-root
# agent_capabilities.json (which is NOT copied into the service dir, so the container could not
# otherwise know it). A plain `mythic-cli` build leaves both "unknown" — the live source fingerprint
# still identifies the code. Baked as ENV so main.py logs them at startup, and as OCI labels for
# `docker inspect`. Declared just before the app COPY so a change never busts the dependency layers
# above.
ARG SAGE_GIT_COMMIT=unknown
ARG SAGE_AGENT_VERSION=unknown
ENV SAGE_GIT_COMMIT=${SAGE_GIT_COMMIT}
ENV SAGE_AGENT_VERSION=${SAGE_AGENT_VERSION}
LABEL org.opencontainers.image.revision=${SAGE_GIT_COMMIT}
LABEL org.opencontainers.image.version=${SAGE_AGENT_VERSION}

# Bytecode caches go to scratch, not into the bind-mounted service directory.
ENV PYTHONPYCACHEPREFIX=/tmp/sage-pycache
# The documented macOS database location (README "SQLite on macOS Docker Desktop / OrbStack") must be
# creatable by the unprivileged runtime user.
RUN install -d -m 1777 /opt/sage_data

# Start as root only long enough to run Sage as the owner of the mounted service directory.
COPY sage_entrypoint.py /usr/local/bin/sage-entrypoint
RUN chmod 0755 /usr/local/bin/sage-entrypoint

WORKDIR /Mythic/

COPY [".", "."]

ENTRYPOINT ["python3", "/usr/local/bin/sage-entrypoint"]
CMD ["python3", "main.py"]
