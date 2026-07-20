#!/usr/bin/env python3
"""Thin Ludus REST client for Sage ranges — Luminara drives range ops without host SSH.

Reads LUDUS_URL + LUDUS_API_KEY from a named server entry in Sage's .mcp.json.
The default entry is ``ludus``; set ``SAGE_LUDUS_MCP_SERVER`` or pass
``--mcp-server`` to select another profile such as ``ludus_sagerepl``.
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
import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP = str(REPO_ROOT / ".mcp.json")
EXIT_AMBIGUOUS = 3  # no name given + multiple snapshots: caller (agent) must pick and re-invoke
RANGE_ID_ENV = "SAGE_LUDUS_RANGE_ID"
MCP_SERVER_ENV = "SAGE_LUDUS_MCP_SERVER"
DEFAULT_MCP_SERVER = "ludus"

def _selected_mcp_server(mcp_server=None):
    value = str(mcp_server or os.environ.get(MCP_SERVER_ENV) or DEFAULT_MCP_SERVER).strip()
    return value or DEFAULT_MCP_SERVER


def _creds(mcp_server=None):
    with open(MCP, encoding="utf-8") as handle:
        c = json.load(handle)
    server_name = _selected_mcp_server(mcp_server)
    srv = (c.get("mcpServers") or c).get(server_name, {})
    env = srv.get("env", {})
    try:
        return env["LUDUS_URL"].rstrip("/"), env["LUDUS_API_KEY"]
    except KeyError as exc:
        raise RuntimeError(
            f"{MCP} does not contain LUDUS_URL and LUDUS_API_KEY for MCP server {server_name!r}"
        ) from exc

def _selected_range_id(range_id=None):
    value = str(range_id or os.environ.get(RANGE_ID_ENV) or "").strip()
    return value or None


def _with_range_id(path, range_id=None):
    selected = _selected_range_id(range_id)
    if not selected:
        return path
    parts = urlsplit(path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "rangeID" for key, _value in query):
        query.append(("rangeID", selected))
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def _call(method, path, body=None, range_id=None, mcp_server=None):
    url, key = _creds(mcp_server)
    path = _with_range_id(path, range_id)
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

def status(range_id=None, mcp_server=None):
    code, r = _call("GET", "/api/v2/range", range_id=range_id, mcp_server=mcp_server)
    if isinstance(r, dict):
        print(f"range #{r.get('rangeNumber')} state={r.get('rangeState')} lastDeploy={r.get('lastDeployment')}")
        for vm in r.get("VMs", []):
            print(f"  {'ON ' if vm.get('poweredOn') else 'off'} {vm.get('name')}  ip={vm.get('ip')}  pmx={vm.get('proxmoxID')}")
    else:
        print(code, r)

def logs(range_id=None, mcp_server=None):
    code, r = _call("GET", "/api/v2/range/logs?tail=60", range_id=range_id, mcp_server=mcp_server)
    print(code); print(r.get("result", r) if isinstance(r, dict) else r)

def snapshots(range_id=None, mcp_server=None):
    print(_call("GET", "/api/v2/snapshots/list", range_id=range_id, mcp_server=mcp_server))

def snapshot(name, *, include_ram=False, description=None, range_id=None, mcp_server=None):
    print(_call("POST", "/api/v2/snapshots/create", {
        "name": name,
        "description": description or f"snapshot {name}",
        "includeRAM": include_ram,
    }, range_id=range_id, mcp_server=mcp_server))

def rollback(name, range_id=None, mcp_server=None):
    print(_call("POST", "/api/v2/snapshots/rollback", {"name": name}, range_id=range_id, mcp_server=mcp_server))

def _snapshot_names(range_id=None, mcp_server=None):
    """Distinct snapshot names for the range. The API returns one row per VM, so a
    range-wide snapshot appears N times — dedupe by name. Exclude the live 'current'
    ('You are here!') pointer, which is not a restore target."""
    _, r = _call("GET", "/api/v2/snapshots/list", range_id=range_id, mcp_server=mcp_server)
    snaps = r.get("snapshots", []) if isinstance(r, dict) else []
    return sorted({s["name"] for s in snaps if s.get("name") and s.get("name") != "current"})

def _resolve_rollback_target(name, range_id=None, mcp_server=None):
    """Explicit name wins; else one -> use it, many -> disambiguate, none -> fail.
    Non-interactive callers (no TTY) get the list on stdout + EXIT_AMBIGUOUS so the
    orchestrator can prompt the user and re-invoke. Never silently guesses a default."""
    names = _snapshot_names(range_id, mcp_server)
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

def poweron(machines="all", range_id=None, mcp_server=None):
    print(_call("PUT", "/api/v2/range/poweron", _machines_body(machines), range_id=range_id, mcp_server=mcp_server))

def poweroff(machines="all", range_id=None, mcp_server=None):
    print(_call("PUT", "/api/v2/range/poweroff", _machines_body(machines), range_id=range_id, mcp_server=mcp_server))

def _add_ludus_args(parser):
    parser.add_argument(
        "--range-id",
        default=argparse.SUPPRESS,
        help=f"Ludus range ID override (default: ${RANGE_ID_ENV} or API user's default range).",
    )
    parser.add_argument(
        "--mcp-server",
        default=argparse.SUPPRESS,
        help=f"Ludus MCP server entry (default: ${MCP_SERVER_ENV} or {DEFAULT_MCP_SERVER}).",
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    _add_ludus_args(parser)
    sub = parser.add_subparsers(dest="command")

    for name in ("status", "logs", "snapshots"):
        command = sub.add_parser(name)
        _add_ludus_args(command)

    snapshot_parser = sub.add_parser("snapshot")
    _add_ludus_args(snapshot_parser)
    snapshot_parser.add_argument("name")
    snapshot_parser.add_argument("--include-ram", action="store_true")
    snapshot_parser.add_argument("-d", "--description", default=None)

    rollback_parser = sub.add_parser("rollback")
    _add_ludus_args(rollback_parser)
    rollback_parser.add_argument("name", nargs="?")
    rollback_parser.add_argument("--yes", action="store_true")

    for name in ("poweron", "poweroff"):
        command = sub.add_parser(name)
        _add_ludus_args(command)
        command.add_argument("machines", nargs="?", default="all")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    command = args.command or "status"
    range_id = getattr(args, "range_id", None)
    mcp_server = getattr(args, "mcp_server", None)
    if command == "status":
        status(range_id, mcp_server)
    elif command == "logs":
        logs(range_id, mcp_server)
    elif command == "snapshots":
        snapshots(range_id, mcp_server)
    elif command == "snapshot":
        snapshot(
            args.name,
            include_ram=args.include_ram,
            description=args.description,
            range_id=range_id,
            mcp_server=mcp_server,
        )
    elif command == "poweron":
        poweron(args.machines, range_id, mcp_server)
    elif command == "poweroff":
        poweroff(args.machines, range_id, mcp_server)
    elif command == "rollback":
        if not args.yes:
            print("rollback is destructive — pass --yes")
            return 2
        rollback(_resolve_rollback_target(args.name, range_id, mcp_server), range_id, mcp_server)
    else:
        print(f"unknown: {command}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
