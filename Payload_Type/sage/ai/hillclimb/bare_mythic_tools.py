"""Stripped Mythic toolset for the BARE model — the fair baseline for "is Sage's harness better?".

These are clean, Sage-FREE wrappers over the raw Mythic SDK, mirroring exactly the tools Sage's
Mythic_Operator/Mythic_Payload agents get via `MythicTools.get_tools([...])` — but with NONE of the
secret sauce (the audit's EXCLUDE/STRIP findings). So the bare model interacts with Mythic the SAME WAY
Sage does (enumerate payloads -> enumerate a payload's commands+args -> issue a task -> read output),
discovering commands at runtime instead of being handed a hardcoded command list. The only difference
left between bare and harness is then Sage's scaffolding/intelligence itself.

STRIPPED (per audit): no engagement-state hook, no dcsync/param normalization, no circuit-breaker,
no schema-repair, no recon-reread guards, no liveness `_compute_liveness` engine, no footprint/OPSEC
annotation, no command-schema cache, no TTP knowledge, no capability/STRIPS planner, no BloodHound
ingest reconciliation. EXCLUDED entirely (they ARE the harness): liveness verdicts, TTP tools,
execute_capability/build_capability_commands, ingest_collection, TTP-pinned download_tool.

LIVE factories — NOT unit-tested against a real Mythic; validated on the lab on first bare run. The
pure parts (tool schemas, dispatch routing, tools-folder discovery) ARE unit-tested.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

# Sage's operator drop zone — the same folder ensure_tool_uploaded scans (ttp_library.TOOLS_DIR).
TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

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
    from mythic import mythic
    return json.dumps(await mythic.get_all_active_callbacks(client), default=str)


async def _get_payload_types(client) -> str:
    """List the payload types installed on this Mythic (apollo, poseidon, merlin, ...)."""
    from mythic import mythic
    r = await mythic.execute_custom_query(client, "query P { payloadtype { name } }")
    return json.dumps([p["name"] for p in (r.get("payloadtype") or [])])


async def _get_payloads(client) -> str:
    """List the built/registered payloads in Mythic."""
    from mythic import mythic
    return json.dumps(await mythic.get_all_payloads(client), default=str)


async def _get_commands(client, payload_type: str) -> str:
    """Enumerate every command + full argument schema for a payload type — how the model learns what
    it can run (instead of being handed a hardcoded command list)."""
    from mythic import mythic
    return json.dumps(await mythic.get_all_commands_for_payloadtype(client, payload_type, _COMMANDS_ATTR), default=str)


async def _issue_command(client, callback_display_id: int, command: str, parameters: str = "", timeout: int = 300) -> str:
    """Issue ONE command to a callback and wait for its output. Raw issue+wait — keeps only the anti-hang
    ceiling (the same waitfor-poller hang we hit in the gauge), nothing else."""
    from mythic import mythic
    params = parameters if isinstance(parameters, str) else (json.dumps(parameters) if parameters else "")
    task = await mythic.issue_task(client, command_name=command, parameters=params,
                                   callback_display_id=int(callback_display_id), wait_for_complete=False)
    out = await asyncio.wait_for(
        mythic.waitfor_for_task_output(client, task_display_id=task["display_id"], timeout=timeout),
        timeout=timeout + 20)
    return out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)


async def _get_task_history(client, callback_display_id: int) -> str:
    """Full task history for a callback (the model's own past actions/output as context)."""
    from mythic import mythic
    return json.dumps(await mythic.get_all_tasks(mythic=client, callback_display_id=int(callback_display_id)), default=str)


async def _get_task_output(client, task_display_id: int) -> str:
    """All output for a task id (base64-decoded for readability)."""
    from mythic import mythic
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
    from mythic import mythic
    return json.dumps(await mythic.get_all_uploaded_files(mythic=client), default=str)


async def _download_file(client, file_uuid: str) -> str:
    """Download a Mythic file by UUID; returns base64 content."""
    from mythic import mythic
    content = await mythic.download_file(mythic=client, file_uuid=file_uuid)
    return base64.b64encode(content).decode() if isinstance(content, (bytes, bytearray)) else str(content)


async def _read_credentials(client, realm: str = "", account: str = "") -> str:
    """Read the Mythic credential store, optionally filtered by realm/account (raw — no shared cache)."""
    from mythic import mythic
    r = await mythic.execute_custom_query(
        client, "query C { credential { account realm credential_type credential metadata } }")
    creds = r.get("credential", []) or []
    rcf, acf = (realm or "").strip().casefold(), (account or "").strip().casefold()
    if rcf:
        creds = [c for c in creds if rcf in str(c.get("realm") or "").casefold()]
    if acf:
        creds = [c for c in creds if acf in str(c.get("account") or "").casefold()]
    return json.dumps(creds, default=str)


async def _add_credential(client, account: str, realm: str, credential: str, credential_type: str = "plaintext") -> str:
    """Add a credential to the Mythic store."""
    from mythic import mythic
    if not credential:
        return "error: credential value is empty"
    return json.dumps(await mythic.create_credential(
        mythic=client, account=account, realm=realm, credential=credential,
        credential_type=credential_type), default=str)


async def _get_operations(client) -> str:
    """List Mythic operations."""
    from mythic import mythic
    return json.dumps(await mythic.get_operations(mythic=client), default=str)


def list_tools_folder_local() -> list[str]:
    """Pure: the binaries available in Sage's tools/ drop zone (no recursion). Unit-testable."""
    if not TOOLS_DIR.is_dir():
        return []
    return sorted(p.name for p in TOOLS_DIR.iterdir() if p.is_file() and not p.name.startswith("."))


async def _list_tools_folder(client) -> str:
    """List local tool binaries in the tools/ drop zone that can be registered + uploaded."""
    return json.dumps(list_tools_folder_local())


async def _register_tool(client, filename: str) -> str:
    """Register a local tools/ binary into Mythic's file store (raw register_file; no dedup/by-name sauce).
    Returns the Mythic file UUID, which `upload_file_to_target` then pushes to a callback."""
    from mythic import mythic
    p = TOOLS_DIR / filename
    if not p.is_file():
        return f"error: {filename!r} not found in {TOOLS_DIR}. Available: {list_tools_folder_local()}"
    return json.dumps(await mythic.register_file(client, filename=p.name, contents=p.read_bytes()), default=str)


async def _upload_file_to_target(client, callback_display_id: int, file_uuid: str, remote_path: str = "") -> str:
    """Push a Mythic-stored file (by UUID) to a target via the callback's upload command (raw task)."""
    args = {"file": file_uuid}
    if remote_path:
        args["remote_path"] = remote_path
    return await _issue_command(client, callback_display_id, "upload", json.dumps(args))


async def _create_payload(client, payload_type: str, os_name: str, c2_profile: str,
                          c2_parameters: dict | None = None, build_parameters: dict | None = None,
                          filename: str = "") -> str:
    """Build a new Mythic payload (raw create_payload)."""
    from mythic import mythic
    return json.dumps(await mythic.create_payload(
        mythic=client, payload_type_name=payload_type, filename=filename or f"{payload_type}.bin",
        operating_system=os_name, c2_profiles=[{"c2_profile": c2_profile,
                                                "c2_profile_parameters": c2_parameters or {}}],
        build_parameters=[{"name": k, "value": v} for k, v in (build_parameters or {}).items()],
        include_all_commands=True), default=str)


async def _delete_payload(client, payload_uuid: str, confirm: bool = False) -> str:
    """Soft-delete a payload — REFUSES if it still has live callbacks (lab safety; not intelligence)."""
    from mythic import mythic
    q = ('query Pre($u:String!){payload(where:{uuid:{_eq:$u}}){id callbacks_aggregate{aggregate{count}}}}')
    r = await mythic.execute_custom_query(client, q, variables={"u": payload_uuid})
    rows = r.get("payload", []) or []
    if not rows:
        return f"error: payload {payload_uuid} not found"
    cbs = (((rows[0].get("callbacks_aggregate") or {}).get("aggregate") or {}).get("count") or 0)
    if cbs:
        return f"refused: payload {payload_uuid} has {cbs} callback(s); not deleting an in-use payload"
    if not confirm:
        return f"payload {payload_uuid} has no callbacks; re-call with confirm=true to delete"
    m = ('mutation Del($id:Int!){updatePayload(payload_id:$id,_set:{deleted:true}){id deleted}}')
    return json.dumps(await mythic.execute_custom_query(client, m, variables={"id": rows[0]["id"]}), default=str)


def _sandbox_exec(client, language: str = "sh", code: str = "", timeout: int = 60) -> str:
    """Run untrusted code in an isolated, network-disabled Docker container (Sage's sage-sandbox image)."""
    image = "sage-sandbox:latest"
    inner = ["python3", "-c", code] if language == "python" else ["sh", "-c", code]
    docker = ["docker", "run", "--rm", "--network=none", "--memory=512m", "--pids-limit=128",
              "--read-only", "--tmpfs", "/tmp:size=64m", "-u", "10001", "--cap-drop=ALL",
              "--security-opt=no-new-privileges", image] + inner
    try:
        p = subprocess.run(docker, capture_output=True, text=True, timeout=min(max(int(timeout), 1), 120))
        return f"exit={p.returncode}\nSTDOUT:\n{p.stdout[:20000]}\nSTDERR:\n{p.stderr[:8000]}"
    except Exception as e:
        return f"[sandbox error] {type(e).__name__}: {e}"


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
    "download_file":         {"fn": _download_file, "async": True,
                              "params": _p({"file_uuid": _STR}, ["file_uuid"]),
                              "desc": "Download a Mythic file by UUID (base64)."},
    "read_credentials":      {"fn": _read_credentials, "async": True,
                              "params": _p({"realm": _STR, "account": _STR}, []),
                              "desc": "Read the Mythic credential store (optional realm/account filter)."},
    "add_credential":        {"fn": _add_credential, "async": True,
                              "params": _p({"account": _STR, "realm": _STR, "credential": _STR,
                                            "credential_type": _STR}, ["account", "realm", "credential"]),
                              "desc": "Add a credential to the Mythic store."},
    "get_operations":        {"fn": _get_operations, "async": True, "params": _p({}, []),
                              "desc": "List Mythic operations."},
    "list_tools_folder":     {"fn": _list_tools_folder, "async": True, "params": _p({}, []),
                              "desc": "List local tool binaries in the tools/ drop zone."},
    "register_tool":         {"fn": _register_tool, "async": True,
                              "params": _p({"filename": _STR}, ["filename"]),
                              "desc": "Register a tools/ binary into Mythic's store; returns its file UUID."},
    "upload_file_to_target": {"fn": _upload_file_to_target, "async": True,
                              "params": _p({"callback_display_id": _INT, "file_uuid": _STR,
                                            "remote_path": _STR}, ["callback_display_id", "file_uuid"]),
                              "desc": "Upload a Mythic-stored file (by UUID) to a target via the callback."},
    "create_payload":        {"fn": _create_payload, "async": True,
                              "params": _p({"payload_type": _STR, "os_name": _STR, "c2_profile": _STR,
                                            "c2_parameters": {"type": "object"},
                                            "build_parameters": {"type": "object"}, "filename": _STR},
                                           ["payload_type", "os_name", "c2_profile"]),
                              "desc": "Build a new Mythic payload."},
    "download_payload":      {"fn": _download_file, "async": True,  # placeholder routed below
                              "params": _p({"payload_uuid": _STR}, ["payload_uuid"]),
                              "desc": "Download a built payload's file reference for reuse."},
    "delete_payload":        {"fn": _delete_payload, "async": True,
                              "params": _p({"payload_uuid": _STR, "confirm": {"type": "boolean"}},
                                           ["payload_uuid"]),
                              "desc": "Soft-delete a payload (refused if it has callbacks)."},
    "sandbox_exec":          {"fn": _sandbox_exec, "async": False,
                              "params": _p({"language": _STR, "code": _STR, "timeout": _INT}, ["code"]),
                              "desc": "Run untrusted code in an isolated, network-disabled container."},
}


