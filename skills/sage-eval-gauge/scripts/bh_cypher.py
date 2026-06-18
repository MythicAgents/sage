#!/usr/bin/env python3
"""Read-only BloodHound Cypher query via the MCP's own signed base client.

Run through the MCP env (so it targets the exact instance the MCP/Sage reach):
  uv --directory /home/john/dev/bloodhound_mcp run python \
      /home/john/dev/sage/skills/sage-eval-gauge/scripts/bh_cypher.py '<cypher query>'

Prints one JSON line: {"status": <code|null>, "node_count": N}. READ-ONLY; never writes.
Grounded in bh_reset.py's client + CypherClient.run_query (POST /api/v2/graphs/cypher)."""
import json
import sys

sys.path.insert(0, "/home/john/dev/bloodhound_mcp")
from lib.bloodhound_api import BloodhoundBaseClient  # noqa: E402

query = sys.argv[1] if len(sys.argv) > 1 else "MATCH (d:Domain) RETURN d"
client = BloodhoundBaseClient()
body = json.dumps({"query": query, "includeproperties": False}).encode("utf8")
status, node_count = None, 0
try:
    resp = client._request("POST", "/api/v2/graphs/cypher", body)
    status = getattr(resp, "status_code", None)
    try:
        data = (resp.json() or {}).get("data", {}) or {}
        node_count = len(data.get("nodes", {}) or {})
    except Exception:
        node_count = 0
except Exception as exc:  # 404 on no-results is expected on an empty graph
    status = f"error:{type(exc).__name__}"
print(json.dumps({"status": status, "node_count": node_count}))
