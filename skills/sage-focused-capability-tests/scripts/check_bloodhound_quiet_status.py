#!/usr/bin/env python3
"""Live check for BloodHound availability shown as per-turn status and explained only when it matters.

Steps share one run file (JSON). ``readiness`` reads, plus one readiness channel holding a ``/bloodhound`` status read
and a one-line prompt. ``run`` is authorized live activity: it may create one API key owned by the logged-in operator,
creates channels A, B, C and E, sends the prompts below, changes channel A's model once, and stops and restarts the
BloodHound CE web container once (its data lives in volumes). ``verify`` reads Mythic's own rows and the live Phoenix
trace and applies the pass rules once.

BloodHound token values are read from the BloodHound MCP server's own .env (``--bloodhound-mcp-env``) into channel C's
and E's settings. They are never printed, logged or written to the run file. Channel B uses placeholder tokens.

  readiness --run-file F --route-file R --phoenix-db P --bloodhound-mcp-env M --bloodhound-url U --ce-container N
  run       (same) --authorized-by TEXT
  verify    --run-file F --phoenix-db P

The route file names the direct OpenAI endpoint, model (A, Daybreak Red), model_b (the lane model, Daybreak off) and a
safety identifier; never a key.

Pass rules, written before the run and read once. "Guard row" is the ``subagent_final:<delegation_id>`` row from
BloodHound in that request; "delegates" means a Phoenix LLM span in that request produced ``transfer_to_BloodHound``.
  L1 Channel A, no BloodHound settings: "What is 2+2?" on model A, then a Model change to B and the same prompt, then
     once more: no row with a ``bloodhound_`` response key; the chip reads not configured after each.
  L2 A: the graph prompt: the Supervisor delegates; the not-configured guard row; zero BloodHound tool calls.
  L3 Channel B, URL http://127.0.0.1:9 with placeholder tokens: two plain prompts, then the graph prompt: no
     ``bloodhound_`` rows; the chip reads unavailable after each, and each tooltip names "Failed to connect to
     BloodHound MCP"; the Supervisor delegates; the guard row contains "BloodHound is configured but not connected",
     "did not read any graph data" and "Failed to connect to BloodHound MCP", and not the setup text. (The deployed
     MCP checks its credentials at startup, so a wrong URL fails the server's start; readiness confirms that check.)
  L4 Channel C, real URL and tokens: ``/bloodhound force``, then a plain prompt: the reply reports a connection verified
     by a live domain read and ingest Ready; no ``bloodhound_`` row; the chip reads connected.
  L5 Stop the CE web container; C: two plain prompts; fresh channel E with C's settings: one plain prompt; C: the graph
     prompt: C gets exactly one loss notice; E gets none; C's and E's chips read unavailable; the Supervisor delegates;
     C's guard row says BloodHound CE did not answer.
  L6 Start CE and wait for its API; C: one plain prompt: no ``bloodhound_`` row; the chip reads connected.
  Every request ends terminal, with zero target tasks. Ceilings: 60 provider calls, 1,048,576 tokens, 30 minutes.
  L2, L3 and L5 exercise the model seam (the Supervisor routing a BloodHound request to the BloodHound agent).
Observations, not pass criteria: whether any closing implies the graph was read; per-request latency on unavailable
turns; chip tooltips.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sage_openai_check_for_bloodhound", _HERE / "check_openai_responses.py")
cor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cor
_spec.loader.exec_module(cor)  # type: ignore[union-attr]
nc = cor.nc
REPO_ROOT = cor.REPO_ROOT

_surface_spec = importlib.util.spec_from_file_location(
    "sage_bloodhound_tool_surface", REPO_ROOT / "Payload_Type/sage/ai/comparison_tool_surface.py")
_surface = importlib.util.module_from_spec(_surface_spec)
_surface_spec.loader.exec_module(_surface)  # type: ignore[union-attr]
BLOODHOUND_TOOL_NAMES = frozenset(_surface.SHIPPING_BLOODHOUND_TOOL_NAMES) | {"file_upload", "cypher_query"}

BRANCH = "feature/bloodhound-quiet-status"
PLAIN = "What is 2+2?"
GRAPH = "Use BloodHound to list the domains in the graph."
READINESS_PROMPT = "Reply with the single word OK."
PLACEHOLDER_URL = "http://127.0.0.1:9"
PLACEHOLDER_TOKENS = {"BLOODHOUND_TOKEN_ID": "placeholder-token-id", "BLOODHOUND_TOKEN_KEY": "placeholder-token-key"}
CEILINGS = {"provider_calls": 60, "total_tokens": 1_048_576, "minutes": 30, "target_tasks": 0}
MAX_STEPS = 6
TERMINAL = cor.TERMINAL
KEY_NAME = "Sage BloodHound status live check"
REQUIRED_KEY_SCOPES = {"apitoken.write", "chat-ai.write", "callback.read"}
NOT_SET_UP = "connect not attempted: required credentials are unset"
GUARD_NOT_CONFIGURED = "BloodHound is not configured for this chat"
GUARD_NO_READ = "did not read any graph data"
CE_DID_NOT_ANSWER = "BloodHound CE did not answer"
GUARD_CONFIGURED_NOT_CONNECTED = "BloodHound is configured but not connected"
CONNECT_FAILED = "Failed to connect to BloodHound MCP"
SAGE_CONTAINER = "sage"
MCP_MAIN = "/opt/bloodhound_mcp/main.py"
CHANGED_FILES = (
    "Payload_Type/sage/ai/bloodhound_config.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/sage_chat/metadata.py",
    "Payload_Type/sage/sage_chat/service.py",
)
OFFLINE_TESTS = ("tests/test_bloodhound_turn_state.py", "tests/test_bloodhound_degraded_chat_notice.py")
MESSAGES_QUERY = """
query SageBloodHoundCheckMessages($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) {
    id author_type message chat_response_key sender_display_name status
  }
  chat_request_by_pk(id: $rid) { id status }
}
"""
CHANNEL_QUERY = "query SageBloodHoundCheckChannel($id: Int!) { chat_channel_by_pk(id: $id) { id ai_metadata } }"
OPERATOR_QUERY = "query SageBloodHoundCheckMe($name: String!) { operator(where: {username: {_eq: $name}}) { id } }"
KEYS_QUERY = """
query SageBloodHoundCheckKeys($operatorId: Int!) {
  apitokens(where: {operator_id: {_eq: $operatorId}, active: {_eq: true}, deleted: {_eq: false},
                    token_type: {_eq: "api"}}, order_by: {id: asc}) { id scopes }
}
"""


# --------------------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------------------

def read_route(path: Path) -> dict[str, str]:
    values = cor.read_route(path)
    if not values.get("model_b") or values["model_b"] == values["model"]:
        raise SystemExit(f"{path} needs model_b, different from model")
    return values


def read_mcp_tokens(path: Path) -> dict[str, str]:
    """The two token values from the MCP server's own .env. Returned to the caller only; never printed or stored."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            key = key.removeprefix("export ").strip()
            if key in ("BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY"):
                values[key] = value.strip().strip("\"'")
    return {key: value for key, value in values.items() if value}


