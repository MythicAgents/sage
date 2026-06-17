#!/usr/bin/env python3
"""Thin Ludus REST client for the GOAD range — Luminara drives range ops without host SSH.

Reads LUDUS_URL + LUDUS_API_KEY from Sage's .mcp.json (the key's user owns range #4 GOADf255df).
Subcommands:
  status                 GET range state + VM power/IP table
  logs                   GET latest deploy logs (tail)
  snapshot <name>        take a Proxmox snapshot of all range VMs (the clean-baseline primitive)
  rollback <name>        roll all range VMs back to <name> (the fast between-eval reset)
  snapshots              list snapshots
  poweron [machines]     PUT power on range VMs (default "all"; CSV of VMID/name for a subset)
  poweroff [machines]    PUT power off range VMs (default "all")
NOTE: destructive ops (rollback) require --yes. Snapshot/rollback endpoints are confirmed against the
live API on first use; if an endpoint path differs this prints the API error verbatim (no silent guess).
"""
from __future__ import annotations
import json, sys, urllib.request, urllib.error, ssl

MCP = "/home/john/dev/sage/.mcp.json"

def _creds():
    c = json.load(open(MCP))
    srv = (c.get("mcpServers") or c).get("ludus", {})
    env = srv.get("env", {})
    return env["LUDUS_URL"].rstrip("/"), env["LUDUS_API_KEY"]

def _call(method, path, body=None):
    url, key = _creds()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            raw = r.read().decode()
            try: return r.status, json.loads(raw)
            except Exception: return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]

def status():
    code, r = _call("GET", "/api/v2/range")
    if isinstance(r, dict):
        print(f"range #{r.get('rangeNumber')} state={r.get('rangeState')} lastDeploy={r.get('lastDeployment')}")
        for vm in r.get("VMs", []):
            print(f"  {'ON ' if vm.get('poweredOn') else 'off'} {vm.get('name')}  ip={vm.get('ip')}  pmx={vm.get('proxmoxID')}")
    else:
        print(code, r)

def logs():
    code, r = _call("GET", "/api/v2/range/logs?tail=60")
    print(code); print(r.get("result", r) if isinstance(r, dict) else r)

def snapshots():
    print(_call("GET", "/api/v2/snapshots/list"))

def snapshot(name):
    print(_call("POST", "/api/v2/snapshots/create", {"name": name, "description": f"clean-baseline {name}", "includeRAM": False}))

def rollback(name):
    print(_call("POST", "/api/v2/snapshots/rollback", {"name": name}))

def _machines_body(machines):
    # API wants {"machines": ["all"]} or a list of VMIDs/names; accept "all" or a CSV string.
    items = [m.strip() for m in machines.split(",")] if isinstance(machines, str) else list(machines)
    return {"machines": items or ["all"]}

def poweron(machines="all"):
    print(_call("PUT", "/api/v2/range/poweron", _machines_body(machines)))

def poweroff(machines="all"):
    print(_call("PUT", "/api/v2/range/poweroff", _machines_body(machines)))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status": status()
    elif cmd == "logs": logs()
    elif cmd == "snapshots": snapshots()
    elif cmd == "snapshot": snapshot(sys.argv[2])
    elif cmd == "poweron": poweron(sys.argv[2] if len(sys.argv) > 2 else "all")
    elif cmd == "poweroff": poweroff(sys.argv[2] if len(sys.argv) > 2 else "all")
    elif cmd == "rollback":
        if "--yes" not in sys.argv: print("rollback is destructive — pass --yes"); sys.exit(2)
        rollback(sys.argv[2])
    else: print(f"unknown: {cmd}"); sys.exit(2)
