"""Stripped Mythic toolset for the BARE model — the fair baseline for "is Sage's harness better?".

These are Sage-free wrappers over the raw Mythic SDK. The naive model can enumerate callbacks,
payloads, commands, schemas, tasks, operation files, credentials, payload/C2 metadata, and payloads,
then issue Mythic commands itself. It can also explicitly select an existing callback collection and
send its bytes to a separately injected BloodHound upload adapter. It receives no Sage-local tool
inventory, registration helper, target-upload shortcut, sandbox, TTP knowledge, planner, or agent
topology.

STRIPPED (per audit): no engagement-state hook, no dcsync/param normalization, no circuit-breaker,
no schema-repair, no recon-reread guards, no liveness `_compute_liveness` engine, no footprint/OPSEC
annotation, no command-schema cache, no TTP knowledge, no capability/STRIPS planner, and no
BloodHound ingest reconciliation. EXCLUDED entirely (they ARE the harness): liveness verdicts, TTP
tools, execute_capability/build_capability_commands, and TTP-pinned download_tool.

The exact runtime GraphQL documents and installed-SDK call contracts are preflighted before a Gate
1P reset. Harmless read wrappers are also exercised by the bounded operator audit.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import os
import zipfile
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

try:
    from ..comparison_tool_surface import (
        SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES,
        SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES,
        SHIPPING_MYTHIC_TOOL_NAMES,
    )
except ImportError:  # direct module import in focused tests
    from ai.comparison_tool_surface import (  # type: ignore
        SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES,
        SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES,
        SHIPPING_MYTHIC_TOOL_NAMES,
    )

BARE_TOOL_PROFILE = "naive-raw-mythic-bloodhound-v2"
_MYTHIC_SDK_OVERRIDE: ContextVar[Any | None] = ContextVar("raw_mythic_sdk_override", default=None)


def _mythic_sdk() -> Any:
    override = _MYTHIC_SDK_OVERRIDE.get()
    if override is not None:
        return override
    from mythic import mythic
    return mythic

_PAYLOAD_TYPES_QUERY = "query RawPayloadTypes { payloadtype { name } }"
_CREDENTIALS_QUERY = (
    "query RawCredentials { credential { account realm type credential_text metadata } }"
)
_PAYLOAD_DELETE_PREFLIGHT_QUERY = """
query RawPayloadDeletePreflight($uuid: String!) {
  payload(where: {uuid: {_eq: $uuid}}) { uuid deleted }
  callback_aggregate(where: {payload: {uuid: {_eq: $uuid}}}) { aggregate { count } }
}
"""
_PAYLOAD_DELETE_MUTATION = """
mutation RawPayloadSoftDelete($uuid: String!) {
  updatePayload(payload_uuid: $uuid, deleted: true) { status error }
}
"""
_PAYLOAD_INFO_QUERY = """
query RawPayloadInfo {
  payloadtype {
    agent_type
    name
    supported_os
    buildparameters {
      id
      name
      parameter_type
      choices
      default_value
      description
    }
  }
}
"""
_PAYLOAD_C2_PROFILES_QUERY = """
query RawPayloadC2Profiles($payload_type_name: String!) {
  payloadtypec2profile(where: {payloadtype: {name: {_eq: $payload_type_name}}}) {
    payloadtype { name }
    c2profile {
      name
      description
      is_p2p
      c2profileparameters {
        name
        description
        parameter_type
        required
        default_value
        choices
      }
    }
  }
}
"""
_CALLBACK_C2_CONFIG_QUERY = """
query RawCallbackC2Config($id: Int!) {
  callback(where: {display_id: {_eq: $id}}) {
    display_id
    host
    user
    c2profileparametersinstances {
      value
      instance_name
      c2profileparameter { name }
      c2profile { name }
    }
  }
}
"""
_LATEST_COLLECTION_QUERY = """
query RawLatestCollection($cbid: Int!) {
  filemeta(
    where: {
      is_download_from_agent: {_eq: true}
      complete: {_eq: true}
      deleted: {_eq: false}
      task: {callback: {display_id: {_eq: $cbid}}}
    }
    order_by: {id: desc}
    limit: 10
  ) {
    agent_file_id
    filename_utf8
    path
    timestamp
    complete
    deleted
    is_payload
    is_download_from_agent
    task {
      display_id
      command_name
      callback { display_id }
    }
  }
}
"""
_DOWNLOAD_PROBE_QUERY = """
query RawTaskCollectionDownloadProbe {
  filemeta(
    where: {
      complete: {_eq: true}
      deleted: {_eq: false}
      is_payload: {_eq: false}
      is_download_from_agent: {_eq: true}
      task: {}
    }
    order_by: {id: desc}
    limit: 25
  ) {
    agent_file_id
    filename_utf8
    path
    complete
    deleted
    is_payload
    is_download_from_agent
    task {
      display_id
      command_name
      callback { display_id }
    }
  }
}
"""

RAW_MYTHIC_GRAPHQL_DOCUMENTS = {
    "get_payload_types": _PAYLOAD_TYPES_QUERY,
    "read_credentials": _CREDENTIALS_QUERY,
    "delete_payload_preflight": _PAYLOAD_DELETE_PREFLIGHT_QUERY,
    "delete_payload_mutation": _PAYLOAD_DELETE_MUTATION,
    "get_all_payload_info": _PAYLOAD_INFO_QUERY,
    "get_c2_profiles_for_payload": _PAYLOAD_C2_PROFILES_QUERY,
    "get_callback_c2_config": _CALLBACK_C2_CONFIG_QUERY,
    "ingest_collection_source": _LATEST_COLLECTION_QUERY,
    "download_probe": _DOWNLOAD_PROBE_QUERY,
}

# Exact installed-SDK functions used by the wrappers.  Parameter-name and async-kind checks catch
# SDK drift before the arm starts; in particular, get_all_uploaded_files is an async generator.
RAW_MYTHIC_SDK_CONTRACT = {
    "get_all_active_callbacks": ("coroutine", {"mythic"}),
    "execute_custom_query": ("coroutine", {"mythic", "query", "variables"}),
    "get_all_payloads": ("coroutine", {"mythic"}),
    "get_all_commands_for_payloadtype": (
        "coroutine", {"mythic", "payload_type_name", "custom_return_attributes"}
    ),
    "issue_task": (
        "coroutine",
        {"mythic", "command_name", "parameters", "callback_display_id", "wait_for_complete"},
    ),
    "waitfor_for_task_output": ("coroutine", {"mythic", "task_display_id", "timeout"}),
    "get_all_tasks": ("coroutine", {"mythic", "callback_display_id"}),
    "get_all_task_output_by_id": ("coroutine", {"mythic", "task_display_id"}),
    "get_all_uploaded_files": ("async_generator", {"mythic"}),
    "get_payload_by_uuid": (
        "coroutine", {"mythic", "payload_uuid", "custom_return_attributes"}
    ),
    "download_file": ("coroutine", {"mythic", "file_uuid"}),
    "create_credential": (
        "coroutine", {"mythic", "account", "realm", "credential", "credential_type"}
    ),
    "get_operations": ("coroutine", {"mythic"}),
    "create_payload": (
        "coroutine",
        {
            "mythic", "payload_type_name", "filename", "operating_system", "c2_profiles",
            "build_parameters", "include_all_commands",
        },
    ),
}

# Capability equivalence, not spelling equivalence.  The raw side deliberately exposes only a
# stripped form of these shipping capabilities.  There are currently no exceptions.
RAW_MYTHIC_TO_SHIPPING_TOOL = {
    "list_callbacks": "list_callbacks",
    "get_payload_types": "get_payload_names",
    "get_payloads": "get_all_payloads",
    "get_commands": "get_all_commands_for_payloadtype",
    "issue_command": "issue_task_and_waitfor_task_output",
    "get_task_history": "get_task_history_for_callback",
    "get_task_output": "get_all_task_output_by_task_id",
    "get_uploaded_files": "get_all_uploaded_files",
    "read_credentials": "read_credentials",
    "add_credential": "add_credential",
    "get_operations": "get_operations",
    "create_payload": "create_payload",
    "delete_payload": "delete_payload",
    "get_all_payload_info": "get_all_payload_info",
    "get_c2_profiles_for_payload": "get_c2_profiles_for_payload",
    "get_callback_c2_config": "get_callback_c2_config",
    "get_payload_c2_config": "get_payload_c2_config",
    "ingest_collection": "ingest_collection",
}
RAW_MYTHIC_TOOL_EXCEPTIONS: dict[str, str] = {}

# Accepted reverse audit: every prompt-visible shipping capability has exactly one disposition.
# `ADD_RAW` remains the historical design decision for the five tools implemented in profile v2.
SHIPPING_MYTHIC_REVERSE_DISPOSITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "list_callbacks": ("ALREADY_DIRECT", ("list_callbacks",)),
    "get_all_command_args_for_payloadtype": ("ALREADY_COMPOSABLE", ("get_commands",)),
    "get_all_command_names_for_payloadtype": ("ALREADY_COMPOSABLE", ("get_commands",)),
    "get_all_commands_for_payloadtype": ("ALREADY_DIRECT", ("get_commands",)),
    "wait_for_seconds": ("DO_NOT_ADD_TREATMENT", ()),
    "issue_task_and_waitfor_task_output": ("ALREADY_DIRECT", ("issue_command",)),
    "get_task_history_for_callback": ("ALREADY_DIRECT", ("get_task_history",)),
    "get_all_task_output_by_task_id": ("ALREADY_DIRECT", ("get_task_output",)),
    "upload_file_by_file_uuid": ("ALREADY_COMPOSABLE", ("get_commands", "issue_command")),
    "get_all_uploaded_files": ("ALREADY_DIRECT", ("get_uploaded_files",)),
    "get_operations": ("ALREADY_DIRECT", ("get_operations",)),
    "read_credentials": ("ALREADY_DIRECT", ("read_credentials",)),
    "add_credential": ("ALREADY_DIRECT", ("add_credential",)),
    "execute_capability": ("DO_NOT_ADD_TREATMENT", ()),
    "build_capability_commands": ("DO_NOT_ADD_TREATMENT", ()),
    "get_ttp_guidance": ("DO_NOT_ADD_TREATMENT", ()),
    "get_ttp_full_reference": ("DO_NOT_ADD_TREATMENT", ()),
    "list_ttp_categories": ("DO_NOT_ADD_TREATMENT", ()),
    "ensure_tool_uploaded": ("DO_NOT_ADD_TREATMENT", ()),
    "download_tool": ("DO_NOT_ADD_TREATMENT", ()),
    "ingest_collection": ("ADD_RAW", ("ingest_collection",)),
    "list_open_artifacts": ("DO_NOT_ADD_TREATMENT", ()),
    "get_payload_names": ("ALREADY_DIRECT", ("get_payload_types",)),
    "create_payload": ("ALREADY_DIRECT", ("create_payload",)),
    "get_all_payload_info": ("ADD_RAW", ("get_all_payload_info",)),
    "get_all_payloads": ("ALREADY_DIRECT", ("get_payloads",)),
    "get_c2_profiles_for_payload": ("ADD_RAW", ("get_c2_profiles_for_payload",)),
    "get_callback_c2_config": ("ADD_RAW", ("get_callback_c2_config",)),
    "get_payload_c2_config": ("ADD_RAW", ("get_payload_c2_config",)),
    "download_payload": ("ALREADY_COMPOSABLE", ("get_payloads", "get_commands", "issue_command")),
    "delete_payload": ("ALREADY_DIRECT", ("delete_payload",)),
}

# GraphQL attribute fragment for command enumeration (commands + full parameter schema) — raw Mythic data.
_COMMANDS_ATTR = """
cmd
description
help_cmd
needs_admin
commandparameters {
  name
  type
  description
  default_value
  choices
  parameter_group_name
  required
}
"""


# --- tool implementations (async; each takes the logged-in client + args, returns a string) ----------

async def _list_callbacks(client) -> str:
    """List all active Mythic callbacks (id, host, user, payload type) so the model can see/pivot."""
    mythic = _mythic_sdk()
    return json.dumps(await mythic.get_all_active_callbacks(client), default=str)


async def _get_payload_types(client) -> str:
    """List the payload types installed on this Mythic (apollo, poseidon, merlin, ...)."""
    mythic = _mythic_sdk()
    r = await mythic.execute_custom_query(client, _PAYLOAD_TYPES_QUERY)
    return json.dumps([p["name"] for p in (r.get("payloadtype") or [])])


async def _get_payloads(client) -> str:
    """List the built/registered payloads in Mythic."""
    mythic = _mythic_sdk()
    return json.dumps(await mythic.get_all_payloads(client), default=str)


async def _get_all_payload_info(client) -> str:
    """Return raw payload-type, supported-OS, and build-parameter metadata."""
    mythic = _mythic_sdk()
    return json.dumps(
        await mythic.execute_custom_query(client, _PAYLOAD_INFO_QUERY),
        default=str,
        sort_keys=True,
    )


async def _get_c2_profiles_for_payload(client, payload_type: str) -> str:
    """Return the raw C2 profile and parameter schemas available to one payload type."""
    mythic = _mythic_sdk()
    return json.dumps(
        await mythic.execute_custom_query(
            client,
            _PAYLOAD_C2_PROFILES_QUERY,
            variables={"payload_type_name": str(payload_type)},
        ),
        default=str,
        sort_keys=True,
    )


async def _get_callback_c2_config(client, callback_display_id: int) -> str:
    """Return raw configured C2 parameter values for one callback."""
    mythic = _mythic_sdk()
    result = await mythic.execute_custom_query(
        client,
        _CALLBACK_C2_CONFIG_QUERY,
        variables={"id": int(callback_display_id)},
    )
    return json.dumps(result, default=str, sort_keys=True)


_PAYLOAD_C2_ATTRIBUTES = """
uuid
file_id
filemetum {
  agent_file_id
  filename_utf8
  id
}
c2profileparametersinstances {
  value
  c2profileparameter { name }
  c2profile { name }
}
"""


async def _get_payload_c2_config(client, payload_uuid: str) -> str:
    """Return raw configured C2 values and file metadata for one built payload."""
    mythic = _mythic_sdk()
    return json.dumps(
        await mythic.get_payload_by_uuid(
            mythic=client,
            payload_uuid=str(payload_uuid),
            custom_return_attributes=_PAYLOAD_C2_ATTRIBUTES,
        ),
        default=str,
        sort_keys=True,
    )


async def _get_commands(client, payload_type: str) -> str:
    """Enumerate every command + full argument schema for a payload type — how the model learns what
    it can run (instead of being handed a hardcoded command list)."""
    mythic = _mythic_sdk()
    return json.dumps(await mythic.get_all_commands_for_payloadtype(client, payload_type, _COMMANDS_ATTR), default=str)


async def _issue_command(client, callback_display_id: int, command: str, parameters: str = "", timeout: int = 300) -> str:
    """Issue ONE command to a callback and wait for its output. Raw issue+wait — keeps only the anti-hang
    ceiling (the same waitfor-poller hang we hit in the gauge), nothing else."""
    mythic = _mythic_sdk()
    params = parameters if isinstance(parameters, str) else (json.dumps(parameters) if parameters else "")
    task = await mythic.issue_task(client, command_name=command, parameters=params,
                                   callback_display_id=int(callback_display_id), wait_for_complete=False)
    out = await asyncio.wait_for(
        mythic.waitfor_for_task_output(client, task_display_id=task["display_id"], timeout=timeout),
        timeout=timeout + 20)
    return out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)


async def _get_task_history(client, callback_display_id: int) -> str:
    """Full task history for a callback (the model's own past actions/output as context)."""
    mythic = _mythic_sdk()
    return json.dumps(await mythic.get_all_tasks(mythic=client, callback_display_id=int(callback_display_id)), default=str)


async def _get_task_output(client, task_display_id: int) -> str:
    """All output for a task id (base64-decoded for readability)."""
    mythic = _mythic_sdk()
    rows = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=int(task_display_id))
    out = []
    for r in (rows or []):
        rt = r.get("response_text", "")
        try:
            out.append(base64.b64decode(rt).decode(errors="replace"))
        except Exception:
            out.append(str(rt))
    return "\n".join(out)


