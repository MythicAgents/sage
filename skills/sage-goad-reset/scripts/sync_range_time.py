#!/usr/bin/env python3
"""Check or synchronize GOAD Windows clocks through the Ludus inventory."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.request

import yaml


DEFAULT_MCP_PATH = Path("/home/john/dev/sage/.mcp.json")
DEFAULT_MAX_SKEW_SECONDS = 60.0
RANGE_ID_ENV = "SAGE_LUDUS_RANGE_ID"
MCP_SERVER_ENV = "SAGE_LUDUS_MCP_SERVER"
DEFAULT_MCP_SERVER = "ludus"


def selected_mcp_server(mcp_server: str | None = None) -> str:
    value = str(mcp_server or os.environ.get(MCP_SERVER_ENV) or DEFAULT_MCP_SERVER).strip()
    return value or DEFAULT_MCP_SERVER


def ludus_creds(mcp_path: Path, mcp_server: str | None = None) -> tuple[str, str]:
    data = json.loads(mcp_path.read_text(encoding="utf-8"))
    server_name = selected_mcp_server(mcp_server)
    server = (data.get("mcpServers") or data).get(server_name, {})
    env = server.get("env", {})
    try:
        return env["LUDUS_URL"].rstrip("/"), env["LUDUS_API_KEY"]
    except KeyError as exc:
        raise RuntimeError(
            f"{mcp_path} does not contain LUDUS_URL and LUDUS_API_KEY for MCP server {server_name!r}"
        ) from exc


def selected_range_id(range_id: str | None = None) -> str | None:
    value = str(range_id or os.environ.get(RANGE_ID_ENV) or "").strip()
    return value or None


def with_range_id(path: str, range_id: str | None = None) -> str:
    selected = selected_range_id(range_id)
    if not selected:
        return path
    parts = urlsplit(path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "rangeID" for key, _value in query):
        query.append(("rangeID", selected))
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def load_inventory(
    mcp_path: Path,
    range_id: str | None = None,
    mcp_server: str | None = None,
) -> dict[str, Any]:
    url, api_key = ludus_creds(mcp_path, mcp_server)
    request = urllib.request.Request(
        f"{url}{with_range_id('/api/v2/range/ansibleinventory', range_id)}",
        headers={"X-API-KEY": api_key},
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        payload: Any = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict):
        for key in ("inventory", "ansible_inventory", "result", "data"):
            if payload.get(key):
                payload = payload[key]
                break
    if isinstance(payload, str):
        payload = yaml.safe_load(payload)
    if not isinstance(payload, dict):
        raise RuntimeError("Ludus ansible inventory response was not a mapping")
    return payload


def flatten_inventory(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hosts: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if isinstance(node.get("hosts"), dict):
            for name, values in node["hosts"].items():
                host = hosts.setdefault(str(name), {"inventory_hostname": str(name)})
                if isinstance(values, dict):
                    host.update(values)
        if isinstance(node.get("children"), dict):
            for child in node["children"].values():
                walk(child)
        for key, value in node.items():
            if key not in {"hosts", "children"} and isinstance(value, dict):
                walk(value)

    walk(inventory.get("all", inventory))
    meta = inventory.get("_meta", {})
    hostvars = meta.get("hostvars", {}) if isinstance(meta, dict) else {}
    if isinstance(hostvars, dict):
        for name, values in hostvars.items():
            if isinstance(values, dict):
                hosts.setdefault(str(name), {"inventory_hostname": str(name)}).update(values)
    return hosts


def windows_hosts(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for name, host in flatten_inventory(inventory).items():
        address = str(host.get("ansible_host") or "")
        connection = str(host.get("ansible_connection") or "").casefold()
        if connection != "winrm" and not address.startswith("10.4.10."):
            continue
        if address.endswith(".254"):
            continue
        item = dict(host)
        item.setdefault("inventory_hostname", name)
        selected.append(item)
    return sorted(
        selected,
        key=lambda host: (
            "DC" not in str(host.get("inventory_hostname", "")).upper(),
            str(host.get("inventory_hostname", "")),
        ),
    )


def winrm_session(host: dict[str, Any]) -> Any:
    import winrm

    address = host.get("ansible_host")
    username = host.get("ansible_user")
    password = host.get("ansible_password")
    if not address or not username or not password:
        raise RuntimeError(
            f"{host.get('inventory_hostname')} lacks Ludus WinRM connection variables"
        )
    return winrm.Session(
        f"https://{address}:5986/wsman",
        auth=(str(username), str(password)),
        transport="ntlm",
        server_cert_validation="ignore",
        operation_timeout_sec=20,
        read_timeout_sec=30,
    )


def run_ps(session: Any, script: str, *, check: bool = True) -> dict[str, Any]:
    response = session.run_ps(script)
    stdout = response.std_out.decode("utf-8", errors="replace").strip()
    stderr = response.std_err.decode("utf-8", errors="replace").strip()
    if check and response.status_code != 0:
        raise RuntimeError(stderr or stdout or f"PowerShell exited {response.status_code}")
    return {
        "status_code": response.status_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def clock_offset_seconds(guest_utc: str, controller_utc: datetime) -> float:
    return abs((parse_utc(guest_utc) - controller_utc).total_seconds())


def read_clock(host: dict[str, Any]) -> dict[str, Any]:
    before = datetime.now(timezone.utc)
    response = run_ps(
        winrm_session(host),
        (
            "$svc=Get-CimInstance Win32_Service -Filter \"Name='w32time'\";"
            "[PSCustomObject]@{"
            "Computer=$env:COMPUTERNAME;"
            "Utc=(Get-Date).ToUniversalTime().ToString('o');"
            "Source=(w32tm /query /source 2>&1 | Out-String).Trim();"
            "TimeServiceState=$svc.State;"
            "TimeServiceStartMode=$svc.StartMode"
            "} | ConvertTo-Json -Compress"
        ),
    )
    after = datetime.now(timezone.utc)
    observed = json.loads(response["stdout"])
    midpoint = before + (after - before) / 2
    return {
        "inventory_hostname": host.get("inventory_hostname"),
        "address": host.get("ansible_host"),
        "computer": observed.get("Computer"),
        "guest_utc": observed["Utc"],
        "source": observed.get("Source"),
        "time_service_state": observed.get("TimeServiceState"),
        "time_service_start_mode": observed.get("TimeServiceStartMode"),
        "offset_seconds": round(clock_offset_seconds(observed["Utc"], midpoint), 3),
    }


def check_clocks(hosts: list[dict[str, Any]], max_skew_seconds: float) -> dict[str, Any]:
    results = []
    errors = []
    for host in hosts:
        try:
            results.append(read_clock(host))
        except Exception as exc:
            errors.append(
                {
                    "inventory_hostname": host.get("inventory_hostname"),
                    "error": str(exc),
                }
            )
    over_limit = [
        result
        for result in results
        if result["offset_seconds"] > max_skew_seconds
    ]
    time_service_enabled = [
        result
        for result in results
        if str(result.get("time_service_state") or "").casefold() != "stopped"
        or str(result.get("time_service_start_mode") or "").casefold() != "disabled"
    ]
    return {
        "ready": not errors and not over_limit and not time_service_enabled and bool(results),
        "max_skew_seconds": max_skew_seconds,
        "hosts": results,
        "errors": errors,
        "over_limit": [result["inventory_hostname"] for result in over_limit],
        "time_service_enabled": [
            result["inventory_hostname"] for result in time_service_enabled
        ],
    }


def build_clock_sync_script(target: str) -> str:
    return (
        "Set-Service -Name w32time -StartupType Disabled;"
        "Stop-Service w32time -Force -ErrorAction SilentlyContinue;"
        "$target=[DateTime]::Parse("
        + repr(target)
        + ").ToLocalTime();"
        "Set-Date -Date $target | Out-Null;"
        "[PSCustomObject]@{Computer=$env:COMPUTERNAME;Utc=(Get-Date).ToUniversalTime().ToString('o')}"
        " | ConvertTo-Json -Compress"
    )


def sync_clocks(hosts: list[dict[str, Any]]) -> None:
    for host in hosts:
        target = datetime.now(timezone.utc).isoformat()
        run_ps(winrm_session(host), build_clock_sync_script(target))
        print(f"set {host.get('inventory_hostname')} ({host.get('ansible_host')})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "sync"))
    parser.add_argument("--mcp-path", default=str(DEFAULT_MCP_PATH))
    parser.add_argument(
        "--range-id",
        default=os.environ.get(RANGE_ID_ENV),
        help=f"Ludus range ID override (default: ${RANGE_ID_ENV} or API user's default range).",
    )
    parser.add_argument(
        "--mcp-server",
        default=os.environ.get(MCP_SERVER_ENV),
        help=f"Ludus MCP server entry (default: ${MCP_SERVER_ENV} or {DEFAULT_MCP_SERVER}).",
    )
    parser.add_argument("--max-skew-seconds", type=float, default=DEFAULT_MAX_SKEW_SECONDS)
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hosts = windows_hosts(load_inventory(Path(args.mcp_path), args.range_id, args.mcp_server))
    if not hosts:
        raise RuntimeError("No Windows hosts found in Ludus inventory")
    if args.action == "sync":
        if not args.yes:
            print("sync changes guest clocks; pass --yes", file=sys.stderr)
            return 2
        sync_clocks(hosts)
    result = check_clocks(hosts, args.max_skew_seconds)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
