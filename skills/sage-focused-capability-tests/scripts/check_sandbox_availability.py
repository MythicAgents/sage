#!/usr/bin/env python3
"""Live check: the Sandbox runs on its confined local executor when Sage runs as Mythic's container.

The deployed Sage container must run the branch under test. One run file (JSON, owner-only) carries every stage.

  readiness --run-file F --route-file R --phoenix-db P [--phoenix-url U]
      Read-only. Deployment parity with this branch (container mode); Sage consuming; no open chat requests; the
      container's Landlock ABI (read with the deployed launcher) is at least 6; Sage in the container is not root;
      /Mythic/.env.local exists (C1's stand-in secret); the container's mounts and privileges (D's baseline); Phoenix
      readable and answering; the Mythic task baseline; this line's offline tests pass. The route credential, by name
      and status only: the installed .env.local's LiteLLM block is its only active provider block, a keyed
      verified-TLS GET /models from inside the container lists the route's model, the installed .env sets no
      credential, and no API_KEY user secret shadows it. No callback or range state is needed. Verdict:
      ready_for_live, deployment_required or not_ready.
  run --run-file F --route-file R --phoenix-db P --plan PLAN --authorized-by TEXT
      Runs `sage-line check-attempt PLAN` first and refuses on failure. Step 1: in a fresh Conversation channel, its
      first message is `/sandbox python SLASH_CODE` (no model involved). Step 2: in a fresh Supervised channel with
      no BloodHound settings it sends PROMPT once; it accepts each card whose only action is sandbox_exec and rejects
      any other card. It stops the request on a new Mythic task or a limit. No retry.
  verify --run-file F --phoenix-db P
      Applies the pass rules below once, reading Mythic's chat rows in place.

Pass rules (frozen with the plan's live check; read once):
  C1 The /sandbox reply shows executor `local`, and its stdout shows `4` and all three attempts denied with
     PermissionError: reading /Mythic/.env.local, connecting to 127.0.0.1:5672, listing /proc.
  M1 (the seam) In the supervised request, at least one sandbox_exec card from the Sandbox agent, with language
     python, was accepted.
  M2 That card's delegation has a completed sandbox_exec tool row whose result shows status ok and executor local,
     and whose stdout contains the SHA-256 digest of `sage-sandbox-check` and 75025, both computed here.
  M3 No other guarded tool ran (no completed tool row for any other guarded tool), and 0 Mythic tasks were created
     during the run.
  D  `docker inspect sage` after the run shows the same mounts and privileges as readiness recorded: one mount (at
     /Mythic), not privileged, no added capabilities or devices.
Observations, not pass criteria: whether the closing states both values, provider calls, tokens, wall time, and
Sage's Sandbox log lines.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"

PROMPT = ("Use the Sandbox to run Python that prints the SHA-256 hex digest of the text sage-sandbox-check and the "
          "25th Fibonacci number (F(1) = F(2) = 1). Report both values.")
SLASH_CODE = (
    "import os,socket,json;r={};exec(\"def t(n,f):\\n try:\\n  f();r[n]='allowed'\\n except OSError as e:\\n  "
    "r[n]='denied:'+type(e).__name__\");t('env_local',lambda:open('/Mythic/.env.local').read());"
    "t('rabbitmq',lambda:socket.create_connection(('127.0.0.1',5672),timeout=3));t('proc',lambda:os.listdir('/proc'));"
    "print(2+2);print(json.dumps(r,sort_keys=True))"
)
SLASH_PROBES = {"env_local": "denied:PermissionError", "proc": "denied:PermissionError",
                "rabbitmq": "denied:PermissionError"}
DIGEST = hashlib.sha256(b"sage-sandbox-check").hexdigest()
LIMITS = {"provider_calls": 15, "total_tokens": 150_000, "minutes": 15, "target_tasks": 0}
SUPERVISED_MAX_STEPS = 8
SLASH_TIMEOUT = 120
OFFLINE_TESTS = ("tests/test_sandbox_executor.py", "tests/test_graph_builds.py")
ROWS_QUERY = """
query SageSandboxCheckRows($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message metadata status
    tool_output }
  chat_request_by_pk(id: $rid) { id status }
}
"""
TASKS_AFTER_QUERY = "query SageSandboxCheckTasks($after: Int!) { task(where: {display_id: {_gt: $after}}) { display_id } }"
_ABI_PROBE = ("import sys; sys.path.insert(0, '/Mythic'); from ai.langgraph.sandbox_launcher import landlock_abi; "
              "import os; print(landlock_abi(), os.stat('/proc/1').st_uid, os.path.isfile('/Mythic/.env.local'))")


def fibonacci(n: int) -> int:
    a, b = 1, 1
    for _ in range(n - 2):
        a, b = b, a + b
    return b


def _module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _base():
    """Native-chat, Mythic and Phoenix helpers of the OpenAI Responses check."""
    return _module("sage_sandbox_check_base", _SCRIPTS / "check_openai_responses.py")


def _dep():
    return _module("sage_sandbox_check_dependency", _SCRIPTS / "check_dependency_advisories.py")


def _split():
    """LiteLLM-block credential rules and the in-container keyed model listing of the provider split check."""
    return _module("sage_sandbox_check_split", _SCRIPTS / "check_provider_split.py")


def _reasoning():
    return _module("sage_sandbox_check_reasoning", _SCRIPTS / "check_reasoning_tools.py")


def _guarded_tools() -> frozenset[str]:
    sys.path.insert(0, str(REPO_ROOT / "Payload_Type" / "sage"))
    from ai.langgraph.mythic_tools import GUARDED_TOOLS
    return frozenset(GUARDED_TOOLS)


# --------------------------------------------------------------------------------------------------
# Pass rules (pure)
# --------------------------------------------------------------------------------------------------

def _json(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else (value or {})
    except ValueError:
        return {}


def _fence(text: str, label: str) -> str:
    marker = f"```{label}\n"
    start = text.find(marker)
    if start < 0:
        return ""
    body = text[start + len(marker):]
    return body[: body.find("\n```")] if "\n```" in body else body


def slash_outcome(rows: dict[str, Any]) -> dict[str, Any]:
    replies = [str(row.get("message") or "") for row in rows.get("messages") or []
               if "**Sandbox result**" in str(row.get("message") or "")]
    reply = replies[-1] if replies else ""
    lines = _fence(reply, "stdout").splitlines()
    probes = _json(lines[1]) if len(lines) > 1 else {}
    passed = bool(len(replies) == 1 and "| Executor | `local` |" in reply and "| Status | `ok` |" in reply
                  and lines[:1] == ["4"] and probes == SLASH_PROBES)
    return {"C1": passed, "detail": {"replies": len(replies), "stdout_lines": lines[:3], "probes": probes}}


def _cards(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in messages:
        metadata = row.get("metadata") or {}
        snapshot = metadata.get("input_requested") or {}
        if metadata.get("special_type") == "input_requested" and isinstance(snapshot, dict):
            data = snapshot.get("data") or {}
            out.append({"row": row["id"], "status": snapshot.get("status"), "tool_name": data.get("tool_name"),
                        "arguments": data.get("arguments") or {}, "delegation_name": metadata.get("delegation_name"),
                        "delegation_id": metadata.get("delegation_id")})
    return out


def _tool_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in messages:
        metadata = row.get("metadata") or {}
        tool_use = metadata.get("tool_use") if metadata.get("special_type") == "tool_use" else None
        if isinstance(tool_use, dict):
            out.append({"row": row["id"], "tool_name": tool_use.get("tool_name"), "status": tool_use.get("status"),
                        "delegation_id": metadata.get("delegation_id"),
                        "delegation_name": metadata.get("delegation_name"),
                        "result": _json(row.get("tool_output") or tool_use.get("result_preview") or "")})
    return out


def supervised_outcome(rows: dict[str, Any], tasks: list[Any], guarded: frozenset[str]) -> dict[str, Any]:
    messages = list(rows.get("messages") or [])
    cards, tools = _cards(messages), _tool_rows(messages)
    seam = [card for card in cards if card["tool_name"] == "sandbox_exec" and card["delegation_name"] == "Sandbox"
            and card["status"] == "accepted" and card["arguments"].get("language") == "python"]
    delegations = {card["delegation_id"] for card in seam if card["delegation_id"]}
    fib = str(fibonacci(25))
    results = [tool for tool in tools if tool["tool_name"] == "sandbox_exec" and tool["status"] == "completed"
               and tool["delegation_id"] in delegations]
    m2 = any(isinstance(t["result"], dict) and t["result"].get("status") == "ok" and t["result"].get("executor") == "local"
             and DIGEST in str(t["result"].get("stdout")) and fib in str(t["result"].get("stdout")) for t in results)
    others = [tool["tool_name"] for tool in tools if tool["tool_name"] in guarded - {"sandbox_exec"}
              and tool["status"] == "completed"]
    texts = [str(row.get("message") or "") for row in messages
             if row.get("author_type") != "operator" and not (row.get("metadata") or {}).get("special_type")]
    closing = texts[-1] if texts else ""
    return {"M1": bool(seam), "M2": m2, "M3": not others and not tasks,
            "detail": {"cards": [{k: c[k] for k in ("row", "tool_name", "status", "delegation_name")} for c in cards],
                       "sandbox_results": [{"row": t["row"], "executor": (t["result"] or {}).get("executor"),
                                            "status": (t["result"] or {}).get("status")} for t in results],
                       "other_guarded_tools": others, "new_tasks": len(tasks), "request_status": rows.get("status")},
            "closing_states_both": DIGEST in closing and fib in closing}


def docker_holds(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(before and before == after and [m.get("Destination") for m in after.get("mounts") or []]
                == ["/Mythic"] and not after.get("privileged") and not after.get("cap_add") and not after.get("devices"))


# --------------------------------------------------------------------------------------------------
# Mythic, container and readiness
# --------------------------------------------------------------------------------------------------

async def _rows(client: Any, request_id: int) -> dict[str, Any]:
    data = await _base()._query(client, ROWS_QUERY, rid=request_id)
    return {"status": (data.get("chat_request_by_pk") or {}).get("status"),
            "messages": list(data.get("chat_message") or [])}


async def _new_tasks(client: Any, baseline: int) -> list[dict[str, Any]]:
    return list((await _base()._query(client, TASKS_AFTER_QUERY, after=baseline)).get("task") or [])


def docker_state() -> dict[str, Any]:
    raw = subprocess.run(["docker", "inspect", "sage"], capture_output=True, text=True, timeout=30).stdout
    info = (_json(raw) or [{}])[0]
    host = info.get("HostConfig") or {}
    return {"mounts": [{"Destination": m.get("Destination"), "Source": m.get("Source"), "RW": m.get("RW")}
                       for m in info.get("Mounts") or []],
            "privileged": bool(host.get("Privileged")), "cap_add": host.get("CapAdd") or [],
            "devices": host.get("Devices") or [], "security_opt": host.get("SecurityOpt") or [],
            "network_mode": host.get("NetworkMode")}


def _channel_metadata(route: dict[str, str], mode: str) -> dict[str, Any]:
    config = {"provider": "litellm", "model": route["MODEL"], "mode": mode, "autonomous_solve": False,
              "max_steps": SUPERVISED_MAX_STEPS}
    return {"config": config, "channel_metadata_display": {"display": _base().nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, dep, split, reasoning = _base(), _dep(), _split(), _reasoning()
    route = dep.read_route(Path(args.route_file))
    deployment = _module("sage_sandbox_check_deployment", _DEPLOYMENT_PATH)
    installed = Path(deployment._installed_service_dir())
    parity = reasoning._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                                  "parity"])
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    probe = subprocess.run(["docker", "exec", "sage", "python3", "-I", "-c", _ABI_PROBE], capture_output=True,
                           text=True, timeout=60).stdout.split()
    tests = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", *OFFLINE_TESTS],
                           capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type" / "sage")
    phoenix = Path(args.phoenix_db)
    gateway = {"GATEWAY_MODEL": route["MODEL"], "GATEWAY_ENDPOINT": route["API_ENDPOINT"]}
    local = split.dotenv_state((installed / ".env.local").read_text(), route["API_ENDPOINT"])
    installed_env = split.dotenv_state((installed / ".env").read_text() if (installed / ".env").exists() else "")
    container_key = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", split._CONTAINER_PROBE,
                                    route["API_ENDPOINT"], route["MODEL"]], capture_output=True, text=True, timeout=90)
    key_probe = _json((container_key.stdout.strip().splitlines() or ["{}"])[-1])
    secrets = await reasoning._operator_secret_names(client)
    report = {
        "route": {"model": route["MODEL"], "endpoint": "from the route file (not recorded)"},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "container": {"landlock_abi": int(probe[0]) if probe[:1] and probe[0].isdigit() else 0,
                      "sage_uid": int(probe[1]) if len(probe) > 1 and probe[1].isdigit() else None,
                      "env_local_present": probe[2:3] == ["True"]},
        "docker": docker_state(),
        "phoenix_readable": phoenix.is_file(),
        "phoenix_health": dep.phoenix_health(args.phoenix_url),
        "task_baseline": await base._max_task(client),
        "offline_tests": {"returncode": tests.returncode, "summary": tests.stdout.strip().splitlines()[-1:]},
        "credentials": {"installed_local": local, "installed_env": installed_env, "container": key_probe,
                        "operator_secrets": secrets},
        "limits": LIMITS,
    }
    blockers = [reason for ok, reason in (
        (report["sage_container_running"], "Sage container not consuming"),
        (not report["open_requests"], "open chat requests"),
        (report["container"]["landlock_abi"] >= 6, "the container's Landlock ABI is below 6 (or the launcher is absent)"),
        (report["container"]["sage_uid"] not in (None, 0), "Sage runs as root in the container"),
        (report["container"]["env_local_present"], "/Mythic/.env.local is absent in the container"),
        (report["phoenix_readable"] and report["phoenix_health"] == 200, "Phoenix unreadable or down"),
        (tests.returncode == 0, "this line's offline tests fail"),
        (not installed_env["credentials"], "the installed .env sets a credential"),
        (secrets.get("resolved") and "API_KEY" not in (secrets.get("names") or []),
         "an API_KEY user secret would shadow the installed key, or secrets are unreadable"),
    ) if not ok]
    blockers.extend(split.phase_blockers(1, local, key_probe, gateway))
    verdict = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


# --------------------------------------------------------------------------------------------------
# Run and verify
# --------------------------------------------------------------------------------------------------

def _usage(phoenix: Path, after: int) -> tuple[int, int]:
    spans = _base().model_spans(phoenix, after)
    return len(spans), sum(int(s.get("prompt_tokens", 0)) + int(s.get("completion_tokens", 0)) for s in spans)


def _over_limit(run: dict[str, Any], phoenix: Path, started: float) -> str:
    calls, tokens = _usage(phoenix, run["phoenix_start"])
    if calls > LIMITS["provider_calls"]:
        return "provider call limit"
    if tokens > LIMITS["total_tokens"]:
        return "token limit"
    if time.monotonic() - started > LIMITS["minutes"] * 60:
        return "time limit"
    return ""


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    nc = base.nc
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness is not ready_for_live in this run file")
    if run.get("slash") or run.get("supervised"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate_check = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    run["check_attempt"] = {"returncode": gate_check.returncode, "output": gate_check.stdout.strip()[-400:]}
    if gate_check.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate_check.stdout.strip()} {gate_check.stderr.strip()}")
    route = _dep().read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, started_at=time.time(), route={"MODEL": route["MODEL"]},
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix))
    started = time.monotonic()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

    created = await nc.create_locked_channel(client, name="Sandbox check: /sandbox", description="Sandbox live check C1",
                                             metadata=_channel_metadata(route, "conversation"))
    channel = int(created["chat_channel_id"])
    message = await nc.create_message(client, channel, f"/sandbox python {SLASH_CODE}")
    run["slash"] = {"channel_id": channel, "request_id": int(message["chat_request_id"]), "sent_at": time.time()}
    try:
        await nc.wait_for_request(client, run["slash"]["request_id"], timeout_seconds=SLASH_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError) as error:
        run["slash"]["timeout"] = str(error) or "request timeout"

    created = await nc.create_locked_channel(client, name="Sandbox check: supervised", description="Sandbox live check M",
                                             metadata=_channel_metadata(route, "supervised"))
    channel = int(created["chat_channel_id"])
    message = await nc.create_message(client, channel, PROMPT)
    entry: dict[str, Any] = {"channel_id": channel, "request_id": int(message["chat_request_id"]),
                             "sent_at": time.time(), "decisions": []}
    run["supervised"] = entry
    while True:
        remaining = LIMITS["minutes"] * 60 - (time.monotonic() - started)
        try:
            snapshot = await nc.wait_for_request(client, entry["request_id"], timeout_seconds=max(1.0, remaining),
                                                 stop_on_input_requested=True)
        except (TimeoutError, asyncio.TimeoutError):
            snapshot = None
        stop = ("a Mythic task was created" if await _new_tasks(client, run["task_baseline"])
                else _over_limit(run, phoenix, started) or ("time limit" if snapshot is None else ""))
        if stop:
            entry["stopped"] = stop
            await nc.stop_native_chat_request(client, channel_id=channel, request_id=entry["request_id"])
            break
        if str((snapshot["request"] or {}).get("status") or "").casefold() in nc.TERMINAL_STATUSES:
            break
        pending = nc._pending_input_requested_messages(snapshot["messages"])
        if pending and pending[0].get("id") in {d["row"] for d in entry["decisions"]}:
            await asyncio.sleep(2)  # Mythic has not yet marked the answered card resolved
            continue
        data = ((pending[0].get("metadata") or {}).get("input_requested") or {}).get("data") or {} if pending else {}
        names = [action.get("tool_name") for action in data.get("actions") or []] or [data.get("tool_name")]
        if names and all(name == "sandbox_exec" for name in names):
            await nc.approve_pending_input_card(client, entry["request_id"])
            entry["decisions"].append({"row": pending[0].get("id"), "tools": names, "decision": "accept"})
        else:
            await nc.reject_pending_input_card(client, entry["request_id"])
            entry["decisions"].append({"row": pending[0].get("id") if pending else None, "tools": names,
                                       "decision": "reject"})
        await asyncio.sleep(2)
    entry["finished_at"] = time.time()
    run["finished_at"], run["phoenix_end"] = time.time(), base.phoenix_mark(phoenix)
    run["docker_after"] = docker_state()
    logs = subprocess.run(["docker", "logs", "--since", since, "sage"], capture_output=True, text=True, timeout=60)
    run["sandbox_log_lines"] = [line[-300:] for line in (logs.stdout + logs.stderr).splitlines() if "Sandbox" in line][-20:]
    return {"slash": run["slash"], "supervised": entry}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verify"):
        raise SystemExit("this run was already read; the result is read once")
    phoenix = Path(args.phoenix_db)
    slash = slash_outcome(await _rows(client, int(run["slash"]["request_id"])))
    tasks = await _new_tasks(client, int(run["task_baseline"]))
    supervised = supervised_outcome(await _rows(client, int(run["supervised"]["request_id"])), tasks, _guarded_tools())
    criteria = {"C1": slash["C1"], "M1": supervised["M1"], "M2": supervised["M2"], "M3": supervised["M3"],
                "D": docker_holds((run.get("readiness") or {}).get("docker") or {}, run.get("docker_after") or {})}
    calls, tokens = _usage(phoenix, int(run.get("phoenix_start", 0)))
    result = {"verdict": "pass" if all(criteria.values()) else "fail", "criteria": criteria,
              "detail": {"C1": slash["detail"], "supervised": supervised["detail"],
                         "decisions": run["supervised"].get("decisions"), "stopped": run["supervised"].get("stopped")},
              "observations": {"closing_states_both_values": supervised["closing_states_both"],
                               "provider_calls": calls, "total_tokens": tokens,
                               "wall_seconds": round(float(run["finished_at"]) - float(run["started_at"]), 1),
                               "sandbox_log_lines": run.get("sandbox_log_lines")},
              "verified_at": time.time()}
    run["verify"] = result
    return result


async def _main(args: argparse.Namespace) -> int:
    run_path = Path(args.run_file)
    run = _base()._load(run_path)
    client = await _base().nc.login()
    handlers = {"readiness": cmd_readiness, "run": cmd_run, "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        _base()._save(run_path, run)
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
            command.add_argument("--route-file", required=True,
                                 help="owner-only KEY=VALUE file with API_ENDPOINT (the gateway) and MODEL (no key)")
        if name == "readiness":
            command.add_argument("--phoenix-url", default="http://127.0.0.1:6006")
        if name == "run":
            command.add_argument("--plan", required=True, help="the line's plan file, for sage-line check-attempt")
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this bounded attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
