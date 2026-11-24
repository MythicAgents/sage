#!/usr/bin/env python3
"""Live check: the deployed container's new MCP client serves BloodHound through native slash dispatch.

The line moved Sage's MCP client from langchain-mcp-adapters to langchain.mcp on MCP 2 and FastMCP, and Phoenix to
20.21.0. Slash dispatch reaches the client without a model, so this check makes no provider call. One existing
channel, two requests, each terminal before the next. One run file (JSON, owner-only) carries every stage.

  readiness --run-file F --phoenix-db P --channel-id N [--phoenix-url U]
      Read-only. Container deployment parity with this branch (the files match and the process is newer than them);
      the running container uses the local `sage:latest` image; the container's installed packages equal this
      branch's constraints.txt, with strawberry-graphql 0.327.7 and no langchain-mcp-adapters; Sage consuming; no
      open chat requests; the channel's latest request terminal and its configuration carrying BLOODHOUND_URL,
      BLOODHOUND_TOKEN_ID and BLOODHOUND_TOKEN_KEY (presence only); BloodHound CE answering HTTP 200 at that URL
      (status only); Phoenix readable and answering /healthz; the Mythic task baseline. Verdict: ready_for_live,
      deployment_required or not_ready with blockers.
  run --run-file F --phoenix-db P --channel-id N --plan PLAN --authorized-by TEXT [--phoenix-url U]
      Runs `sage-line check-attempt PLAN` first and refuses on failure. Marks the Mythic task and Phoenix span
      high-water marks, sends B1 `/bloodhound`, waits for its terminal row, then sends B2 `/mcp tools BloodHound`.
      Both share one five-minute budget; on exhausting it the check stops that request. It stops between requests on
      a new target task. No retry, no upload.
  verify --run-file F --phoenix-db P
      Applies the pass rules below once, reading Mythic's rows and the live Phoenix spans in place.

Pass rules (frozen with the plan's live check; read once):
  L1 B1 ends `complete` by itself and its answer reports the ingest status read ready: "**Ingest:** Ready. The
     connected BloodHound MCP answered an exact-job ingest status read."
  L2 B2 ends `complete` by itself and its answer lists exactly one server, `BloodHound`, under which `file_upload`,
     `domain_info` and `cypher_query` each appear exactly once.
  L3 Zero Mythic tasks were created across the set.
  L4 Zero LLM spans were recorded across the set.
  L5 The set ran to its end inside its limits: both requests sent once, neither stopped by the check, inside the
     five-minute budget.
If B1 was never sent, the check did not run (verdict did_not_run): it is not an attempt.
Observations, not pass criteria: wall time per request, the B1 connection line, the B2 tool count, Phoenix /healthz at
each probe, non-LLM spans in the window.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = REPO_ROOT / "Payload_Type" / "sage"
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"

PROMPTS = {"B1": "/bloodhound", "B2": "/mcp tools BloodHound"}
BUDGET_SECONDS = 300
SETTLE_SECONDS = 3
SERVER = "BloodHound"
REQUIRED_TOOLS = ("file_upload", "domain_info", "cypher_query")
INGEST_READY = "**Ingest:** Ready. The connected BloodHound MCP answered an exact-job ingest status read."
BLOODHOUND_KEYS = ("BLOODHOUND_URL", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY")
EXPECTED_PACKAGES = {"strawberry-graphql": "0.327.7", "arize-phoenix": "20.21.0", "mcp": "2.3.0",
                     "langchain": "1.4.6", "langgraph": "1.2.22"}
RETIRED_ADAPTER = "langchain-mcp-adapters"
TOOLING = {"pip", "setuptools", "wheel", "uv"}
HEADING = re.compile(r"^\*\*(?P<server>.+?)\*\* — (?P<count>\d+) tool\(s\)$")
TOOL_LINE = re.compile(r"^- `(?P<tool>[^`]+)`")
LATEST_REQUEST_QUERY = """
query SageMcpCheckLatest($cid: Int!) {
  chat_request(where: {channel_id: {_eq: $cid}}, order_by: {id: desc}, limit: 1) { id status }
}
"""
REQUEST_ROWS_QUERY = """
query SageMcpCheckRows($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message }
  chat_request_by_pk(id: $rid) { id status }
}
"""


def _module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _base():
    """Shared native-chat, Mythic and Phoenix helpers of the OpenAI Responses check."""
    return _module("sage_mcp_check_base", _SCRIPTS / "check_openai_responses.py")


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.lower())


# --------------------------------------------------------------------------------------------------
# Readiness probes (read-only)
# --------------------------------------------------------------------------------------------------

def _run_json(command: list[str], timeout: int = 300) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout)
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip().startswith("{") else {}
    except ValueError:
        payload = {}
    return {"returncode": completed.returncode, "payload": payload}


def _docker(*args: str) -> str:
    completed = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def container_image() -> dict[str, Any]:
    running, latest = _docker("inspect", "--format", "{{.Image}}", "sage"), _docker(
        "image", "inspect", "--format", "{{.Id}}", "sage:latest")
    return {"running_image": running, "sage_latest": latest, "matches": bool(running) and running == latest,
            "sage_latest_created": _docker("image", "inspect", "--format", "{{.Created}}", "sage:latest")}


def container_packages() -> dict[str, Any]:
    installed = {}
    for line in _docker("exec", "sage", "python3", "-m", "pip", "list", "--format=freeze",
                        "--disable-pip-version-check").splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            installed[_name(name)] = version.strip()
    pins = {}
    for line in (RUNTIME / "constraints.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            name, version = line.split("==", 1)
            pins[_name(name)] = version.strip()
    differing = {name: [version, pins.get(name)] for name, version in installed.items()
                 if name not in TOOLING and pins.get(name) != version}
    return {"count": len(installed), "differing_from_constraints": differing,
            "expected": {name: installed.get(name) == version for name, version in EXPECTED_PACKAGES.items()},
            "retired_adapter_installed": RETIRED_ADAPTER in installed}


def http_status(url: str, timeout: float = 5) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except Exception:  # noqa: BLE001 - any other failure is "no answer"
        return None


def channel_bloodhound(config: dict[str, Any]) -> dict[str, Any]:
    """Presence of the channel's BloodHound settings and CE's HTTP status at its URL; no value is recorded."""
    present = {key: bool(str(config.get(key) or "").strip()) for key in BLOODHOUND_KEYS}
    url = str(config.get("BLOODHOUND_URL") or "").strip()
    return {"settings_present": present, "ce_status": http_status(url.rstrip("/") + "/") if url else None}


def llm_spans(db: Path, after: int, until: int) -> dict[str, int]:
    with _base()._phoenix(db) as conn:
        rows = conn.execute("SELECT span_kind, COUNT(*) FROM spans WHERE id > ? AND id <= ? GROUP BY span_kind",
                            (after, until)).fetchall()
    return {str(kind): int(count) for kind, count in rows}


# --------------------------------------------------------------------------------------------------
# Pass rules (pure; tested offline)
# --------------------------------------------------------------------------------------------------

def answer(rows: dict[str, Any]) -> str:
    return "\n".join(str(m.get("message") or "") for m in rows.get("messages", []) if m.get("author_type") == "ai")


def listed_tools(text: str) -> dict[str, dict[str, int]]:
    """Each server heading in a `/mcp tools` answer and how often each tool is listed under it."""
    servers: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None
    for line in text.splitlines():
        line = line.strip()
        if heading := HEADING.match(line):
            current = servers.setdefault(heading["server"], {})
        elif (tool := TOOL_LINE.match(line)) and current is not None:
            current[tool["tool"]] = current.get(tool["tool"], 0) + 1
    return servers


def evaluate(run: dict[str, Any], rows: dict[str, dict[str, Any]], spans_by_kind: dict[str, int],
             tasks_after: int) -> dict[str, Any]:
    requests = run.get("requests") or {}
    b1, b2 = rows.get("B1") or {}, rows.get("B2") or {}
    servers = listed_tools(answer(b2))
    criteria = {
        "L1": b1.get("status") == "complete" and INGEST_READY in answer(b1),
        "L2": b2.get("status") == "complete" and list(servers) == [SERVER]
        and all(servers[SERVER].get(tool) == 1 for tool in REQUIRED_TOOLS),
        "L3": tasks_after == run.get("task_baseline") and not run.get("stopped_for_task"),
        "L4": spans_by_kind.get("LLM", 0) == 0,
        "L5": set(requests) == set(PROMPTS) and not run.get("stopped")
        and not any(info.get("timeout") for info in requests.values())
        and float(run.get("finished_at", 0)) - float(run.get("started_at", 0)) <= BUDGET_SECONDS,
    }
    connection = next((line for line in answer(b1).splitlines() if line.startswith("**Connection:**")), "")
    observations = {
        "request_ids": {name: info.get("request_id") for name, info in requests.items()},
        "request_status": {name: (rows.get(name) or {}).get("status") for name in requests},
        "wall_seconds": {name: info.get("wall_seconds") for name, info in requests.items()},
        "b1_connection_line": connection[:300],
        "b2_servers": {server: sum(tools.values()) for server, tools in servers.items()},
        "spans_by_kind": spans_by_kind, "phoenix_health": run.get("phoenix_health"), "stopped": run.get("stopped"),
        "answers": {name: answer(rows.get(name) or {}) for name in requests},
    }
    verdict = "did_not_run" if "B1" not in requests else ("pass" if all(criteria.values()) else "fail")
    return {"criteria": criteria, "observations": observations, "verdict": verdict}


# --------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------

async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    parity = _run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container", "parity"])
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    stored = (await base._query(client, base.CHANNEL_QUERY, id=args.channel_id)).get("chat_channel_by_pk") or {}
    latest = ((await base._query(client, LATEST_REQUEST_QUERY, cid=args.channel_id)).get("chat_request") or [{}])[0]
    phoenix = Path(args.phoenix_db)
    report = {
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "image": container_image(),
        "container_packages": container_packages(),
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "channel": {"id": args.channel_id, "exists": bool(stored), "latest_request": latest,
                    **channel_bloodhound(((stored.get("ai_metadata") or {}).get("config") or {}))},
        "phoenix_readable": phoenix.is_file(),
        "phoenix_health": http_status(args.phoenix_url.rstrip("/") + "/healthz"),
        "task_baseline": await base._max_task(client),
        "prompts": PROMPTS, "budget_seconds": BUDGET_SECONDS,
    }
    blockers = []
    if not report["image"]["matches"]:
        blockers.append("the running container is not the local sage:latest image")
    packages = report["container_packages"]
    if packages["differing_from_constraints"] or not all(packages["expected"].values()) \
            or packages["retired_adapter_installed"]:
        blockers.append("container packages differ from this branch's constraints")
    if not report["sage_container_running"]:
        blockers.append("Sage container not consuming")
    if report["open_requests"]:
        blockers.append("open chat requests")
    channel = report["channel"]
    if not channel["exists"]:
        blockers.append("the channel does not exist")
    elif latest and latest.get("status") not in {"complete", "error", "cancelled"}:
        blockers.append("the channel's latest request is not terminal")
    if not all(channel["settings_present"].values()):
        blockers.append("the channel lacks a BloodHound setting")
    if channel["ce_status"] != 200:
        blockers.append("BloodHound CE did not answer HTTP 200")
    if not report["phoenix_readable"] or report["phoenix_health"] != 200:
        blockers.append("Phoenix unreadable or not answering /healthz")
    verdict = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    readiness = run.get("readiness") or {}
    if readiness.get("verdict") != "ready_for_live":
        raise SystemExit("readiness has not passed in this run file")
    if readiness.get("channel", {}).get("id") != args.channel_id:
        raise SystemExit("readiness checked a different channel")
    if run.get("requests"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    if gate.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate.stdout.strip()} {gate.stderr.strip()}".strip())
    base, nc = _base(), _base().nc
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, channel_id=args.channel_id, started_at=time.time(),
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix), requests={},
               phoenix_health=[http_status(args.phoenix_url.rstrip("/") + "/healthz")])
    deadline = time.monotonic() + BUDGET_SECONDS
    for name, prompt in PROMPTS.items():
        sent = time.monotonic()
        created = await nc.create_message(client, args.channel_id, prompt)
        entry: dict[str, Any] = {"request_id": int(created["chat_request_id"])}
        run["requests"][name] = entry
        try:
            await nc.wait_for_request(client, entry["request_id"], timeout_seconds=max(1.0, deadline - sent),
                                      poll_interval_seconds=2.0)
        except (TimeoutError, asyncio.TimeoutError) as error:
            entry["timeout"] = str(error) or "budget exhausted"
            await nc.stop_native_chat_request(client, channel_id=args.channel_id, request_id=entry["request_id"])
        entry["wall_seconds"] = round(time.monotonic() - sent, 1)
        await asyncio.sleep(SETTLE_SECONDS)
        entry["tasks_after"] = await base._max_task(client)
        run["phoenix_health"].append(http_status(args.phoenix_url.rstrip("/") + "/healthz"))
        if entry["tasks_after"] != run["task_baseline"]:
            run["stopped"], run["stopped_for_task"] = {"request": name, "reason": "a target task"}, True
        elif entry.get("timeout"):
            run["stopped"] = {"request": name, "reason": entry["timeout"]}
        if run.get("stopped"):
            break
    run["finished_at"] = time.time()
    run["phoenix_end"] = base.phoenix_mark(phoenix)
    return {"channel_id": args.channel_id, "requests": run["requests"], "stopped": run.get("stopped")}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if "verify" in run:
        raise SystemExit("this run was already read; the result is read once")
    base = _base()
    requests = run.get("requests") or {}
    rows = {}
    for name, info in requests.items():
        data = await base._query(client, REQUEST_ROWS_QUERY, rid=int(info["request_id"]))
        rows[name] = {"status": (data.get("chat_request_by_pk") or {}).get("status"),
                      "messages": list(data.get("chat_message") or [])}
    spans_by_kind = (llm_spans(Path(args.phoenix_db), int(run["phoenix_start"]), int(run["phoenix_end"]))
                     if "phoenix_start" in run and "phoenix_end" in run else {})
    result = evaluate(run, rows, spans_by_kind, await base._max_task(client))
    run["verify"] = result
    return result


async def _main(args: argparse.Namespace) -> int:
    base = _base()
    run_path = Path(args.run_file)
    run = base._load(run_path)
    if run.get("steps") == {}:  # the shared loader's empty default; this check keeps requests, not steps
        del run["steps"]
    client = await base.nc.login()
    handlers = {"readiness": cmd_readiness, "run": cmd_run, "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        base._save(run_path, run)
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
            command.add_argument("--channel-id", type=int, required=True,
                                 help="an existing idle channel carrying the BloodHound settings")
            command.add_argument("--phoenix-url", default="http://127.0.0.1:6006",
                                 help="the deployed Sage's Phoenix, for /healthz")
        if name == "run":
            command.add_argument("--plan", required=True, help="the line's plan file, for sage-line check-attempt")
            command.add_argument("--authorized-by", required=True, help="the operator's authorization for this attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
