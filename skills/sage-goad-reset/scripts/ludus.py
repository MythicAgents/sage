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
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
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
TLS_CONFIG_KEYS = ("LUDUS_CA_FILE", "LUDUS_TLS_SERVER_NAME", "LUDUS_CERT_SHA256")


def _configured_resource_ledger():
    sage_root = REPO_ROOT / "Payload_Type" / "sage"
    if str(sage_root) not in sys.path:
        sys.path.insert(0, str(sage_root))
    from ai import resource_budget
    return resource_budget.configured_ledger()


def _reserve_effect(amounts, operation):
    ledger = _configured_resource_ledger()
    if ledger is not None:
        ledger.reserve(amounts, operation)


class _MeteredHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, resource_ledger, resource_operation, **kwargs):
        self._resource_ledger = resource_ledger
        self._resource_operation = resource_operation
        super().__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        self._resource_ledger.reserve({"control_requests": 1}, self._resource_operation)
        return super().request(*args, **kwargs)


class _MeteredHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, resource_ledger, resource_operation, **kwargs):
        self._resource_ledger = resource_ledger
        self._resource_operation = resource_operation
        super().__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        self._resource_ledger.reserve({"control_requests": 1}, self._resource_operation)
        return super().request(*args, **kwargs)


class _MeteredHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, ledger, operation):
        super().__init__()
        self.ledger = ledger
        self.operation = operation

    def http_open(self, req):
        factory = lambda host, **kwargs: _MeteredHTTPConnection(
            host, resource_ledger=self.ledger, resource_operation=self.operation, **kwargs
        )
        return self.do_open(factory, req)


class _MeteredHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ledger, operation, context):
        super().__init__(context=context)
        self.ledger = ledger
        self.operation = operation

    def https_open(self, req):
        factory = lambda host, **kwargs: _MeteredHTTPSConnection(
            host, resource_ledger=self.ledger, resource_operation=self.operation, **kwargs
        )
        return self.do_open(factory, req, context=self._context)


def _metered_urlopen(req, *, timeout, context, ledger, operation="ludus-control-request"):
    opener = urllib.request.build_opener(
        _MeteredHTTPHandler(ledger, operation),
        _MeteredHTTPSHandler(ledger, operation, context),
    )
    return opener.open(req, timeout=timeout)

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


def _verified_tls_config(mcp_server=None):
    """Opt-in endpoint trust; a partial specification must never use legacy TLS."""
    with open(MCP, encoding="utf-8") as handle:
        config = json.load(handle)
    server = (config.get("mcpServers") or config).get(_selected_mcp_server(mcp_server), {})
    configured = server.get("env", {})
    values = {key: str(os.environ.get(key) or configured.get(key) or "").strip()
              for key in TLS_CONFIG_KEYS}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise RuntimeError("Ludus verified TLS requires all three LUDUS_CA_FILE, "
                           "LUDUS_TLS_SERVER_NAME and LUDUS_CERT_SHA256")
    ca_file = Path(values["LUDUS_CA_FILE"])
    if not ca_file.is_absolute():
        ca_file = REPO_ROOT / ca_file
    if not ca_file.is_file():
        raise RuntimeError("configured LUDUS_CA_FILE does not exist")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", values["LUDUS_CERT_SHA256"]):
        raise RuntimeError("configured LUDUS_CERT_SHA256 is not a SHA-256 digest")
    server_name = values["LUDUS_TLS_SERVER_NAME"]
    if any(char.isspace() for char in server_name) or "/" in server_name:
        raise RuntimeError("configured LUDUS_TLS_SERVER_NAME is invalid")
    return ca_file, server_name, values["LUDUS_CERT_SHA256"].lower()


class _VerifiedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, context, server_name, certificate_sha256, timeout):
        super().__init__(host, port, context=context, timeout=timeout)
        self._verified_server_name = server_name
        self._certificate_sha256 = certificate_sha256

    def connect(self):
        raw = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self._verified_server_name)
        except BaseException:
            raw.close()
            raise
        try:
            observed = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        except BaseException:
            self.sock.close()
            raise
        if observed != self._certificate_sha256:
            self.sock.close()
            raise RuntimeError("Ludus presented certificate does not match configured pin")


