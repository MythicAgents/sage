#!/usr/bin/env python3
"""BloodHound reset — wipe collected graph data so each eval starts on a FRESH DB.

Uses the BloodHound MCP's own signed base client (reads the MCP's .env), so it targets the exact
instance the MCP/Sage reach — no raw host:port guessing. Run via the MCP's env:
  uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py [status|wipe --yes]
"""
import json
import sys
import time

_client = None

def _get_client():
    global _client
    if _client is None:
        sys.path.insert(0, "/home/john/dev/bloodhound_mcp")
        from lib.bloodhound_api import BloodhoundBaseClient
        _client = BloodhoundBaseClient()
    return _client

def _domains():
    c = _get_client()
    r = c._request("GET", "/api/v2/available-domains")
    try:
        return r.status_code, r.json().get("data", [])
    except Exception:
        return r.status_code, r.text[:200]

def _print_domains(code, domains):
    if isinstance(domains, list):
        print(f"available-domains: {code} count={len(domains)} -> {[x.get('name') for x in domains][:8]}")
    else:
        print("available-domains:", code, domains)

def status():
    c = _get_client()
    print("target:", c._format_url("/").rstrip("/"))
    try:
        v = c._request("GET", "/api/version")
        print("api/version:", v.status_code, v.text[:160])
    except Exception as e:
        print("version err:", e)
    _print_domains(*_domains())

def wait_for_empty(*, initial_wait=10, poll_interval=5, attempts=12, sleep=time.sleep):
    print(f"waiting {initial_wait}s for asynchronous database clear")
    sleep(initial_wait)
    for attempt in range(1, attempts + 1):
        code, domains = _domains()
        _print_domains(code, domains)
        if code == 200 and isinstance(domains, list) and not domains:
            return True
        if attempt < attempts:
            sleep(poll_interval)
    return False

def wipe():
    c = _get_client()
    body = json.dumps({
        "deleteCollectedGraphData": True,
        "deleteFileIngestHistory": True,
        "deleteDataQualityHistory": True,
        "deleteAssetGroupSelectors": [],
    }).encode()
    r = c._request("POST", "/api/v2/clear-database", body=body)
    print("clear-database:", r.status_code, (r.text[:300] or "(empty body)"))
    if not 200 <= r.status_code < 300:
        return False
    return wait_for_empty()

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "wipe":
        if "--yes" not in sys.argv:
            print("destructive — pass --yes")
            return 2
        if not wipe():
            print("database clear did not reach available-domains: count=0")
            return 1
    else:
        print(f"unknown: {cmd}")
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