def channel_config(route: dict[str, str], *, lane: bool, bloodhound: dict[str, str] | None = None) -> dict[str, Any]:
    config = {"provider": "openai", "model": route["model_b"] if lane else route["model"],
              "API_ENDPOINT": route["API_ENDPOINT"], "mode": "conversation", "autonomous_solve": False,
              "max_steps": MAX_STEPS, "SAGE_OPENAI_API": "responses",
              "SAGE_OPENAI_ACCESS_PROGRAM": "standard" if lane else "daybreak_red",
              "SAGE_OPENAI_SAFETY_IDENTIFIER": route["SAGE_OPENAI_SAFETY_IDENTIFIER"], **(bloodhound or {})}
    return {"config": config, "channel_metadata_display": {"display": nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


def _identity() -> dict[str, Any]:
    import hashlib

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "head": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "Payload_Type/sage", "skills")),
            "files": {path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() for path in CHANGED_FILES}}


def _head_committed_at() -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%cI", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _parity() -> dict[str, Any]:
    command = [str(REPO_ROOT / ".venv/bin/python"), str(REPO_ROOT / "skills/sage-goad-reset/scripts/sage_deployment.py"),
               "--mode", "container", "parity"]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT)
    return {"returncode": completed.returncode, "tail": completed.stdout.strip().splitlines()[-3:]}


