#!/usr/bin/env python3
"""Native check for channel-scoped Codex ACP connection controls (`/acp`) on a container deployment.

Configuration only: no model prompt, no provider inference and no target task. Steps share one run file (JSON).
``readiness`` reads the deployment and sends one ``/acp status`` in a fresh readiness channel. ``run`` is the
authorized attempt: it starts a dedicated, network-disabled worker (``sage-codex-check``) on the deployment's
socket directory with a public, per-run sentinel as its API-key input, creates channels A (conversation) and B
(supervised), sends the fixed sequence below once (no retries), restarts that worker once, restarts the Sage
container once, recreates the worker without a key, and finally stops it. ``verify`` reads Mythic's own rows
and the worker's bridge events and process snapshots captured during the run, and applies the rules once.

  readiness --run-file F --service-dir D --phoenix-db P --plan PLAN [--worker-image TAG]
  run       (same) --authorized-by TEXT
  verify    --run-file F --phoenix-db P

``--service-dir`` is Sage's installed service directory (the ``/Mythic`` bind mount). Its ``.sage_acp`` directory
must exist (mode 0700, owned by the directory's owner) and its ``.env`` must set
``SAGE_ACP_SOCKET=/Mythic/.sage_acp/acp.sock``.

Pass rules, written before the run and read once. "Reply" is the request's one AI message.
  C1 A: status, connect claude, connect codex, models. Status reads `disconnected` (configured, not unconfigured);
     the unknown profile is refused and the worker accepts no connection for it; connect reads connected over
     ACP v1 with provider access unverified and adds one worker connection; models lists at least two model IDs.
  C2 A: model <another advertised ID>, status. Effective and saved model are that exact ID; the worker relayed one
     session/set_config_option on A's connection.
  C3 B: connect codex, status. B is connected on its own worker connection with the agent's default model;
     at least two adapter processes are live.
  C4 A: model not-a-real-model, status. Refused as not advertised; no further session/set_config_option on A's
     connection; A's effective model is still the C2 ID.
  C5 A: disconnect; B: status. A's selection is cleared and its worker connection is cleaned up (adapter exited,
     within five seconds); B is still connected with its default model.
  C6 Restart only the worker; B: status, connect. Status reads disconnected (no stale connected claim); connect
     reads connected and shows B's saved selection.
  C7 Restart the Sage container; B: status, connect. Status reads disconnected with B's saved selection; connect
     reads connected.
  C8 B: stop, status. Stop keeps the existing no-session wording and adds the coding-agent closure; the worker
     cleans up B's connection within five seconds of the stop's reply; status reads disconnected with B's saved
     selection.
  C9 Recreate the worker with empty state and no key; A: connect, status, disconnect. No `authenticated` claim;
     the observed state is `unauthenticated` or `unknown` and the reply says the worker has no usable API key;
     the worker reports no key provisioned; disconnect clears A's selection.
  C10 Stop the worker; A: connect. A bounded failure naming the unavailable worker, with no fallback.
  Every request ends `complete` with exactly one reply. 22 slash requests (ceiling 25), within 15 minutes; zero
  target tasks; zero LLM spans in Phoenix; the worker relays no session/prompt, fs/*, terminal/* or permission
  request; the sentinel appears in no native row and no worker event.
Observations, not pass criteria: connect latency (about 15 s with networking disabled), worker cleanup stages.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sage_openai_check_for_acp", _HERE / "check_openai_responses.py")
cor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cor
_spec.loader.exec_module(cor)  # type: ignore[union-attr]
nc = cor.nc
REPO_ROOT = cor.REPO_ROOT

BRANCH = "feature/codex-acp-connection"
WORKER = "sage-codex-check"
DEFAULT_IMAGE = "sage-codex-acp:2.9.0"
SAGE_CONTAINER = "sage"
SOCKET_SUBDIR = ".sage_acp"
SAGE_SOCKET = "/Mythic/.sage_acp/acp.sock"
EXPECTED_SDK = "1.0.0"
EXPECTED_LABELS = {"io.sage.acp.adapter": "@agentclientprotocol/codex-acp@2.9.0", "io.sage.acp.codex": "0.171.0"}
CEILINGS = {"slash_requests": 25, "minutes": 15, "target_tasks": 0, "llm_spans": 0}
EXPECTED_REQUESTS = 22
UNKNOWN_MODEL = "not-a-real-model"
KEY_NAME = "Sage ACP connection live check"
REQUIRED_KEY_SCOPES = {"apitoken.write", "chat-ai.write", "callback.read"}
OFFLINE_TESTS = ("tests/test_acp_connection.py",)
FORBIDDEN_METHOD = re.compile(r"^(session/prompt|fs/|terminal/|session/request_permission)")
MESSAGES_QUERY = """
query SageAcpCheckMessages($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message }
  chat_request_by_pk(id: $rid) { id status }
}
"""
OPERATOR_QUERY = "query SageAcpCheckMe($name: String!) { operator(where: {username: {_eq: $name}}) { id } }"
KEYS_QUERY = """
query SageAcpCheckKeys($operatorId: Int!) {
  apitokens(where: {operator_id: {_eq: $operatorId}, active: {_eq: true}, deleted: {_eq: false},
                    token_type: {_eq: "api"}}, order_by: {id: asc}) { id scopes }
}
"""


# --------------------------------------------------------------------------------------------------
# Docker and files
# --------------------------------------------------------------------------------------------------

def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity() -> dict[str, Any]:
    return {"branch": _git("rev-parse", "--abbrev-ref", "HEAD"), "head": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "--", "Payload_Type/sage", "skills")),
            "bridge_sha256": hashlib.sha256(
                (REPO_ROOT / "Payload_Type/sage/container/codex/bridge.mjs").read_bytes()).hexdigest()}


def _parity() -> dict[str, Any]:
    command = [str(REPO_ROOT / ".venv/bin/python"), str(REPO_ROOT / "skills/sage-goad-reset/scripts/sage_deployment.py"),
               "--mode", "container", "parity"]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT)
    return {"returncode": completed.returncode, "tail": completed.stdout.strip().splitlines()[-3:]}


def _offline_tests() -> dict[str, Any]:
    completed = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:logging",
                                *OFFLINE_TESTS], capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type/sage")
    return {"returncode": completed.returncode, "summary": completed.stdout.strip().splitlines()[-1:]}


def _sage_line(plan: str) -> dict[str, Any]:
    completed = subprocess.run(["sage-line", "check-attempt", plan], capture_output=True, text=True)
    return {"returncode": completed.returncode, "tail": (completed.stdout + completed.stderr).strip().splitlines()[-3:]}


def env_setting(path: Path, name: str) -> str:
    """The last uncommented ``NAME=value`` in an env file, or ""."""
    value = ""
    if path.is_file():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                value = stripped.split("=", 1)[1].strip().strip("\"'")
    return value


def socket_dir_state(service_dir: Path) -> dict[str, Any]:
    owner = service_dir.stat()
    target = service_dir / SOCKET_SUBDIR
    if not target.is_dir():
        return {"exists": False, "service_owner": f"{owner.st_uid}:{owner.st_gid}",
                "create_with": f"install -d -m 0700 -o {owner.st_uid} -g {owner.st_gid} {target}"}
    info = target.stat()
    return {"exists": True, "service_owner": f"{owner.st_uid}:{owner.st_gid}", "owner": f"{info.st_uid}:{info.st_gid}",
            "mode": oct(stat.S_IMODE(info.st_mode)), "private": stat.S_IMODE(info.st_mode) == 0o700,
            "owned_by_service_owner": (info.st_uid, info.st_gid) == (owner.st_uid, owner.st_gid)}


def sage_state(service_dir: Path) -> dict[str, Any]:
    inspect = _docker("inspect", SAGE_CONTAINER)
    mounts, started = [], ""
    if inspect.returncode == 0:
        data = json.loads(inspect.stdout)[0]
        mounts = [m for m in data.get("Mounts", []) if m.get("Destination") == "/Mythic"]
        started = data.get("State", {}).get("StartedAt", "")
    status = _docker("exec", SAGE_CONTAINER, "cat", "/proc/1/status").stdout
    ids = dict(re.findall(r"^(Uid|Gid):\s+\d+\s+(\d+)", status, re.M))  # effective IDs of Sage's process
    uid = f"{ids.get('Uid', '')}:{ids.get('Gid', '')}"
    sdk = _docker("exec", SAGE_CONTAINER, "python3", "-c",
                  "import importlib.metadata as m; print(m.version('agent-client-protocol'))").stdout.strip()
    return {"running": inspect.returncode == 0, "started_at": started,
            "mythic_mount_matches": bool(mounts) and Path(mounts[0]["Source"]).resolve() == service_dir.resolve(),
            "process_owner": uid, "sdk_version": sdk}


def worker_image_state(image: str) -> dict[str, Any]:
    inspect = _docker("image", "inspect", image)
    if inspect.returncode != 0:
        return {"present": False}
    data = json.loads(inspect.stdout)[0]
    labels = (data.get("Config") or {}).get("Labels") or {}
    probe = _docker("run", "--rm", "--network", "none", "--entrypoint", "node", image, "-e",
                    "const c=require('crypto'),f=require('fs');const r=p=>require(p).version;"
                    "console.log(JSON.stringify({adapter:r('/opt/codex-acp/node_modules/@agentclientprotocol/codex-acp/package.json'),"
                    "codex:r('/opt/codex-acp/node_modules/@openai/codex/package.json'),"
                    "bridge:c.createHash('sha256').update(f.readFileSync('/opt/codex-acp/bridge.mjs')).digest('hex')}))")
    try:
        versions = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        versions = {}
    return {"present": True, "id": data.get("Id"), "labels": {k: labels.get(k) for k in EXPECTED_LABELS},
            "versions": versions}


def worker_events() -> list[dict[str, Any]]:
    completed = _docker("logs", WORKER)
    events = []
    for line in (completed.stdout + completed.stderr).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            events.append(row)
    return events


def worker_processes() -> list[dict[str, str]]:
    completed = _docker("top", WORKER, "-o", "pid,pgid,args")
    rows = []
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and "bridge.mjs" not in parts[2] and "docker-init" not in parts[2]:
            rows.append({"pid": parts[0], "pgid": parts[1], "args": parts[2][:160]})
    return rows


def start_worker(service_dir: Path, image: str, owner: str, key_file: Path | None) -> dict[str, Any]:
    uid, gid = owner.split(":")
    args = ["run", "-d", "--name", WORKER, "--init", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", owner,
            "--tmpfs", f"/state:mode=0700,uid={uid},gid={gid}", "--tmpfs", "/tmp:mode=1777",
            "-v", f"{service_dir / SOCKET_SUBDIR}:/transport"]
    if key_file is not None:
        args += ["-v", f"{key_file}:/run/secrets/codex_api_key:ro"]
    started = _docker(*args, image)
    listening = _wait(lambda: any(e.get("event") == "listening" for e in worker_events()), 30)
    return {"returncode": started.returncode, "listening": listening, "key": key_file is not None}


def _wait(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return bool(predicate())


def restart_sage() -> dict[str, Any]:
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    restarted = _docker("restart", "-t", "30", SAGE_CONTAINER, timeout=180)
    ready = _wait(lambda: "Successfully connected to rabbitmq" in "".join(
        (lambda c: c.stdout + c.stderr)(_docker("logs", "--since", since, SAGE_CONTAINER))), 240)
    return {"returncode": restarted.returncode, "consuming": ready,
            "started_at": _docker("inspect", "-f", "{{.State.StartedAt}}", SAGE_CONTAINER).stdout.strip()}


# --------------------------------------------------------------------------------------------------
# Pure rules (tested offline)
# --------------------------------------------------------------------------------------------------

def replies(row: dict[str, Any]) -> list[str]:
    return [str(m.get("message") or "") for m in row.get("messages", []) if m.get("author_type") == "ai"]


def reply(row: dict[str, Any]) -> str:
    found = replies(row)
    return found[0] if len(found) == 1 else ""


def conn_events(events: list[dict[str, Any]], conn: Any, **match: Any) -> list[dict[str, Any]]:
    return [e for e in events if e.get("conn") == conn and all(e.get(k) == v for k, v in match.items())]


def _epoch(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def cleaned_within(events: list[dict[str, Any]], conn: Any, deadline: float) -> bool:
    return any(e.get("exited") is True and _epoch(str(e.get("time"))) <= deadline
               for e in conn_events(events, conn, event="cleanup"))


def criteria_for(run: dict[str, Any], rows: dict[int, dict[str, Any]]) -> dict[str, bool]:
    steps = run.get("steps") or {}
    sent = {step: info.get("requests", []) for step, info in steps.items()}
    logs = run.get("worker_logs") or {}
    first, second = logs.get("first") or [], logs.get("second") or []
    chosen = run.get("models") or {}
    default, other = chosen.get("default", ""), chosen.get("other", "")

    def text(step: str, index: int) -> str:
        requests = sent.get(step) or []
        if not -len(requests) <= index < len(requests):
            return ""
        return reply(rows.get(int(requests[index]["request_id"]), {}))

    def info(step: str, index: int) -> dict[str, Any]:
        requests = sent.get(step) or []
        return requests[index] if -len(requests) <= index < len(requests) else {}

    a_conn, b_conn = run.get("conns", {}).get("A1"), run.get("conns", {}).get("B1")
    b_last = run.get("conns", {}).get("B3")
    connected, disconnected = "| Transport | `connected` |", "| Transport | `disconnected` |"
    saved = "(saved for this channel)"
    every = [r for requests in sent.values() for r in requests]
    all_rows = [rows.get(int(r["request_id"]), {}) for r in every]
    all_events = first + second
    stop = info("C8", 0)
    return {
        "C1": disconnected in text("C1", 0) and "unconfigured" not in text("C1", 0)
        and "Unknown coding-agent profile `claude`" in text("C1", 1) and "Nothing was started" in text("C1", 1)
        and info("C1", 1).get("accepts_after") == info("C1", 0).get("accepts_after")
        and connected in text("C1", 2) and "| Protocol | ACP v1 |" in text("C1", 2)
        and "| Provider access | unverified" in text("C1", 2)
        and info("C1", 2).get("accepts_after") == info("C1", 1).get("accepts_after", -9) + 1
        and len(re.findall(r"^\| `[^`]+` \|", text("C1", 3), re.M)) >= 2 and bool(default) and bool(other),
        "C2": f"| Effective model | `{other}` |" in text("C2", 0) and f"| Saved model | `{other}` |" in text("C2", 1)
        and len(conn_events(first, a_conn, event="message", method="session/set_config_option")) == 1,
        "C3": connected in text("C3", 0) and f"| Effective model | `{default}` |" in text("C3", 0)
        and b_conn not in (None, a_conn) and connected in text("C3", 1)
        and len({p["pgid"] for p in info("C3", 1).get("processes", [])}) >= 2,
        "C4": "is not a model this agent advertises" in text("C4", 0)
        and info("C4", 0).get("a_settings_after") == 1 and f"| Effective model | `{other}` |" in text("C4", 1),
        "C5": "Disconnected this channel's coding agent and cleared its saved selection." in text("C5", 0)
        and cleaned_within(first, a_conn, float(info("C5", 0).get("completed_at", 0)) + 5)
        and connected in text("C5", 1) and f"| Effective model | `{default}` |" in text("C5", 1),
        "C6": bool(run.get("worker_restart", {}).get("listening")) and disconnected in text("C6", 0)
        and connected in text("C6", 1) and saved in text("C6", 1),
        "C7": bool(run.get("sage_restart", {}).get("consuming"))
        and run.get("sage_restart", {}).get("started_at") != (run.get("readiness") or {}).get("sage", {}).get("started_at")
        and disconnected in text("C7", 0) and saved in text("C7", 0) and connected in text("C7", 1),
        "C8": text("C8", 0).startswith("No running session to stop on this channel.")
        and "Closed this channel's coding-agent connection; its saved selection remains." in text("C8", 0)
        and b_last is not None and cleaned_within(first, b_last, float(stop.get("completed_at", 0)) + 5)
        and disconnected in text("C8", 1) and saved in text("C8", 1),
        "C9": all("`authenticated`" not in text("C9", i) for i in (0, 1)) and connected in text("C9", 0)
        and any(state in text("C9", 0) for state in ("`unauthenticated`", "`unknown`"))
        and "No usable API-key configuration in the worker" in text("C9", 0)
        and any(e.get("event") == "accept" and e.get("api_key_provisioned") is False for e in second)
        and "cleared its saved selection" in text("C9", 2),
        "C10": "| Transport | `failed` |" in text("C10", 0) and "worker is unavailable" in text("C10", 0)
        and info("C10", 0).get("accepts_after") == info("C9", 2).get("accepts_after"),
        "terminal_one_reply": bool(every) and all(r.get("status") == "complete" and len(replies(r)) == 1
                                                   for r in all_rows),
        "budget": len(every) == EXPECTED_REQUESTS and len(every) <= CEILINGS["slash_requests"]
        and float(run.get("finished_at", 0)) - float(run.get("started_at", 0)) <= CEILINGS["minutes"] * 60,
        "no_tasks_no_inference": not run.get("stopped") and run.get("llm_spans") == 0
        and all(r.get("tasks_after") == run.get("task_baseline") for r in every),
        "no_forbidden_methods": not any(FORBIDDEN_METHOD.match(str(e.get("method") or "")) for e in all_events),
        "no_sentinel": bool(run.get("sentinel")) and not any(run["sentinel"] in json.dumps(r) for r in all_rows)
        and not any(run["sentinel"] in json.dumps(e) for e in all_events),
    }


# --------------------------------------------------------------------------------------------------
# Mythic
# --------------------------------------------------------------------------------------------------

async def _key(client: Any) -> dict[str, Any]:
    username = str(getattr(client, "username", "") or nc.DEFAULT_USER)
    operators = (await cor._query(client, OPERATOR_QUERY, name=username)).get("operator") or []
    if len(operators) != 1:
        raise SystemExit(f"could not resolve the logged-in operator {username!r}")
    operator_id = int(operators[0]["id"])
    for key in (await cor._query(client, KEYS_QUERY, operatorId=operator_id)).get("apitokens") or []:
        scopes = set(key.get("scopes") or [])
        if "*" in scopes or REQUIRED_KEY_SCOPES <= scopes:
            return {"id": int(key["id"]), "created": False}
    created = (await cor._query(client, nc.CREATE_TOKEN_MUTATION, operatorId=operator_id, name=KEY_NAME,
                                scopes=sorted(REQUIRED_KEY_SCOPES))).get("createAPIToken") or {}
    if created.get("status") != "success":
        raise SystemExit(f"could not create the channel's API key: {created.get('error')}")
    return {"id": int(created["id"]), "created": True}


async def _channel(client: Any, container_id: int, key_id: int, label: str, mode: str) -> int:
    metadata = {"config": {"mode": mode, "autonomous_solve": False},
                "channel_metadata_display": {"display": nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}
    created = (await cor._query(client, nc.CREATE_CHANNEL_MUTATION, name=f"ACP check {label} {int(time.time())}",
                                description="Sage ACP connection live check", containerId=container_id, model="Sage",
                                tokenId=key_id, metadata=nc.default_ai_metadata(metadata))).get("chatCreateChannel") or {}
    if created.get("status") != "success":
        raise SystemExit(f"channel creation failed: {created.get('error')}")
    return int(created.get("channel_id") or created.get("id"))


async def _rows(client: Any, request_id: int) -> dict[str, Any]:
    data = await cor._query(client, MESSAGES_QUERY, rid=request_id)
    return {"status": (data.get("chat_request_by_pk") or {}).get("status"),
            "messages": list(data.get("chat_message") or [])}


async def _send(client: Any, channel_id: int, message: str) -> tuple[int, dict[str, Any]]:
    sent = await nc.create_message(client, channel_id, message)
    request_id = int(sent["chat_request_id"])
    await nc.wait_for_request(client, request_id, timeout_seconds=120)
    return request_id, await _rows(client, request_id)


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    service_dir = Path(args.service_dir).resolve()
    containers = (await cor._query(client, nc.READINESS_QUERY)).get("consuming_container") or []
    sage = sage_state(service_dir)
    sockets = socket_dir_state(service_dir)
    image = worker_image_state(args.worker_image)
    identity = _identity()
    report: dict[str, Any] = {
        "identity": identity, "parity": _parity(), "offline_tests": _offline_tests(), "sage_line": _sage_line(args.plan),
        "sage": sage, "socket_dir": sockets, "worker_image": image,
        "sage_acp_socket": env_setting(service_dir / ".env", "SAGE_ACP_SOCKET"),
        "worker_absent": _docker("inspect", WORKER).returncode != 0,
        "chat_container_id": int(containers[0]["id"]) if containers else None,
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await cor._query(client, cor.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "phoenix_readable": Path(args.phoenix_db).is_file(), "task_baseline": await cor._max_task(client),
        "ceilings": CEILINGS,
    }
    ready_so_far = bool(
        identity["branch"] == BRANCH and not identity["dirty"] and report["parity"]["returncode"] == 0
        and report["offline_tests"]["returncode"] == 0 and report["sage_line"]["returncode"] == 0
        and sage["running"] and sage["mythic_mount_matches"] and sage["sdk_version"] == EXPECTED_SDK
        and sockets.get("exists") and sockets.get("private") and sockets.get("owned_by_service_owner")
        and sage["process_owner"] == sockets.get("service_owner")
        and image.get("present") and image.get("labels") == EXPECTED_LABELS
        and image.get("versions", {}).get("bridge") == identity["bridge_sha256"]
        and report["sage_acp_socket"] == SAGE_SOCKET and report["worker_absent"]
        and report["sage_container_running"] and not report["open_requests"] and report["phoenix_readable"]
    )
    if ready_so_far:
        key = await _key(client)
        channel_id = await _channel(client, report["chat_container_id"], key["id"], "readiness", "conversation")
        request_id, rows = await _send(client, channel_id, "/acp status")
        report["readiness_channel"] = {"channel": channel_id, "request": request_id, "status": rows["status"]}
        report["configured_not_connected"] = "| Transport | `disconnected` |" in reply(rows)
    report["ready"] = bool(ready_so_far and report.get("configured_not_connected"))
    run["readiness"] = report
    return report


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not (run.get("readiness") or {}).get("ready"):
        raise SystemExit("readiness has not passed in this run file")
    if run.get("steps"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate = _sage_line(args.plan)
    if gate["returncode"] != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate['tail']}")
    service_dir = Path(args.service_dir).resolve()
    owner = run["readiness"]["socket_dir"]["service_owner"]
    phoenix = Path(args.phoenix_db)
    sentinel = f"public-acp-check-sentinel-{secrets.token_hex(6)}"
    key_dir = Path(tempfile.mkdtemp(prefix="sage-acp-check-"))
    key_file = key_dir / "codex_api_key"
    key_file.write_text(sentinel + "\n")
    key_file.chmod(0o600)
    run.update(authorized_by=args.authorized_by, started_at=time.time(), identity=_identity(), sentinel=sentinel,
               task_baseline=await cor._max_task(client), phoenix_start=cor.phoenix_mark(phoenix), steps={},
               channels={}, conns={}, worker_logs={}, gate=gate)
    started = time.monotonic()
    key = await _key(client)
    container_id = int(run["readiness"]["chat_container_id"])

    def accepts() -> list[int]:
        return [e.get("conn") for e in worker_events() if e.get("event") == "accept"]

    async def send(step: str, label: str, message: str, *, note_conn: str = "") -> str:
        before = accepts()
        sent_at = time.time()
        request_id, rows = await _send(client, run["channels"][label], message)
        after = accepts()
        entry = {"channel": label, "request_id": request_id, "message": message, "sent_at": sent_at,
                 "completed_at": time.time(), "tasks_after": await cor._max_task(client),
                 "accepts_after": len(after), "processes": worker_processes()}
        if note_conn and len(after) > len(before):
            run["conns"][note_conn] = after[-1]
        if label == "A" and run["conns"].get("A1") is not None:
            entry["a_settings_after"] = len(conn_events(worker_events(), run["conns"]["A1"], event="message",
                                                        method="session/set_config_option"))
        run["steps"].setdefault(step, {"requests": []})["requests"].append(entry)
        if entry["tasks_after"] != run["task_baseline"]:
            run["stopped"] = {"step": step, "reason": "a target task was created"}
        elif len(cor.model_spans(phoenix, run["phoenix_start"])) > CEILINGS["llm_spans"]:
            run["stopped"] = {"step": step, "reason": "an LLM span was recorded"}
        elif time.monotonic() - started > CEILINGS["minutes"] * 60:
            run["stopped"] = {"step": step, "reason": "wall-time ceiling"}
        return reply(rows)

    def go() -> bool:
        return not run.get("stopped")

    try:
        run["worker_start"] = start_worker(service_dir, args.worker_image, owner, key_file)
        if not run["worker_start"]["listening"]:
            run["stopped"] = {"step": "setup", "reason": "the dedicated worker did not start"}
        if go():
            run["channels"]["A"] = await _channel(client, container_id, key["id"], "A", "conversation")
            run["channels"]["B"] = await _channel(client, container_id, key["id"], "B", "supervised")
            await send("C1", "A", "/acp status")
            await send("C1", "A", "/acp connect claude")
            await send("C1", "A", "/acp connect codex", note_conn="A1")
            models = await send("C1", "A", "/acp models")
            ids = re.findall(r"^\| `([^`]+)` \| [^|]* \| (yes)? *\|", models, re.M)
            default = next((value for value, current in ids if current), "")
            other = next((value for value, current in ids if not current), "")
            run["models"] = {"default": default, "other": other, "count": len(ids)}
        if go():
            await send("C2", "A", f"/acp model {run['models']['other']}")
            await send("C2", "A", "/acp status")
        if go():
            await send("C3", "B", "/acp connect codex", note_conn="B1")
            await send("C3", "B", "/acp status")
        if go():
            await send("C4", "A", f"/acp model {UNKNOWN_MODEL}")
            await send("C4", "A", "/acp status")
        if go():
            await send("C5", "A", "/acp disconnect")
            await send("C5", "B", "/acp status")
        if go():
            restarted = _docker("restart", "-t", "10", WORKER)
            run["worker_restart"] = {"returncode": restarted.returncode, "listening": _wait(
                lambda: sum(e.get("event") == "listening" for e in worker_events()) >= 2, 30)}
            await send("C6", "B", "/acp status")
            await send("C6", "B", "/acp connect codex", note_conn="B2")
        if go():
            run["sage_restart"] = restart_sage()
            await send("C7", "B", "/acp status")
            await send("C7", "B", "/acp connect codex", note_conn="B3")
        if go():
            await send("C8", "B", "/stop")
            _wait(lambda: bool(conn_events(worker_events(), run["conns"].get("B3"), event="cleanup")), 6)
            await send("C8", "B", "/acp status")
        if go():
            run["worker_logs"]["first"] = worker_events()
            _docker("rm", "-f", WORKER)
            run["worker_recreate"] = start_worker(service_dir, args.worker_image, owner, None)
            await send("C9", "A", "/acp connect codex", note_conn="A2")
            await send("C9", "A", "/acp status")
            await send("C9", "A", "/acp disconnect")
        if go():
            run["worker_stop"] = {"returncode": _docker("stop", "-t", "10", WORKER).returncode}
            await send("C10", "A", "/acp connect codex")
    finally:
        run["finished_at"] = time.time()
        logs = worker_events()
        run["worker_logs"].setdefault("first", logs)
        if run.get("worker_recreate"):
            run["worker_logs"]["second"] = logs
        run["llm_spans"] = len(cor.model_spans(phoenix, run["phoenix_start"]))
        run["cleanup"] = {"worker_removed": _docker("rm", "-f", WORKER).returncode == 0,
                          "socket_dir_kept": (service_dir / SOCKET_SUBDIR).is_dir()}
        key_file.unlink(missing_ok=True)
        key_dir.rmdir()
    return {"channels": run["channels"], "models": run.get("models"), "stopped": run.get("stopped"),
            "requests": sum(len(i["requests"]) for i in run["steps"].values()), "cleanup": run["cleanup"]}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    steps = run.get("steps") or {}
    rows = {int(r["request_id"]): await _rows(client, int(r["request_id"]))
            for info in steps.values() for r in info["requests"]}
    criteria = criteria_for(run, rows)
    observations = {
        "request_seconds": {step: [round(r["completed_at"] - r["sent_at"], 1) for r in info["requests"]]
                            for step, info in steps.items()},
        "cleanups": [{k: e.get(k) for k in ("conn", "reason", "stage", "ms")}
                     for e in (run.get("worker_logs") or {}).get("first", []) + (run.get("worker_logs") or {}).get(
                         "second", []) if e.get("event") == "cleanup"],
        "models": run.get("models"), "sage_restart": run.get("sage_restart"), "cleanup": run.get("cleanup"),
        "llm_spans": run.get("llm_spans"), "stopped": run.get("stopped"),
    }
    complete = all(steps.get(f"C{n}") for n in range(1, 11))
    verdict = ("pass" if all(criteria.values()) else "fail") if complete else "not-run (the sequence did not finish)"
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
            command.add_argument("--service-dir", required=True, help="Sage's installed service directory")
            command.add_argument("--plan", required=True, help="the line's plan file, for sage-line check-attempt")
            command.add_argument("--worker-image", default=DEFAULT_IMAGE)
        if name == "run":
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this bounded attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
