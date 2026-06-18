#!/usr/bin/env python3
"""Bootstrap fresh Sage/Apollo payloads after a Mythic reset.

This script never deletes files or Mythic objects. It is intended for the reset window:
active Sage/Phoenix DBs are archived, Mythic is reset, local Sage is restarted, then
this helper creates fresh payloads before Apollo is launched on CASTELBLACK.

Examples:
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-sage --provider Bedrock --model us.anthropic.claude-3-5-sonnet-20241022-v2:0
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-apollo --callback-host https://10.4.10.1 --download-dir /tmp/sage_payloads
  .venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-all --callback-host https://10.4.10.1 --download-dir /tmp/sage_payloads
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
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
    host
    user
    active
    payload { payloadtype { name } }
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
) -> dict[str, Any]:
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
    live_apollo = [
        row for row in rows
        if row["payloadtype"] == "apollo"
        and row["live"]
        and str(row.get("host") or "").casefold() == "castelblack"
        and "samwell" in str(row.get("user") or "").casefold()
    ]
    return {
        "ready": bool(live_sage and live_apollo),
        "callbacks": rows,
        "selected_sage_cb": max((row["display_id"] for row in live_sage), default=None),
        "selected_apollo_cb": max((row["display_id"] for row in live_apollo), default=None),
        "required": "fresh live sage callback plus live apollo callback on CASTELBLACK as samwell.tarly",
    }


async def login(args: argparse.Namespace):
    password = args.password or resolve_password(Path(args.env_path))
    return await mythic.login(server_ip=args.server, username=args.user, password=password)


async def inspect(client) -> dict[str, Any]:
    schemas = await mythic.execute_custom_query(client, BOOTSTRAP_SCHEMA_QUERY)
    callbacks = await mythic.execute_custom_query(client, CALLBACK_QUERY)
    return {"schemas": schemas, "callbacks": callbacks.get("callback", [])}


async def readiness(
    client,
    repo_root: Path = REPO_ROOT,
    *,
    runtime_dbs_archived: bool = False,
    operator_db_cleanup_confirmed: bool | None = None,
) -> dict[str, Any]:
    if operator_db_cleanup_confirmed is not None:
        runtime_dbs_archived = operator_db_cleanup_confirmed
    observed = await inspect(client)
    liveness_by_display_id: dict[int, dict[str, Any]] = {}
    for callback in observed.get("callbacks", []):
        display_id = callback.get("display_id")
        if not isinstance(display_id, int):
            continue
        if payload_type_name(callback).lower() not in {"sage", "apollo"}:
            continue
        try:
            liveness_by_display_id[display_id] = await assess_callback_liveness(client, display_id)
        except Exception as exc:
            liveness_by_display_id[display_id] = {"alive": False, "reason": f"liveness check failed: {exc}"}

    runtime = runtime_db_status(repo_root, runtime_dbs_archived=runtime_dbs_archived)
    callbacks = summarize_callback_readiness(observed.get("callbacks", []), liveness_by_display_id)
    blockers = []
    if not runtime["ready"]:
        blockers.append("archive stale Sage/Phoenix runtime DBs before restarting Sage")
    if not callbacks["ready"]:
        blockers.append("fresh live Sage and Apollo callbacks are not both present")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "runtime_databases": runtime,
        "callbacks": callbacks,
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
        ),
        indent=2,
        sort_keys=True,
    ))


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


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=None)
    parser.add_argument("--env-path", default=str(MYTHIC_ENV_PATH))


def add_sage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sage-filename", default="sage-goad-fresh")
    parser.add_argument("--provider", default=os.environ.get("SAGE_PROVIDER", "Bedrock"))
    parser.add_argument("--model", default=os.environ.get("SAGE_MODEL", ""))
    parser.add_argument("--api-endpoint", default=os.environ.get("SAGE_API_ENDPOINT", ""))
    parser.add_argument("--api-key", default=os.environ.get("SAGE_API_KEY", ""))
    parser.add_argument("--aws-access-key-id", default=os.environ.get("AWS_ACCESS_KEY_ID", ""))
    parser.add_argument("--aws-secret-access-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
    parser.add_argument("--aws-session-token", default=os.environ.get("AWS_SESSION_TOKEN", ""))
    parser.add_argument("--aws-default-region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def add_apollo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apollo-filename", default="apollo-castelblack-fresh.exe")
    parser.add_argument("--callback-host", required=True)
    parser.add_argument("--callback-port", default=80, type=int)
    parser.add_argument("--callback-interval", default=3, type=int)
    parser.add_argument("--callback-jitter", default=23, type=int)
    parser.add_argument("--aespsk", default="aes256_hmac", choices=["aes256_hmac", "none"])
    parser.add_argument("--get-uri", default="index")
    parser.add_argument("--post-uri", default="data")
    parser.add_argument("--query-path-name", default="q")
    parser.add_argument("--output-type", default="WinExe", choices=["WinExe", "Shellcode", "Service", "Source"])
    parser.add_argument("--adjust-filename", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--download-dir", default=None)


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
    readiness_parser.set_defaults(func=command_readiness)

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

    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