def bare_tool_specs() -> list[dict]:
    """OpenAI-function tool schemas for the stripped Mythic toolset (bind to the bare model's LLM)."""
    return [{"type": "function",
             "function": {"name": name, "description": t["desc"], "parameters": t["params"]}}
            for name, t in TOOLS.items()]


def make_mythic_dispatcher(client: Any) -> Callable[[dict], str]:
    """A bare-runner tool_executor: dispatch {"tool": name, "args": {...}} to the stripped impl.
    Mirrors live_seams.make_tool_executor's reuse-client-across-asyncio.run pattern."""
    async def _download_payload_impl(c, payload_uuid: str) -> str:
        from mythic import mythic
        meta = await mythic.get_payload_by_uuid(mythic=c, payload_uuid=payload_uuid)
        content = await mythic.download_payload(c, payload_uuid=payload_uuid)
        return json.dumps({"payload": meta,
                           "bytes": len(content) if isinstance(content, (bytes, bytearray)) else None}, default=str)

    def tool_executor(call: dict) -> str:
        name = call.get("tool", "")
        args = call.get("args", {}) or {}
        t = TOOLS.get(name)
        if t is None:
            return f"[unknown tool] {name!r}. Available: {sorted(TOOLS)}"
        try:
            if name == "download_payload":
                return asyncio.run(_download_payload_impl(client, **args))
            if t["async"]:
                return asyncio.run(t["fn"](client, **args))
            return t["fn"](client, **args)
        except Exception as exc:
            return f"[tool error] {name}: {type(exc).__name__}: {exc}"

    return tool_executor
