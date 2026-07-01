#!/usr/bin/env python3
"""Thin Ludus REST client for the GOAD range — Luminara drives range ops without host SSH.

Reads LUDUS_URL + LUDUS_API_KEY from Sage's .mcp.json (the key's user owns range #4 GOADf255df).
Subcommands:
  status                 GET range state + VM power/IP table
  logs                   GET latest deploy logs (tail)
  snapshot <name> [--include-ram] [-d|--description TEXT]
                         take a Proxmox snapshot of all range VMs
  rollback [name]        roll all range VMs back to a snapshot. If [name] is omitted:
                         one snapshot -> use it; many -> pick interactively at a TTY,
                         else print the names and exit 3 for the caller to choose;
                         none -> error. An explicit [name] is validated against the list.
  snapshots              list snapshots
  poweron [machines]     PUT power on range VMs (default "all"; CSV of VMID/name for a subset)
  poweroff [machines]    PUT power off range VMs (default "all")
NOTE: destructive ops (rollback) require --yes. Snapshot/rollback endpoints are confirmed against the
live API on first use; if an endpoint path differs this prints the API error verbatim (no silent guess).
"""
from __future__ import annotations
import json, sys, urllib.request, urllib.error, ssl

MCP = "/home/john/dev/sage/.mcp.json"
EXIT_AMBIGUOUS = 3  # no name given + multiple snapshots: caller (agent) must pick and re-invoke

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

def snapshot(name, *, include_ram=False, description=None):
    print(_call("POST", "/api/v2/snapshots/create", {
        "name": name,
        "description": description or f"snapshot {name}",
        "includeRAM": include_ram,
    }))

def rollback(name):
    print(_call("POST", "/api/v2/snapshots/rollback", {"name": name}))

def _snapshot_names():
    """Distinct snapshot names for the range. The API returns one row per VM, so a
    range-wide snapshot appears N times — dedupe by name. Exclude the live 'current'
    ('You are here!') pointer, which is not a restore target."""
    _, r = _call("GET", "/api/v2/snapshots/list")
    snaps = r.get("snapshots", []) if isinstance(r, dict) else []
    return sorted({s["name"] for s in snaps if s.get("name") and s.get("name") != "current"})

def _resolve_rollback_target(name):
    """Explicit name wins; else one -> use it, many -> disambiguate, none -> fail.
    Non-interactive callers (no TTY) get the list on stdout + EXIT_AMBIGUOUS so the
    orchestrator can prompt the user and re-invoke. Never silently guesses a default."""
    names = _snapshot_names()
    if not names:
        print("no snapshots exist for this range — nothing to roll back to", file=sys.stderr)
        sys.exit(2)
    if name:
        if name in names:
            return name
        print(f"snapshot {name!r} not found. available: {', '.join(names)}", file=sys.stderr)
        sys.exit(2)
    if len(names) == 1:
        return names[0]
    if sys.stdin.isatty():
        print("multiple snapshots — choose one:", file=sys.stderr)
        for i, n in enumerate(names, 1):
            print(f"  {i}) {n}", file=sys.stderr)
        choice = input("number or name: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        if choice in names:
            return choice
        print(f"invalid choice: {choice!r}", file=sys.stderr)
        sys.exit(2)
    # non-interactive (agent/CI): emit the choices and signal the caller to pick
    print("ambiguous: multiple snapshots and no name given — pass one of:", file=sys.stderr)
    for n in names:
        print(n)
    sys.exit(EXIT_AMBIGUOUS)

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
    elif cmd == "snapshot":
        extra = sys.argv[3:]
        desc = next((extra[i+1] for i, a in enumerate(extra)
                     if a in ("--description", "-d") and i+1 < len(extra)), None)
        snapshot(sys.argv[2], include_ram="--include-ram" in extra, description=desc)
    elif cmd == "poweron": poweron(sys.argv[2] if len(sys.argv) > 2 else "all")
    elif cmd == "poweroff": poweroff(sys.argv[2] if len(sys.argv) > 2 else "all")
    elif cmd == "rollback":
        if "--yes" not in sys.argv: print("rollback is destructive — pass --yes"); sys.exit(2)
        name_arg = next((a for a in sys.argv[2:] if not a.startswith("--")), None)
        rollback(_resolve_rollback_target(name_arg))
    else: print(f"unknown: {cmd}"); sys.exit(2)