async def _get_uploaded_files(client) -> str:
    """List operator-uploaded files already in Mythic's store (uploadable to a target)."""
    mythic = _mythic_sdk()
    rows = [row async for row in mythic.get_all_uploaded_files(mythic=client)]
    return json.dumps(rows, default=str)


async def _read_credentials(client, realm: str = "", account: str = "") -> str:
    """Read the Mythic credential store, optionally filtered by realm/account (raw — no shared cache)."""
    mythic = _mythic_sdk()
    r = await mythic.execute_custom_query(client, _CREDENTIALS_QUERY)
    creds = r.get("credential", []) or []
    rcf, acf = (realm or "").strip().casefold(), (account or "").strip().casefold()
    if rcf:
        creds = [c for c in creds if rcf in str(c.get("realm") or "").casefold()]
    if acf:
        creds = [c for c in creds if acf in str(c.get("account") or "").casefold()]
    return json.dumps(creds, default=str)


async def _add_credential(client, account: str, realm: str, credential: str, credential_type: str = "plaintext") -> str:
    """Add a credential to the Mythic store."""
    mythic = _mythic_sdk()
    if not credential:
        return "error: credential value is empty"
    return json.dumps(await mythic.create_credential(
        mythic=client, account=account, realm=realm, credential=credential,
        credential_type=credential_type), default=str)


