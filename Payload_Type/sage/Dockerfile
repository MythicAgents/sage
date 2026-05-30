FROM python:3.13-slim-bookworm AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      g++ \
      && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim-bookworm

COPY --from=builder /install /usr/local

# --- BloodHound MCP (baked in for the Phase 1 T1.5 attack-path loop) ---
# Placed before the app COPY so these network-heavy layers cache independently of
# app-code changes. uv runs the stdio MCP server; git fetches it at build time.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv
# Clone the BloodHound MCP and pre-sync its deps so first `mcp-connect` needs no network.
# TODO: pin to a reviewed commit once validated against the lab (currently tracks main).
RUN git clone --depth 1 https://github.com/mwnickerson/bloodhound_mcp.git /opt/bloodhound_mcp && \
    cd /opt/bloodhound_mcp && uv sync --no-dev
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