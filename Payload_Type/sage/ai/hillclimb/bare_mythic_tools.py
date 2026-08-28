"""Stripped Mythic toolset for the BARE model — the fair baseline for "is Sage's harness better?".

These are Sage-free wrappers over the raw Mythic SDK. The naive model can enumerate callbacks,
payloads, commands, schemas, tasks, operation files, credentials, and payloads, then issue Mythic
commands itself. It receives no Sage-local tool inventory, registration helper, upload shortcut,
sandbox, TTP knowledge, planner, or agent topology.

STRIPPED (per audit): no engagement-state hook, no dcsync/param normalization, no circuit-breaker,
no schema-repair, no recon-reread guards, no liveness `_compute_liveness` engine, no footprint/OPSEC
annotation, no command-schema cache, no TTP knowledge, no capability/STRIPS planner, no BloodHound
ingest reconciliation. EXCLUDED entirely (they ARE the harness): liveness verdicts, TTP tools,
execute_capability/build_capability_commands, ingest_collection, TTP-pinned download_tool.

The exact runtime GraphQL documents and installed-SDK call contracts are preflighted before a Gate
1P reset. Harmless read wrappers are also exercised by the bounded operator audit.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable

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

BARE_TOOL_PROFILE = "naive-raw-mythic-bloodhound-v1"

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

RAW_MYTHIC_GRAPHQL_DOCUMENTS = {
    "get_payload_types": _PAYLOAD_TYPES_QUERY,
    "read_credentials": _CREDENTIALS_QUERY,
    "delete_payload_preflight": _PAYLOAD_DELETE_PREFLIGHT_QUERY,
    "delete_payload_mutation": _PAYLOAD_DELETE_MUTATION,
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
}
RAW_MYTHIC_TOOL_EXCEPTIONS: dict[str, str] = {}

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
    r = await mythic.execute_custom_query(client, _PAYLOAD_TYPES_QUERY)
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
    rows = [row async for row in mythic.get_all_uploaded_files(mythic=client)]
    return json.dumps(rows, default=str)


async def _read_credentials(client, realm: str = "", account: str = "") -> str:
    """Read the Mythic credential store, optionally filtered by realm/account (raw — no shared cache)."""
    from mythic import mythic
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


async def raw_mythic_surface_preflight(client: Any, *, timeout: int = 30) -> tuple[bool, str]:
    """Fail-cheap parity, SDK, and exact-live-schema preflight for the runtime raw surface."""
    parity = raw_mythic_parity_disposition()
    sdk = raw_mythic_sdk_disposition()
    graphql = await raw_mythic_graphql_disposition(client, timeout=timeout)
    failures = [
        name for name, disposition in (("parity", parity), ("sdk", sdk), ("graphql", graphql))
        if disposition.get("valid") is not True
    ]
    if failures:
        return False, f"raw Mythic surface failed {', '.join(failures)} validation"
    return True, (
        f"raw Mythic surface validated: {parity['raw_count']} raw <= "
        f"{parity['shipping_count']} shipping tools; {sdk['checked']} SDK calls; "
        f"{graphql['checked']} exact GraphQL documents"
    )


def bare_tool_specs() -> list[dict]:
    """OpenAI-function tool schemas for the stripped Mythic toolset (bind to the bare model's LLM)."""
    return [{"type": "function",
             "function": {"name": name, "description": t["desc"], "parameters": t["params"]}}
            for name, t in TOOLS.items()]


def make_mythic_dispatcher(client: Any) -> Callable[[dict], str]:
    """A bare-runner tool_executor: dispatch {"tool": name, "args": {...}} to the stripped impl.
    Mirrors live_seams.make_tool_executor's reuse-client-across-asyncio.run pattern."""
    def tool_executor(call: dict) -> str:
        name = call.get("tool", "")
        args = call.get("args", {}) or {}
        t = TOOLS.get(name)
        if t is None:
            return f"[unknown tool] {name!r}. Available: {sorted(TOOLS)}"
        try:
            if t["async"]:
                return asyncio.run(t["fn"](client, **args))
            return t["fn"](client, **args)
        except Exception as exc:
            return f"[tool error] {name}: {type(exc).__name__}: {exc}"

    return tool_executor