def _offline_tests() -> dict[str, Any]:
    completed = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:logging",
                                *OFFLINE_TESTS], capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type/sage")
    return {"returncode": completed.returncode, "summary": completed.stdout.strip().splitlines()[-1:]}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def mcp_startup_check() -> bool:
    """The deployed BloodHound MCP validates its credentials before it serves, so L3's wrong URL fails its start."""
    completed = _docker("exec", SAGE_CONTAINER, "grep", "-c", "validate_credentials", MCP_MAIN)
    count = completed.stdout.strip()
    return completed.returncode == 0 and count.isdigit() and int(count) >= 1


def sage_restart(head_committed_at: str) -> dict[str, Any]:
    """The Sage container started after HEAD was committed, so its process holds no earlier attempt's session or
    failed-credential cache. Read-only: ``docker inspect``."""
    from datetime import datetime

    completed = _docker("inspect", "-f", "{{.State.StartedAt}}", SAGE_CONTAINER)
    started_at = completed.stdout.strip() if completed.returncode == 0 else ""
    try:
        restarted = datetime.fromisoformat(started_at) > datetime.fromisoformat(head_committed_at)
    except ValueError:
        restarted = False
    return {"sage_started_at": started_at, "head_committed_at": head_committed_at, "sage_restarted": restarted}