async def _get_operations(client) -> str:
    """List Mythic operations."""
    mythic = _mythic_sdk()
    return json.dumps(await mythic.get_operations(mythic=client), default=str)


async def _create_payload(client, payload_type: str, os_name: str, c2_profile: str,
                          c2_parameters: dict | None = None, build_parameters: dict | None = None,
                          filename: str = "") -> str:
    """Build a new Mythic payload (raw create_payload)."""
    mythic = _mythic_sdk()
    return json.dumps(await mythic.create_payload(
        mythic=client, payload_type_name=payload_type, filename=filename or f"{payload_type}.bin",
        operating_system=os_name, c2_profiles=[{"c2_profile": c2_profile,
                                                "c2_profile_parameters": c2_parameters or {}}],
        build_parameters=[{"name": k, "value": v} for k, v in (build_parameters or {}).items()],
        include_all_commands=True), default=str)


async def _delete_payload(client, payload_uuid: str, confirm: bool = False) -> str:
    """Soft-delete a payload — REFUSES if it still has live callbacks (lab safety; not intelligence)."""
    mythic = _mythic_sdk()
    r = await mythic.execute_custom_query(
        client,
        _PAYLOAD_DELETE_PREFLIGHT_QUERY,
        variables={"uuid": payload_uuid},
    )
    rows = r.get("payload", []) or []
    if not rows:
        return f"error: payload {payload_uuid} not found"
    cbs = (((r.get("callback_aggregate") or {}).get("aggregate") or {}).get("count") or 0)
    if cbs:
        return f"refused: payload {payload_uuid} has {cbs} callback(s); not deleting an in-use payload"
    if not confirm:
        return f"payload {payload_uuid} has no callbacks; re-call with confirm=true to delete"
    return json.dumps(
        await mythic.execute_custom_query(
            client,
            _PAYLOAD_DELETE_MUTATION,
            variables={"uuid": payload_uuid},
        ),
        default=str,
    )


