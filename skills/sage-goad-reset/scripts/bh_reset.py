#!/usr/bin/env python3
"""BloodHound reset — wipe collected graph data so each eval starts on a FRESH DB.

Uses the BloodHound MCP's own signed base client (reads the MCP's .env), so it targets the exact
instance the MCP/Sage reach — no raw host:port guessing. Run via the MCP's env:
  uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py [status|wipe --yes]
"""
import sys, json
sys.path.insert(0, "/home/john/dev/bloodhound_mcp")
from lib.bloodhound_api import BloodhoundBaseClient  # noqa: E402

c = BloodhoundBaseClient()

def _domains():
    r = c._request("GET", "/api/v2/available-domains")
    try:
        return r.status_code, r.json().get("data", [])
    except Exception:
        return r.status_code, r.text[:200]

def status():
    print("target:", c._format_url("/").rstrip("/"))
    try:
        v = c._request("GET", "/api/version")
        print("api/version:", v.status_code, v.text[:160])
    except Exception as e:
        print("version err:", e)
    code, d = _domains()
    if isinstance(d, list):
        print(f"available-domains: {code} count={len(d)} -> {[x.get('name') for x in d][:8]}")
    else:
        print("available-domains:", code, d)

def wipe():
    body = json.dumps({
        "deleteCollectedGraphData": True,
        "deleteFileIngestHistory": True,
        "deleteDataQualityHistory": True,
        "deleteAssetGroupSelectors": [],
    }).encode()
    r = c._request("POST", "/api/v2/clear-database", body=body)
    print("clear-database:", r.status_code, (r.text[:300] or "(empty body)"))

cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
if cmd == "status":
    status()
elif cmd == "wipe":
    if "--yes" not in sys.argv:
        print("destructive — pass --yes"); sys.exit(2)
    wipe()
else:
    print(f"unknown: {cmd}"); sys.exit(2)