def _verified_call(method, url, path, data, key, trust, ledger):
    endpoint = urlsplit(url)
    if (endpoint.scheme != "https" or not endpoint.hostname or
            endpoint.username is not None or endpoint.password is not None or
            endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise RuntimeError("verified Ludus TLS requires a plain HTTPS base URL")
    ca_file, server_name, certificate_sha256 = trust
    context = ssl.create_default_context(cafile=str(ca_file))
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    if ledger is not None:
        ledger.reserve({"control_requests": 1}, "ludus-control-request")
    connection = _VerifiedHTTPSConnection(
        endpoint.hostname, endpoint.port or 443, context=context,
        server_name=server_name, certificate_sha256=certificate_sha256, timeout=30,
    )
    try:
        connection.request(method, path, body=data,
                           headers={"X-API-KEY": key, "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read().decode()
        if response.status >= 400:
            return response.status, raw[:500]
        try:
            return response.status, json.loads(raw)
        except Exception:
            return response.status, raw
    finally:
        connection.close()

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
    trust = _verified_tls_config(mcp_server)
    if trust is not None:
        return _verified_call(method, url, path, data, key, trust, _configured_resource_ledger())
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        ledger = _configured_resource_ledger()
        result = (
            urllib.request.urlopen(req, timeout=30, context=ctx)
            if ledger is None else
            _metered_urlopen(req, timeout=30, context=ctx, ledger=ledger)
        )
        with result as r:
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

def _print_mutation_result(operation, result):
    code, body = result
    if not 200 <= code < 300:
        raise RuntimeError(f"Ludus {operation} failed with HTTP {code}")
    print((code, body))

def snapshot(name, *, include_ram=False, description=None, range_id=None, mcp_server=None):
    _reserve_effect({"new_staged_snapshots": 1}, "new-staged-snapshot")
    _print_mutation_result("snapshot create", _call("POST", "/api/v2/snapshots/create", {
        "name": name,
        "description": description or f"snapshot {name}",
        "includeRAM": include_ram,
    }, range_id=range_id, mcp_server=mcp_server))

def rollback(name, range_id=None, mcp_server=None):
    _reserve_effect({"physical_restores": 1}, "physical-range-restore")
    _print_mutation_result(
        "snapshot rollback",
        _call("POST", "/api/v2/snapshots/rollback", {"name": name}, range_id=range_id, mcp_server=mcp_server),
    )

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
    items = [m.strip() for m in machines.split(",")] if isinstance(machines, str) else [str(m).strip() for m in machines]
    if not items or any(not item for item in items) or ("all" in items and items != ["all"]):
        raise RuntimeError("power operation requires VM names/IDs or 'all' alone")
    return {"machines": items}


def _resolved_machines_body(machines, range_id=None, mcp_server=None):
    body = _machines_body(machines)
    if body["machines"] == ["all"]:
        return body
    # Ludus 2.0.6 silently ignores numeric IDs yet reports the request count as success.
    # Validate explicit selectors against the selected range and send VM names only.
    code, inventory = _call("GET", "/api/v2/range", range_id=range_id, mcp_server=mcp_server)
    if not 200 <= code < 300 or not isinstance(inventory, dict) or not isinstance(inventory.get("VMs"), list):
        raise RuntimeError("Ludus range inventory unavailable; power operation not sent")
    vms = inventory["VMs"]
    if not vms:
        raise RuntimeError("Ludus range inventory is empty; power operation not sent")
    resolved = []
    for selector in body["machines"]:
        matches = [vm for vm in vms if isinstance(vm, dict) and
                   (vm.get("name") == selector or
                    (re.fullmatch(r"[0-9]+", selector) and str(vm.get("proxmoxID")) == selector))]
        if len(matches) != 1 or not isinstance(matches[0].get("name"), str) or not matches[0]["name"]:
            raise RuntimeError(f"Ludus VM selector {selector!r} is unknown or ambiguous; power operation not sent")
        name = matches[0]["name"]
        if sum(isinstance(vm, dict) and vm.get("name") == name for vm in vms) != 1:
            raise RuntimeError(f"Ludus VM name {name!r} is ambiguous; power operation not sent")
        if name in resolved:
            raise RuntimeError("Ludus VM selector repeats a VM; power operation not sent")
        resolved.append(name)
    return {"machines": resolved}

def poweron(machines="all", range_id=None, mcp_server=None):
    _print_mutation_result(
        "power on",
        _call("PUT", "/api/v2/range/poweron", _resolved_machines_body(machines, range_id, mcp_server),
              range_id=range_id, mcp_server=mcp_server),
    )

def poweroff(machines="all", range_id=None, mcp_server=None):
    _print_mutation_result(
        "power off",
        _call("PUT", "/api/v2/range/poweroff", _resolved_machines_body(machines, range_id, mcp_server),
              range_id=range_id, mcp_server=mcp_server),
    )

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
    try:
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
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