def ce_running(name: str) -> bool:
    completed = _docker("inspect", "-f", "{{.State.Running}}", name)
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def ce_api_answers(url: str) -> int:
    """HTTP status of BloodHound CE's version endpoint (401 also means the API is up), or 0."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


def wait_for_ce(url: str, seconds: float = 240) -> float:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if ce_api_answers(url) in (200, 401):
            return round(time.monotonic() - started, 1)
        time.sleep(3)
    raise SystemExit("BloodHound CE's API did not come back")


# --------------------------------------------------------------------------------------------------
# Phoenix (read-only, in place)
# --------------------------------------------------------------------------------------------------

def produced_tool_names(db: Path, after: int, until: int) -> list[str]:
    """Function names of tool calls produced by LLM spans in (after, until]."""
    query = "SELECT attributes FROM spans WHERE id > ? AND id <= ? AND span_kind = 'LLM' ORDER BY id"
    with cor._phoenix(db) as conn:
        rows = conn.execute(query, (after, until)).fetchall()
    names: list[str] = []
    for (attributes,) in rows:
        try:
            attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
        except ValueError:
            continue
        llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
        for path, value in cor._walk(llm.get("output_messages") or []):
            if isinstance(value, str) and path.endswith("function.name"):
                names.append(value)
    return names


# --------------------------------------------------------------------------------------------------
# Pure rules (tested offline)
# --------------------------------------------------------------------------------------------------

def over_ceiling(spans: list[dict[str, Any]], elapsed_seconds: float) -> str:
    if len(spans) > CEILINGS["provider_calls"]:
        return "provider call ceiling"
    if sum(s["prompt_tokens"] + s["completion_tokens"] for s in spans) > CEILINGS["total_tokens"]:
        return "token ceiling"
    if elapsed_seconds > CEILINGS["minutes"] * 60:
        return "wall-time ceiling"
    return ""


def bloodhound_rows(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in request.get("messages", []) if str(m.get("chat_response_key") or "").startswith("bloodhound_")]


def guard_rows(request: dict[str, Any]) -> list[str]:
    return [str(m.get("message") or "") for m in request.get("messages", [])
            if str(m.get("chat_response_key") or "").startswith("subagent_final:")
            and str(m.get("sender_display_name") or "") == "BloodHound"]


def text_of(request: dict[str, Any]) -> str:
    return "\n".join(str(m.get("message") or "") for m in request.get("messages", []))


def usable_key(keys: list[dict[str, Any]]) -> dict[str, Any] | None:
    for key in keys:
        scopes = set(key.get("scopes") or [])
        if "*" in scopes or REQUIRED_KEY_SCOPES <= scopes:
            return key
    return None


def bloodhound_chip(ai_metadata: Any) -> dict[str, Any]:
    items = (((ai_metadata or {}).get("channel_metadata") or {}).get("items") or []) if isinstance(ai_metadata, dict) else []
    chip = next((item for item in items if isinstance(item, dict) and item.get("key") == "bloodhound"), None) or {}
    return {"value": chip.get("value"), "color": chip.get("color"), "tooltip": chip.get("tooltip")}


def criteria_for(run: dict[str, Any], rows: dict[str, list[dict[str, Any]]],
                 tools: dict[str, list[list[str]]]) -> dict[str, Any]:
    """Each step's rule. ``rows[step]`` and ``tools[step]`` are per request, in the order the run sent them."""
    sent = {step: info["requests"] for step, info in (run.get("steps") or {}).items()}

    def chips(step: str, channel: str | None = None) -> list[Any]:
        return [r.get("chip", {}).get("value") for r in sent.get(step, []) if channel in (None, r.get("channel"))]

    def tooltips(step: str) -> list[str]:
        return [str(r.get("chip", {}).get("tooltip") or "") for r in sent.get(step, [])]

    def delegated(step: str, index: int) -> bool:
        per_request = tools.get(step) or []
        return -len(per_request) <= index < len(per_request) and "transfer_to_BloodHound" in per_request[index]

    def no_bloodhound_tool(step: str, index: int) -> bool:
        per_request = tools.get(step) or []
        return not (-len(per_request) <= index < len(per_request)
                    and set(per_request[index]) & BLOODHOUND_TOOL_NAMES)

    def requests_of(step: str, channel: str | None = None) -> list[dict[str, Any]]:
        return [row for row, info in zip(rows.get(step, []), sent.get(step, []))
                if channel in (None, info.get("channel"))]

    def guard(step: str, index: int) -> str:
        requests = rows.get(step, [])
        found = guard_rows(requests[index]) if -len(requests) <= index < len(requests) else []
        return found[0] if len(found) == 1 else ""

    l2_guard, l3_guard, l5_guard = guard("L2", -1), guard("L3", -1), guard("L5", -1)
    c_l5 = requests_of("L5", "C")
    l4 = rows.get("L4") or [{}, {}]
    return {
        "L1": len(rows.get("L1", [])) == 3 and not any(bloodhound_rows(r) for r in rows["L1"])
        and chips("L1") == ["not_configured"] * 3,
        "L2": delegated("L2", -1) and GUARD_NOT_CONFIGURED in l2_guard and GUARD_NO_READ in l2_guard
        and no_bloodhound_tool("L2", -1),
        "L3": len(rows.get("L3", [])) == 3 and not any(bloodhound_rows(r) for r in rows["L3"])
        and chips("L3") == ["unavailable"] * 3 and len(tooltips("L3")) == 3
        and all(CONNECT_FAILED in tooltip for tooltip in tooltips("L3")) and delegated("L3", -1)
        and GUARD_CONFIGURED_NOT_CONNECTED in l3_guard and GUARD_NO_READ in l3_guard and CONNECT_FAILED in l3_guard
        and GUARD_NOT_CONFIGURED not in l3_guard and no_bloodhound_tool("L3", -1),
        "L4": len(l4) == 2 and "verified by a live domain read" in text_of(l4[0]) and "**Ingest:** Ready" in text_of(l4[0])
        and not bloodhound_rows(l4[1]) and chips("L4")[-1:] == ["connected"],
        "L5": len(c_l5) == 3 and [m["chat_response_key"] for r in c_l5 for m in bloodhound_rows(r)]
        == [f"bloodhound_lost:{(run.get('channels') or {}).get('C')}"]
        and not any(bloodhound_rows(r) for r in requests_of("L5", "E")) and len(requests_of("L5", "E")) == 1
        and chips("L5", "C") == ["unavailable"] * 3 and chips("L5", "E") == ["unavailable"]
        and delegated("L5", -1) and CE_DID_NOT_ANSWER in l5_guard and GUARD_NO_READ in l5_guard
        and no_bloodhound_tool("L5", -1),
        "L6": len(rows.get("L6", [])) == 1 and not bloodhound_rows(rows["L6"][0]) and chips("L6") == ["connected"],
        "terminal": all(str(r.get("status") or "") in TERMINAL for step in rows for r in rows[step]),
        "tasks_and_ceilings": not run.get("stopped")
        and all(r.get("tasks_after") == run.get("task_baseline") for step in sent for r in sent[step])
        and not run.get("ceiling"),
    }


# --------------------------------------------------------------------------------------------------
# Mythic
# --------------------------------------------------------------------------------------------------

