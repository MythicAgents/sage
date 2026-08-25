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
# Clone the BloodHound MCP and pre-sync its deps so first connect needs no network.
# Override only with another prospectively verified immutable commit:
# --build-arg BLOODHOUND_MCP_REF=<full-commit> --build-arg BLOODHOUND_MCP_REPO=<url>
#
# Sage requires the byte-upload path: mythic_tools.py calls `file_upload` with
# `info_type="upload_bytes"`, and bloodhound_config.py lists `file_upload` in
# REQUIRED_BLOODHOUND_TOOLS. The exact commit below carries that path. Pinning the commit prevents
# a future branch update from silently dropping `upload_bytes` while leaving the image build green.
ARG BLOODHOUND_MCP_REPO=https://github.com/mwnickerson/bloodhound_mcp.git
ARG BLOODHOUND_MCP_REF=92a37dd481ce675fe552f14c9957a31dbbcd212e
RUN git init /opt/bloodhound_mcp && \
    cd /opt/bloodhound_mcp && \
    git remote add origin ${BLOODHOUND_MCP_REPO} && \
    git fetch --depth 1 origin ${BLOODHOUND_MCP_REF} && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${BLOODHOUND_MCP_REF}" && \
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

WORKDIR /Mythic/

COPY [".", "."]

CMD ["python3", "main.py"]
