FROM python:3.13-slim-bookworm AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      g++ \
      && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim-bookworm

COPY --from=builder /install /usr/local

# --- BloodHound MCP used by native chat graph analysis and autonomous execution ---
# Placed before the app COPY so these network-heavy layers cache independently of
# app-code changes. uv runs the stdio MCP server; git fetches it at build time.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv
# Clone the BloodHound MCP and pre-sync its deps so first connect needs no network.
# Override at build time: --build-arg BLOODHOUND_MCP_REF=<branch|tag> --build-arg BLOODHOUND_MCP_REPO=<url>
#
# `master`, not `main` — that repository has no `main` branch. The previous `main` default made every
# build fail at this step.
#
# Sage requires the byte-upload path: mythic_tools.py calls `file_upload` with
# `info_type="upload_bytes"`, and bloodhound_config.py lists `file_upload` in
# REQUIRED_BLOODHOUND_TOOLS. That lived only on `feature/byte-code-upload` until upstream merged it
# (PR #23, 2026-07-30), so `master` now carries it alongside the CE 9.1 edge-schema alignment, the
# opt-in TLS verification override, and the unused-FastMCP removal. If a future `master` ever drops
# `upload_bytes`, BloodHound ingest breaks at runtime while the build still goes green.
ARG BLOODHOUND_MCP_REPO=https://github.com/mwnickerson/bloodhound_mcp.git
ARG BLOODHOUND_MCP_REF=master
RUN git clone --depth 1 --branch ${BLOODHOUND_MCP_REF} ${BLOODHOUND_MCP_REPO} /opt/bloodhound_mcp && \
    cd /opt/bloodhound_mcp && uv sync --no-dev
# Tell Sage where the baked-in BloodHound MCP lives. Native chat connects it in the serving event loop before
# model initialization; autonomous sessions additionally require exact tool admission. Local development can
# override SAGE_BLOODHOUND_MCP_DIR with another checkout.
ENV SAGE_BLOODHOUND_MCP_DIR=/opt/bloodhound_mcp
# Connection is configured at RUNTIME (never bake the token). Required env, injected by the
# operator via Mythic container env / build params:
#   BLOODHOUND_DOMAIN, BLOODHOUND_TOKEN_ID, BLOODHOUND_TOKEN_KEY
#   BLOODHOUND_PORT  (optional; MCP default 443, BloodHound CE web UI is 8080)
#   BLOODHOUND_SCHEME (optional; default https)
# Sage connects via its MCP subsystem (stdio):
#   command="uv", args=["--directory", "/opt/bloodhound_mcp", "run", "main.py"]
# See Payload_Type/sage/ttps/bloodhound-mcp.md for the full standup + connection guidance.

WORKDIR /Mythic/

COPY [".", "."]

CMD ["python3", "main.py"]