async def _operator_id(client: Any) -> int:
    username = str(getattr(client, "username", "") or nc.DEFAULT_USER)
    rows = (await cor._query(client, OPERATOR_QUERY, name=username)).get("operator") or []
    if len(rows) != 1:
        raise SystemExit(f"could not resolve the logged-in operator {username!r}")
    return int(rows[0]["id"])


async def _key(client: Any, *, create: bool) -> dict[str, Any] | None:
    operator_id = await _operator_id(client)
    key = usable_key(list((await cor._query(client, KEYS_QUERY, operatorId=operator_id)).get("apitokens") or []))
    if key is None and create:
        created = await cor._query(client, nc.CREATE_TOKEN_MUTATION, operatorId=operator_id, name=KEY_NAME,
                                   scopes=sorted(REQUIRED_KEY_SCOPES))
        made = created.get("createAPIToken") or {}
        if made.get("status") != "success":
            raise SystemExit(f"could not create the channel's API key: {made.get('error')}")
        key = {"id": made["id"], "created": True}
    return key


async def _channel(client: Any, container_id: int, key_id: int, name: str, metadata: dict[str, Any]) -> int:
    created = await cor._query(client, nc.CREATE_CHANNEL_MUTATION, name=f"{name} {int(time.time())}",
                               description="Sage BloodHound status live check", containerId=container_id,
                               model="Sage", tokenId=key_id, metadata=nc.default_ai_metadata(metadata))
    made = created.get("chatCreateChannel") or {}
    if made.get("status") != "success":
        raise SystemExit(f"channel creation failed: {made.get('error')}")
    return int(made.get("channel_id") or made.get("id"))