CollectionIngestor = Callable[[str, bytes], Awaitable[Any]]


def _collection_zip_valid(data: bytes) -> bool:
    """Recognize a non-empty ZIP containing at least one BloodHound JSON member."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            return any(
                not member.is_dir()
                and member.filename.casefold().endswith(".json")
                and member.file_size > 0
                for member in archive.infolist()
            )
    except (OSError, zipfile.BadZipFile):
        return False


def _task_collection_source(
    row: Any,
    *,
    callback_display_id: int | None = None,
    name_contains: str = "zip",
) -> tuple[str, str] | None:
    """Return one exact task-produced collection source, or fail closed with ``None``."""
    if not isinstance(row, dict):
        return None
    if (
        row.get("complete") is not True
        or row.get("deleted") is not False
        or row.get("is_payload") is not False
        or row.get("is_download_from_agent") is not True
    ):
        return None
    agent_file_id = str(row.get("agent_file_id") or "").strip()
    filename = str(row.get("filename_utf8") or "").strip()
    storage_path = str(row.get("path") or "").strip()
    if not agent_file_id or not filename or not storage_path or not filename.casefold().endswith(".zip"):
        return None
    needle = str(name_contains or "").strip().casefold()
    if needle and needle not in filename.casefold():
        return None
    task = row.get("task")
    if not isinstance(task, dict):
        return None
    task_id = task.get("display_id")
    command_name = str(task.get("command_name") or "").strip()
    callback = task.get("callback")
    callback_id = callback.get("display_id") if isinstance(callback, dict) else None
    if (
        not isinstance(task_id, int)
        or isinstance(task_id, bool)
        or task_id <= 0
        or not command_name
        or not isinstance(callback_id, int)
        or isinstance(callback_id, bool)
        or callback_id <= 0
    ):
        return None
    if callback_display_id is not None and callback_id != callback_display_id:
        return None
    return agent_file_id, filename


async def _resolve_collection_source(
    client: Any,
    *,
    file_uuid: str,
    callback_display_id: int | None,
    name_contains: str,
) -> tuple[str, str, str]:
    """Resolve exactly one explicit raw Mythic source to UUID, filename, and selector label."""
    mythic = _mythic_sdk()

    exact_uuid = str(file_uuid or "").strip()
    callback_supplied = callback_display_id is not None
    if bool(exact_uuid) == callback_supplied:
        raise ValueError("provide exactly one of file_uuid or callback_display_id")
    if exact_uuid:
        return exact_uuid, f"{exact_uuid}.zip", "file_uuid"
    callback_id = int(callback_display_id)
    if callback_id <= 0:
        raise ValueError("callback_display_id must be a positive integer")
    result = await mythic.execute_custom_query(
        client,
        _LATEST_COLLECTION_QUERY,
        variables={"cbid": callback_id},
    )
    rows = result.get("filemeta") if isinstance(result, dict) else None
    for row in rows or []:
        source = _task_collection_source(
            row,
            callback_display_id=callback_id,
            name_contains=name_contains,
        )
        if source is not None:
            agent_file_id, filename = source
            return agent_file_id, filename, f"callback:{callback_id}"
    raise LookupError(
        f"no completed non-deleted callback download matched name_contains={name_contains!r}"
    )


async def _ingest_collection(
    client: Any,
    *,
    file_uuid: str = "",
    callback_display_id: int | None = None,
    file_name: str = "",
    name_contains: str = "zip",
    collection_ingestor: CollectionIngestor | None = None,
) -> str:
    """Raw Mythic-download -> BloodHound-upload bridge with no Sage lifecycle treatment."""
    mythic = _mythic_sdk()

    if collection_ingestor is None:
        raise RuntimeError("BloodHound file_upload adapter is unavailable")
    resolved_uuid, source_name, resolved_by = await _resolve_collection_source(
        client,
        file_uuid=file_uuid,
        callback_display_id=callback_display_id,
        name_contains=name_contains,
    )
    blob = await mythic.download_file(mythic=client, file_uuid=resolved_uuid)
    if not _collection_zip_valid(blob):
        raise ValueError("Mythic source is not a non-empty collection ZIP containing JSON")
    safe_name = os.path.basename(str(file_name or source_name).strip().replace("\\", "/"))
    if not safe_name:
        safe_name = f"{resolved_uuid}.zip"
    if not safe_name.casefold().endswith(".zip"):
        safe_name += ".zip"
    raw_result = await collection_ingestor(safe_name, bytes(blob))
    return json.dumps(
        {
            "source": {
                "file_uuid": resolved_uuid,
                "file_name": safe_name,
                "resolved_by": resolved_by,
                "bytes": len(blob),
            },
            "bloodhound": raw_result,
        },
        default=str,
        sort_keys=True,
    )
# --- tool registry: name -> (impl, is_async, JSON-schema params, description) ------------------------

def _p(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOLS: dict[str, dict] = {
    "list_callbacks":        {"fn": _list_callbacks, "async": True, "params": _p({}, []),
                              "desc": "List all active Mythic callbacks (id, host, user, payload type)."},
    "get_payload_types":     {"fn": _get_payload_types, "async": True, "params": _p({}, []),
                              "desc": "List installed payload types (apollo, poseidon, merlin, ...)."},
    "get_payloads":          {"fn": _get_payloads, "async": True, "params": _p({}, []),
                              "desc": "List built/registered payloads."},
    "get_all_payload_info":  {"fn": _get_all_payload_info, "async": True, "params": _p({}, []),
                              "desc": "Return payload-type supported OS and build-parameter metadata."},
    "get_c2_profiles_for_payload": {
                              "fn": _get_c2_profiles_for_payload, "async": True,
                              "params": _p({"payload_type": _STR}, ["payload_type"]),
                              "desc": "Return raw C2 profile and parameter schemas for a payload type."},
    "get_callback_c2_config": {"fn": _get_callback_c2_config, "async": True,
                              "params": _p({"callback_display_id": _INT}, ["callback_display_id"]),
                              "desc": "Return configured C2 parameter values for one callback."},
    "get_payload_c2_config": {"fn": _get_payload_c2_config, "async": True,
                              "params": _p({"payload_uuid": _STR}, ["payload_uuid"]),
                              "desc": "Return configured C2 values and file metadata for one payload."},
    "get_commands":          {"fn": _get_commands, "async": True,
                              "params": _p({"payload_type": _STR}, ["payload_type"]),
                              "desc": "Enumerate a payload type's commands and full argument schema."},
    "issue_command":         {"fn": _issue_command, "async": True,
                              "params": _p({"callback_display_id": _INT, "command": _STR,
                                            "parameters": _STR}, ["callback_display_id", "command"]),
                              "desc": "Issue one command to a callback and wait for output."},
    "get_task_history":      {"fn": _get_task_history, "async": True,
                              "params": _p({"callback_display_id": _INT}, ["callback_display_id"]),
                              "desc": "Full task history for a callback."},
    "get_task_output":       {"fn": _get_task_output, "async": True,
                              "params": _p({"task_display_id": _INT}, ["task_display_id"]),
                              "desc": "All output for a task id."},
    "get_uploaded_files":    {"fn": _get_uploaded_files, "async": True, "params": _p({}, []),
                              "desc": "List files already in Mythic's store."},
    "read_credentials":      {"fn": _read_credentials, "async": True,
                              "params": _p({"realm": _STR, "account": _STR}, []),
                              "desc": "Read the Mythic credential store (optional realm/account filter)."},
    "add_credential":        {"fn": _add_credential, "async": True,
                              "params": _p({"account": _STR, "realm": _STR, "credential": _STR,
                                            "credential_type": _STR}, ["account", "realm", "credential"]),
                              "desc": "Add a credential to the Mythic store."},
    "get_operations":        {"fn": _get_operations, "async": True, "params": _p({}, []),
                              "desc": "List Mythic operations."},
    "create_payload":        {"fn": _create_payload, "async": True,
                              "params": _p({"payload_type": _STR, "os_name": _STR, "c2_profile": _STR,
                                            "c2_parameters": {"type": "object"},
                                            "build_parameters": {"type": "object"}, "filename": _STR},
                                           ["payload_type", "os_name", "c2_profile"]),
                              "desc": "Build a new Mythic payload."},
    "delete_payload":        {"fn": _delete_payload, "async": True,
                              "params": _p({"payload_uuid": _STR, "confirm": {"type": "boolean"}},
                                           ["payload_uuid"]),
                              "desc": "Soft-delete a payload (refused if it has callbacks)."},
    "ingest_collection":     {"fn": _ingest_collection, "async": True,
                              "params": _p({"file_uuid": _STR,
                                            "callback_display_id": _INT,
                                            "file_name": _STR,
                                            "name_contains": _STR}, []),
                              "desc": ("Download one explicitly selected Mythic collection and submit its "
                                       "bytes to BloodHound; provide exactly one of file_uuid or "
                                       "callback_display_id. Returns raw upload/job status.")},
}

RAW_MYTHIC_FAKE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_callbacks": {}, "get_payload_types": {}, "get_payloads": {},
    "get_all_payload_info": {}, "get_c2_profiles_for_payload": {"payload_type": "apollo"},
    "get_callback_c2_config": {"callback_display_id": 7},
    "get_payload_c2_config": {"payload_uuid": "payload-1"},
    "get_commands": {"payload_type": "apollo"},
    "issue_command": {"callback_display_id": 7, "command": "whoami", "parameters": ""},
    "get_task_history": {"callback_display_id": 7}, "get_task_output": {"task_display_id": 41},
    "get_uploaded_files": {}, "read_credentials": {},
    "add_credential": {"account": "user", "realm": "example", "credential": "value"},
    "get_operations": {},
    "create_payload": {"payload_type": "apollo", "os_name": "Windows", "c2_profile": "http"},
    "delete_payload": {"payload_uuid": "payload-1", "confirm": True},
    "ingest_collection": {"file_uuid": "collection-1"},
}


def _fake_collection_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("users.json", '{"data":[{"name":"EXAMPLE"}]}')
    return output.getvalue()


class _DeterministicFakeMythic:
    """In-memory SDK boundary used by the pre-comparison executable contract."""

    def __init__(self):
        self.calls: list[str] = []

    async def _return(self, name: str, value: Any):
        self.calls.append(name)
        return value

    async def get_all_active_callbacks(self, *_a, **_k): return await self._return("get_all_active_callbacks", [])
    async def get_all_payloads(self, *_a, **_k): return await self._return("get_all_payloads", [])
    async def get_all_commands_for_payloadtype(self, *_a, **_k): return await self._return("get_all_commands_for_payloadtype", [])
    async def issue_task(self, *_a, **_k): return await self._return("issue_task", {"display_id": 41})
    async def waitfor_for_task_output(self, *_a, **_k): return await self._return("waitfor_for_task_output", b"ok")
    async def get_all_tasks(self, *_a, **_k): return await self._return("get_all_tasks", [])
    async def get_all_task_output_by_id(self, *_a, **_k): return await self._return("get_all_task_output_by_id", [{"response_text": "b2s="}])
    async def get_payload_by_uuid(self, *_a, **_k): return await self._return("get_payload_by_uuid", {"uuid": "payload-1"})
    async def download_file(self, *_a, **_k): return await self._return("download_file", _fake_collection_zip())
    async def create_credential(self, *_a, **_k): return await self._return("create_credential", {"status": "success"})
    async def get_operations(self, *_a, **_k): return await self._return("get_operations", [])
    async def create_payload(self, *_a, **_k): return await self._return("create_payload", {"uuid": "payload-1"})

    async def get_all_uploaded_files(self, *_a, **_k):
        self.calls.append("get_all_uploaded_files")
        yield {"agent_file_id": "file-1"}

    async def execute_custom_query(self, _client, query, variables=None):
        self.calls.append("execute_custom_query")
        if query == _PAYLOAD_TYPES_QUERY: return {"payloadtype": [{"name": "apollo"}]}
        if query == _CREDENTIALS_QUERY: return {"credential": []}
        if query == _PAYLOAD_DELETE_PREFLIGHT_QUERY:
            return {"payload": [{"uuid": "payload-1"}], "callback_aggregate": {"aggregate": {"count": 0}}}
        if query == _PAYLOAD_DELETE_MUTATION: return {"updatePayload": {"status": "success", "error": ""}}
        return {"payloadtype": [], "payloadtypec2profile": [], "callback": []}


def _fake_args_valid(name: str, args: dict[str, Any]) -> bool:
    schema = TOOLS[name]["params"]
    if set(schema.get("required") or ()) - set(args):
        return False
    types = {"string": str, "integer": int, "boolean": bool, "object": dict}
    for key, value in args.items():
        expected = types.get((schema.get("properties", {}).get(key) or {}).get("type"))
        if expected is None or not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            return False
    return True


async def _execute_mythic_call(
    client: Any, call: dict[str, Any], *, collection_ingestor: CollectionIngestor | None = None,
) -> str:
    name, args = call.get("tool", ""), call.get("args", {}) or {}
    tool = TOOLS.get(name)
    if tool is None:
        return f"[unknown tool] {name!r}. Available: {sorted(TOOLS)}"
    if name == "ingest_collection":
        return await tool["fn"](client, collection_ingestor=collection_ingestor, **args)
    return await tool["fn"](client, **args)


async def raw_mythic_fake_execution_disposition() -> dict[str, object]:
    failures: dict[str, str] = {}
    fake = _DeterministicFakeMythic()
    token = _MYTHIC_SDK_OVERRIDE.set(fake)

    async def ingest(_name: str, _data: bytes) -> dict[str, str]: return {"status": "accepted"}

    try:
        for name in sorted(TOOLS):
            args = RAW_MYTHIC_FAKE_ARGUMENTS.get(name)
            if not isinstance(args, dict) or not _fake_args_valid(name, args):
                failures[name] = "fake arguments do not satisfy the runtime schema"
                continue
            try:
                result = await _execute_mythic_call(object(), {"tool": name, "args": args}, collection_ingestor=ingest)
            except Exception as exc:
                failures[name] = f"fake execution raised {type(exc).__name__}"
                continue
            if not isinstance(result, str) or not result.strip():
                failures[name] = "runtime result is not nonempty text"
    finally:
        _MYTHIC_SDK_OVERRIDE.reset(token)
    expected = set(TOOLS)
    if set(RAW_MYTHIC_TO_SHIPPING_TOOL) != expected:
        failures["shipping_mapping"] = "raw inventory and shipping mapping differ"
    return {
        "valid": not failures,
        "executed_count": len(expected) - len([name for name in failures if name in expected]),
        "raw_count": len(expected),
        "shipping_mapping_count": len(RAW_MYTHIC_TO_SHIPPING_TOOL),
        "sdk_call_count": len(fake.calls),
        "failures": failures,
    }


def shipping_visible_mythic_tool_names() -> frozenset[str]:
    """Return the source-declared tools that survive shipping prompt frontmatter."""
    try:
        from ..langgraph.prompt_loader import get_prompt_tools
    except ImportError:
        from ai.langgraph.prompt_loader import get_prompt_tools  # type: ignore
    operator = set(SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES) & set(get_prompt_tools("mythic_operator"))
    payload = set(SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES) & set(get_prompt_tools("mythic_payload"))
    return frozenset(operator | payload)


def raw_mythic_parity_disposition(
    shipping_tool_names: set[str] | frozenset[str] | None = None,
) -> dict[str, object]:
    """Prove that every raw tool is a mapped subset of the shipping Sage surface."""
    if shipping_tool_names is None:
        shipping_tool_names = shipping_visible_mythic_tool_names()
    raw = set(TOOLS)
    mapped = set(RAW_MYTHIC_TO_SHIPPING_TOOL)
    exceptions = set(RAW_MYTHIC_TOOL_EXCEPTIONS)
    if mapped & exceptions:
        return {"valid": False, "reason": "a raw tool is both mapped and excepted"}
    if raw != mapped | exceptions:
        return {
            "valid": False,
            "reason": "raw tool inventory and parity declarations differ",
            "undeclared": sorted(raw - mapped - exceptions),
            "stale": sorted((mapped | exceptions) - raw),
        }
    missing_shipping = {
        raw_name: shipping_name
        for raw_name, shipping_name in RAW_MYTHIC_TO_SHIPPING_TOOL.items()
        if shipping_name not in shipping_tool_names
    }
    if missing_shipping:
        return {
            "valid": False,
            "reason": "a mapped raw capability is absent from shipping Sage",
            "missing_shipping": missing_shipping,
        }
    if len(raw) > len(shipping_tool_names):
        return {"valid": False, "reason": "raw inventory is larger than shipping Sage"}
    return {
        "valid": True,
        "raw_count": len(raw),
        "shipping_count": len(shipping_tool_names),
        "exceptions": dict(RAW_MYTHIC_TOOL_EXCEPTIONS),
        "mapping": dict(sorted(RAW_MYTHIC_TO_SHIPPING_TOOL.items())),
    }


def raw_mythic_bidirectional_disposition() -> dict[str, object]:
    """Validate the accepted 31-row reverse audit against the current shipping and raw inventories."""
    shipping = set(shipping_visible_mythic_tool_names())
    classified = set(SHIPPING_MYTHIC_REVERSE_DISPOSITIONS)
    raw = set(TOOLS)
    failures: dict[str, str] = {}
    if classified != shipping:
        failures["inventory"] = (
            f"missing={sorted(shipping - classified)} extra={sorted(classified - shipping)}"
        )
    counts: dict[str, int] = {}
    allowed = {"ALREADY_DIRECT", "ALREADY_COMPOSABLE", "ADD_RAW", "DO_NOT_ADD_TREATMENT"}
    for shipping_name, (decision, coverage) in SHIPPING_MYTHIC_REVERSE_DISPOSITIONS.items():
        counts[decision] = counts.get(decision, 0) + 1
        if decision not in allowed:
            failures[shipping_name] = f"unknown decision {decision}"
        elif decision == "DO_NOT_ADD_TREATMENT":
            if coverage:
                failures[shipping_name] = "treatment exclusion declares raw coverage"
        elif not coverage or not set(coverage) <= raw:
            failures[shipping_name] = f"missing raw coverage {sorted(set(coverage) - raw)}"
    return {
        "valid": not failures,
        "shipping_count": len(shipping),
        "classified_count": len(classified),
        "counts": dict(sorted(counts.items())),
        "failures": failures,
    }
def raw_mythic_sdk_disposition(mythic_module: Any = None) -> dict[str, object]:
    """Validate exact installed Mythic SDK call names, parameter names, and async kinds."""
    if mythic_module is None:
        from mythic import mythic as mythic_module
    failures: dict[str, str] = {}
    for name, (expected_kind, required_parameters) in RAW_MYTHIC_SDK_CONTRACT.items():
        fn = getattr(mythic_module, name, None)
        if fn is None:
            failures[name] = "callable is absent"
            continue
        actual_kind = (
            "async_generator" if inspect.isasyncgenfunction(fn)
            else "coroutine" if inspect.iscoroutinefunction(fn)
            else "synchronous"
        )
        if actual_kind != expected_kind:
            failures[name] = f"expected {expected_kind}, observed {actual_kind}"
            continue
        parameters = set(inspect.signature(fn).parameters)
        missing = set(required_parameters) - parameters
        if missing:
            failures[name] = f"missing parameters {sorted(missing)}"
    return {
        "valid": not failures,
        "checked": len(RAW_MYTHIC_SDK_CONTRACT),
        "failures": failures,
    }


async def raw_mythic_graphql_disposition(client: Any, *, timeout: int = 30) -> dict[str, object]:
    """Validate the exact runtime GraphQL documents against the live schema without mutations."""
    from graphql import build_client_schema, get_introspection_query, parse, validate
    from mythic import mythic

    introspection = await asyncio.wait_for(
        mythic.execute_custom_query(
            client,
            get_introspection_query(descriptions=False),
            variables={},
        ),
        timeout=timeout,
    )
    schema = build_client_schema(introspection)
    failures: dict[str, list[str]] = {}
    for name, document in RAW_MYTHIC_GRAPHQL_DOCUMENTS.items():
        errors = validate(schema, parse(document))
        if errors:
            failures[name] = [error.message for error in errors]
    return {
        "valid": not failures,
        "checked": len(RAW_MYTHIC_GRAPHQL_DOCUMENTS),
        "failures": failures,
    }


async def raw_mythic_download_disposition(client: Any, *, timeout: int = 30) -> dict[str, object]:
    """Exercise the official SDK on one task-produced collection before reset can start."""
    from mythic import mythic

    try:
        result = await asyncio.wait_for(
            mythic.execute_custom_query(client, _DOWNLOAD_PROBE_QUERY, variables={}),
            timeout=timeout,
        )
        rows = result.get("filemeta") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return {"valid": False, "reason": "collection download preflight omitted the file list"}
        source = next(
            (
                candidate
                for row in rows
                if (candidate := _task_collection_source(row)) is not None
            ),
            None,
        )
        if source is None:
            return {
                "valid": False,
                "reason": "no valid task-produced collection file is available for download preflight",
            }
        file_uuid, _ = source
        blob = await asyncio.wait_for(
            mythic.download_file(mythic=client, file_uuid=file_uuid),
            timeout=timeout,
        )
        if not _collection_zip_valid(blob):
            return {
                "valid": False,
                "reason": "download preflight did not return a valid collection ZIP",
            }
        return {"valid": True, "checked": 1, "bytes_received": len(blob)}
    except Exception as exc:
        return {
            "valid": False,
            "reason": f"task-produced collection download preflight failed ({type(exc).__name__})",
        }


async def raw_mythic_surface_preflight(client: Any, *, timeout: int = 30) -> tuple[bool, str]:
    """Fail-cheap structural preflight for the runtime raw Mythic surface."""
    parity = raw_mythic_parity_disposition()
    bidirectional = raw_mythic_bidirectional_disposition()
    sdk = raw_mythic_sdk_disposition()
    graphql = await raw_mythic_graphql_disposition(client, timeout=timeout)
    fake_execution = await raw_mythic_fake_execution_disposition()
    failures = [
        name for name, disposition in (
            ("parity", parity), ("bidirectional", bidirectional), ("sdk", sdk),
            ("graphql", graphql), ("fake-execution", fake_execution),
        )
        if disposition.get("valid") is not True
    ]
    if failures:
        return False, f"raw Mythic surface failed {', '.join(failures)} validation"
    return True, (
        f"raw Mythic surface validated: {parity['raw_count']} raw <= "
        f"{parity['shipping_count']} shipping tools; {sdk['checked']} SDK calls; "
        f"{graphql['checked']} exact GraphQL documents; "
        f"{fake_execution['executed_count']} fake-boundary tool executions"
    )


def bare_tool_specs() -> list[dict]:
    """OpenAI-function tool schemas for the stripped Mythic toolset (bind to the bare model's LLM)."""
    return [{"type": "function",
             "function": {"name": name, "description": t["desc"], "parameters": t["params"]}}
            for name, t in TOOLS.items()]


def make_mythic_dispatcher(
    client: Any,
    *,
    collection_ingestor: CollectionIngestor | None = None,
) -> Callable[[dict], str]:
    """A bare-runner tool_executor: dispatch {"tool": name, "args": {...}} to the stripped impl.
    Mirrors live_seams.make_tool_executor's reuse-client-across-asyncio.run pattern."""
    def tool_executor(call: dict) -> str:
        try:
            return asyncio.run(_execute_mythic_call(
                client, call, collection_ingestor=collection_ingestor,
            ))
        except Exception as exc:
            return f"[tool error] {call.get('tool', '')}: {type(exc).__name__}: {exc}"

    return tool_executor