async def _chip(client: Any, channel_id: int) -> dict[str, Any]:
    stored = (await cor._query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {}
    return bloodhound_chip(stored.get("ai_metadata"))


async def _request_rows(client: Any, request_id: int) -> dict[str, Any]:
    data = await cor._query(client, MESSAGES_QUERY, rid=request_id)
    return {"status": (data.get("chat_request_by_pk") or {}).get("status"),
            "messages": list(data.get("chat_message") or [])}


async def _settings(client: Any, channel_id: int, **config: Any) -> list[str]:
    stored = (await cor._query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {}
    metadata = copy.deepcopy(stored.get("ai_metadata") or {})
    metadata["config"] = {**(metadata.get("config") or {}), **config}
    result = await cor._query(client, cor.UPDATE_CHANNEL, channelId=channel_id, metadata=metadata)
    if (result.get("chatUpdateChannel") or {}).get("status") != "success":
        raise SystemExit(f"settings change failed: {result}")
    return sorted(config)


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    route = read_route(Path(args.route_file))
    tokens = read_mcp_tokens(Path(args.bloodhound_mcp_env))
    containers = (await cor._query(client, nc.READINESS_QUERY)).get("consuming_container") or []
    key = await _key(client, create=False)
    report: dict[str, Any] = {
        "route": cor.route_summary(route), "route_models": {"a": route["model"], "lane": route["model_b"]},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "chat_container_id": int(containers[0]["id"]) if containers else None,
        "open_requests": (await cor._query(client, cor.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "parity": _parity(), "identity": _identity(), "offline_tests": _offline_tests(),
        "phoenix_readable": Path(args.phoenix_db).is_file(),
        "ce_container_running": ce_running(args.ce_container), "ce_api_status": ce_api_answers(args.ce_api),
        "mcp_token_keys": sorted(tokens), "task_baseline": await cor._max_task(client),
        "creator_key": {"id": key["id"]} if key else {"create": sorted(REQUIRED_KEY_SCOPES)}, "ceilings": CEILINGS,
        "mcp_startup_check": mcp_startup_check(),
    }
    report.update(sage_restart(_head_committed_at()))
    ready_so_far = bool(
        report["route"]["direct_openai"] and report["sage_container_running"] and not report["open_requests"]
        and report["parity"]["returncode"] == 0 and not report["identity"]["dirty"]
        and report["identity"]["branch"] == BRANCH and report["offline_tests"]["returncode"] == 0
        and report["phoenix_readable"] and report["ce_container_running"] and report["ce_api_status"] in (200, 401)
        and report["mcp_token_keys"] == ["BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY"]
        and report["mcp_startup_check"] and report["sage_restarted"]
    )
    if ready_so_far:
        key = await _key(client, create=True)
        channel_id = await _channel(client, report["chat_container_id"], int(key["id"]), "BloodHound status readiness",
                                    channel_config(route, lane=True))
        probes = {}
        for label, message in (("status", "/bloodhound"), ("model", READINESS_PROMPT)):
            sent = await nc.create_message(client, channel_id, message)
            await nc.wait_for_request(client, int(sent["chat_request_id"]), timeout_seconds=300)
            probes[label] = await _request_rows(client, int(sent["chat_request_id"]))
        report["readiness_channel"] = channel_id
        report["no_bloodhound_session"] = NOT_SET_UP in text_of(probes["status"])
        report["lane_model_answers"] = probes["model"]["status"] == "complete"
        report["readiness_requests"] = {label: row["status"] for label, row in probes.items()}
    report["ready"] = bool(ready_so_far and report.get("no_bloodhound_session") and report.get("lane_model_answers"))
    run["readiness"] = report
    return report


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not (run.get("readiness") or {}).get("ready"):
        raise SystemExit("readiness has not passed in this run file")
    if run.get("steps"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    route = read_route(Path(args.route_file))
    tokens = read_mcp_tokens(Path(args.bloodhound_mcp_env))
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, started_at=time.time(), identity=_identity(),
               task_baseline=await cor._max_task(client), phoenix_start=cor.phoenix_mark(phoenix), steps={},
               channels={})
    started = time.monotonic()
    key = await _key(client, create=True)
    run["channel_key"] = {"id": int(key["id"]), "created": bool(key.get("created"))}
    container_id = int(run["readiness"]["chat_container_id"])
    real = {"BLOODHOUND_URL": args.bloodhound_url, **tokens}
    placeholder = {"BLOODHOUND_URL": PLACEHOLDER_URL, **PLACEHOLDER_TOKENS}
    ce_stopped = False

    async def open_channel(label: str, *, lane: bool, bloodhound: dict[str, str] | None = None) -> int:
        channel_id = await _channel(client, container_id, int(key["id"]), f"BloodHound status {label}",
                                    channel_config(route, lane=lane, bloodhound=bloodhound))
        run["channels"][label] = channel_id
        return channel_id

    async def send(step: str, label: str, message: str) -> None:
        mark = cor.phoenix_mark(phoenix)
        began = time.monotonic()
        sent = await nc.create_message(client, run["channels"][label], message)
        request_id = int(sent["chat_request_id"])
        await nc.wait_for_request(client, request_id, timeout_seconds=300)
        run["steps"].setdefault(step, {"requests": []})["requests"].append({
            "channel": label, "request_id": request_id, "message": message, "phoenix_after": mark,
            "phoenix_until": cor.phoenix_mark(phoenix), "seconds": round(time.monotonic() - began, 1),
            "tasks_after": await cor._max_task(client), "chip": await _chip(client, run["channels"][label])})

    def checkpoint(step: str) -> bool:
        reason = over_ceiling(cor.model_spans(phoenix, run["phoenix_start"]), time.monotonic() - started)
        if any(r["tasks_after"] != run["task_baseline"] for r in run["steps"].get(step, {}).get("requests", [])):
            reason = "a target task was created"
        if reason:
            run["stopped"] = {"step": step, "reason": reason}
        return not reason

    try:
        await open_channel("A", lane=False)
        await send("L1", "A", PLAIN)
        await _settings(client, run["channels"]["A"], model=route["model_b"], SAGE_OPENAI_ACCESS_PROGRAM="standard")
        await send("L1", "A", PLAIN)
        await send("L1", "A", PLAIN)
        if checkpoint("L1"):
            await send("L2", "A", GRAPH)
        if not run.get("stopped") and checkpoint("L2"):
            await open_channel("B", lane=True, bloodhound=placeholder)
            for message in (PLAIN, PLAIN, GRAPH):
                await send("L3", "B", message)
        if not run.get("stopped") and checkpoint("L3"):
            await open_channel("C", lane=True, bloodhound=real)
            await send("L4", "C", "/bloodhound force")
            await send("L4", "C", PLAIN)
        if not run.get("stopped") and checkpoint("L4"):
            stopped = _docker("stop", args.ce_container)
            ce_stopped = stopped.returncode == 0
            run["ce_stop"] = {"returncode": stopped.returncode, "running_after": ce_running(args.ce_container)}
            await send("L5", "C", PLAIN)
            await send("L5", "C", PLAIN)
            await open_channel("E", lane=True, bloodhound=real)
            await send("L5", "E", PLAIN)
            await send("L5", "C", GRAPH)
        if ce_stopped:
            started_ce = _docker("start", args.ce_container)
            run["ce_start"] = {"returncode": started_ce.returncode, "api_seconds": wait_for_ce(args.ce_api)}
            ce_stopped = False
        if not run.get("stopped") and checkpoint("L5"):
            await send("L6", "C", PLAIN)
            checkpoint("L6")
    finally:
        if ce_stopped:
            _docker("start", args.ce_container)
            run["ce_start"] = {"cleanup": True, "api_seconds": wait_for_ce(args.ce_api)}
        run["finished_at"] = time.time()
        run["cleanup"] = {"ce_running": ce_running(args.ce_container), "ce_api_status": ce_api_answers(args.ce_api)}
    return {"channels": run["channels"], "steps": {s: [r["request_id"] for r in i["requests"]]
                                                    for s, i in run["steps"].items()},
            "stopped": run.get("stopped"), "cleanup": run["cleanup"]}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    phoenix = Path(args.phoenix_db)
    steps = run.get("steps") or {}
    rows = {step: [await _request_rows(client, int(r["request_id"])) for r in info["requests"]]
            for step, info in steps.items()}
    tools = {step: [produced_tool_names(phoenix, r["phoenix_after"], r["phoenix_until"]) for r in info["requests"]]
             for step, info in steps.items()}
    every = cor.model_spans(phoenix, run["phoenix_start"],
                            max((r["phoenix_until"] for i in steps.values() for r in i["requests"]), default=None))
    elapsed = float(run.get("finished_at", 0)) - float(run.get("started_at", 0))
    run["ceiling"] = over_ceiling(every, elapsed)
    criteria = criteria_for(run, rows, tools)
    closings = {step: [text_of(row)[-600:] for row in rows.get(step, [])][-1:] for step in ("L2", "L3", "L5")}
    observations = {
        "provider_calls": len(every), "total_tokens": sum(s["prompt_tokens"] + s["completion_tokens"] for s in every),
        "wall_seconds": round(elapsed, 1),
        "request_seconds": {step: [r["seconds"] for r in info["requests"]] for step, info in steps.items()},
        "chips": {step: [r["chip"] for r in info["requests"]] for step, info in steps.items()},
        "tool_calls": tools, "guard_rows": {step: [guard_rows(row) for row in rows[step]] for step in rows},
        "graph_turn_text_tail": closings, "channels": run.get("channels"), "ce": [run.get("ce_stop"), run.get("ce_start")],
        "cleanup": run.get("cleanup"),
    }
    seam_arose = all(steps.get(step) for step in ("L2", "L3", "L5"))
    verdict = ("pass" if all(criteria.values()) else "fail") if seam_arose else "not-run (a seam step never ran)"
    run["verify"] = {"criteria": criteria, "observations": observations, "verdict": verdict}
    return run["verify"]


async def _main(args: argparse.Namespace) -> int:
    run_path = Path(args.run_file)
    run = cor._load(run_path)
    client = await nc.login()
    handlers = {"readiness": cmd_readiness, "run": cmd_run, "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        cor._save(run_path, run)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("readiness", "run", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--run-file", required=True)
        command.add_argument("--phoenix-db", required=True,
                             help="the deployed Sage's live phoenix.db, read in place (read-only)")
        if name in {"readiness", "run"}:
            command.add_argument("--route-file", required=True)
            command.add_argument("--bloodhound-mcp-env", required=True,
                                 help="the BloodHound MCP server's own .env; only its two token values are read")
            command.add_argument("--bloodhound-url", required=True,
                                 help="BLOODHOUND_URL as the Sage container reaches BloodHound CE")
            command.add_argument("--ce-api", required=True, help="BloodHound CE's URL as this host reaches it")
            command.add_argument("--ce-container", required=True, help="the BloodHound CE web container's name")
        if name == "run":
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this bounded attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
