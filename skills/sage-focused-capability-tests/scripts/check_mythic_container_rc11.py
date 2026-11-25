#!/usr/bin/env python3
"""Live check: Sage on mythic-container 0.7.0rc11 against a Mythic server at Mythic-v4.0.0 d8a94943 or later.

rc11 sends a channel-metadata update with no channel identity; the server takes the channel from the request being
answered and keeps only `items` (and `default_display`) from it. Sage's chips already are `items`; the Watcher's
applied marker moved into one `items` entry keyed `watcher`. This check deploys nothing. It runs one native set on the
deployed container and reads Mythic's own rows. One run file (JSON, owner-only) carries every stage.

  readiness --run-file F --route-file R --phoenix-db P --callback N [--phoenix-url U]
      Read-only. Deployment parity with this branch; Sage consuming; no open chat requests; the container's installed
      packages equal this branch's constraints.txt (mythic-container 0.7.0rc11, dynaconf 3.2.13, ujson 5.13.0); the
      lab Mythic checkout contains d8a94943, builds mythic_server from its build context, and the running
      mythic_server image is newer than the checkout's HEAD commit; no `Sage Watcher` channel exists in the
      operation; callback N is active; Phoenix readable and answering /healthz; the Mythic task baseline. The route
      credential, by name and status only (as the dependency-advisories check): exactly one active provider block in
      the installed .env.local, a keyed verified-TLS GET /models from inside the container, no shadowing user
      secret. The Watcher route by name only: SAGE_WATCHER_PROVIDER, SAGE_WATCHER_MODEL, SAGE_WATCHER_API_ENDPOINT,
      SAGE_WATCHER_API_KEY and SAGE_WATCHER_APITOKEN active in the installed .env.local, provider openai, the route's
      model and endpoint. Verdict: ready_for_live, deployment_required or not_ready with blockers.
  run --run-file F --route-file R --phoenix-db P --callback N --plan PLAN --authorized-by TEXT --pip-audit BIN
      Runs `sage-line check-attempt PLAN` first and refuses on failure. Records A1 (the container's installed set,
      `pip check`, and pip-audit of that set). Then, each request terminal before the next:
        C (Conversation): L1 "List the callbacks Mythic has right now, with host and user."
        S (Supervised, Autonomous Solve off): L2 "Run whoami on callback N." The first approval card is accepted
          (approve-pending); any later card is rejected.
        W (locked Sage Watcher, empty channel config): `/watcher apply`, `/watcher status`; one Sage restart
          (`docker restart sage`: same container and image, no rebuild); `/watcher status`; `/watcher scan`.
      It stops between requests on a second target task, a limit or a wrong model. No retry.
  verify --run-file F --phoenix-db P
      Applies the pass rules below once, reading Mythic's rows, channel metadata and tasks, and the live Phoenix
      spans in place.

Pass rules (frozen with the plan's live check; read once):
  A1 The container's installed set shows mythic-container 0.7.0rc11, dynaconf 3.2.13 and ujson 5.13.0, `pip check`
     is clean, and pip-audit of the installed set reports zero findings.
  L1 C's request ends `complete` with a native answer. C's stored `channel_metadata.items` is non-empty, with
     `updated_at` no earlier than the request start (second resolution).
  L2 S raised exactly one approval card, accepted. Exactly one new Mythic task exists, on callback N, completed, with
     at least one response; S's request ends `complete`. No card means L2 was not exercised: the attempt cannot pass.
  L3 `/watcher apply` reports generation g. W's stored `channel_metadata.items` holds exactly one `watcher` entry
     whose value's generation is g. `/watcher status` shows owner W, generation g, and a state other than
     `stale-generation`.
  L4 After the restart, `/watcher status` in W shows owner W and generation g, and a state that is neither
     `stale-generation` nor `controller-missing`.
  L5 `/watcher scan` completes, its scan count is above L4's, and its reply has no denial and no scope or
     authorization error.
  L6 The set stayed inside its limits: 3 channels, at most 20 provider calls and 300,000 tokens, 1 target task,
     30 minutes, one Sage restart, no stop by the check, no retry.
If L1 was never sent, the check did not run (verdict did_not_run): it is not an attempt.
Observations, not pass criteria: how the W chip renders in the UI, Phoenix /healthz at each probe, the Watcher's
background scans and #sage-findings delivery, Apollo check-ins, token use and wall time per request.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = REPO_ROOT / "Payload_Type" / "sage"
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"

MIN_MYTHIC_COMMIT = "d8a94943"
EXPECTED_PACKAGES = {"mythic-container": "0.7.0rc11", "dynaconf": "3.2.13", "ujson": "5.13.0"}
LIMITS = {"channels": 3, "provider_calls": 20, "total_tokens": 300_000, "target_tasks": 1, "active_minutes": 30,
          "restarts": 1}
PROMPTS = {"L1": "List the callbacks Mythic has right now, with host and user.",
           "L2": "Run whoami on callback {callback}."}
TIMEOUTS = {"L1": 300, "L2": 600, "W": 120}
SETTLE_SECONDS = 3
RESTART_WAIT_SECONDS = 240
RESTORE_SETTLE_SECONDS = 20
WATCHER_ENV = ("SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT", "SAGE_WATCHER_API_KEY",
               "SAGE_WATCHER_APITOKEN")
NATIVE_TERMINAL = {"complete", "error"}
APPLY = re.compile(r"Watcher profile generation `(?P<generation>\d+)` applied from locked owner")
STATUS_ROW = re.compile(
    r"^\| `(?P<state>[^`]*)` \| `[^`]*` \(`(?P<owner>[^`]*)`\) \| (?P<generation>\d+) \|", re.MULTILINE)
SCANS = re.compile(r"· scans: (?P<scans>\d+) · last error: `(?P<error>[^`]*)`")
AUTH_ERROR = re.compile(r"scope|authori[sz]|permission|forbidden|denied|unauthori[sz]ed", re.IGNORECASE)

CHANNEL_QUERY = "query SageRc11Channel($id: Int!) { chat_channel_by_pk(id: $id) { id chat_model locked ai_metadata } }"
WATCHER_CHANNELS_QUERY = """
query SageRc11WatcherChannels { chat_channel(where: {chat_model: {_eq: "Sage Watcher"}}) { id archived } }
"""
CALLBACK_QUERY = """
query SageRc11Callback($cb: Int!) {
  callback(where: {display_id: {_eq: $cb}}) { display_id active payload { payloadtype { name } } }
}
"""
TASKS_AFTER_QUERY = """
query SageRc11Tasks($after: Int!) {
  task(where: {display_id: {_gt: $after}}, order_by: {display_id: asc}) {
    display_id command_name completed status callback { display_id } responses(limit: 1) { id }
  }
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
    return _module("sage_rc11_check_base", _SCRIPTS / "check_openai_responses.py")


def _dep():
    """Route, credential, package and Phoenix-span helpers of the dependency-advisories check."""
    return _module("sage_rc11_check_dependency", _SCRIPTS / "check_dependency_advisories.py")


def _reasoning():
    return _module("sage_rc11_check_reasoning", _SCRIPTS / "check_reasoning_tools.py")


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.lower())


# --------------------------------------------------------------------------------------------------
# Parsers and pass rules (pure; tested offline)
# --------------------------------------------------------------------------------------------------

def _texts(rows: dict[str, Any]) -> list[str]:
    return [str(m.get("message") or "").strip() for m in rows.get("messages", [])
            if str(m.get("author_type") or "") != "user" and str(m.get("message") or "").strip()]


def parse_apply(text: str) -> int | None:
    match = APPLY.search(text)
    return int(match["generation"]) if match else None


def parse_status(text: str) -> dict[str, Any] | None:
    row = STATUS_ROW.search(text)
    if not row:
        return None
    scans = SCANS.search(text)
    return {"state": row["state"], "owner": row["owner"], "generation": int(row["generation"]),
            "scans": int(scans["scans"]) if scans else None, "last_error": scans["error"] if scans else None}


def watcher_entries(channel_metadata: Any) -> list[Any]:
    items = channel_metadata.get("items") if isinstance(channel_metadata, dict) else None
    return [entry.get("value") for entry in (items if isinstance(items, list) else [])
            if isinstance(entry, dict) and entry.get("key") == "watcher"]


def _rfc3339(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def evaluate(run: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Apply A1 and L1–L6 to the run record and the rows `verify` read from Mythic and Phoenix."""
    requests = run.get("requests") or {}
    rows = observed.get("rows") or {}
    a1 = run.get("a1") or {}
    packages = a1.get("packages") or {}
    criteria: dict[str, bool] = {}
    criteria["A1"] = (all(packages.get(name) == version for name, version in EXPECTED_PACKAGES.items())
                      and not a1.get("differing_from_constraints") and a1.get("pip_check_returncode") == 0
                      and a1.get("audit_ran") is True and a1.get("audit_findings") == 0)

    l1_rows = rows.get("L1") or {}
    c_meta = observed.get("c_channel_metadata") or {}
    updated = _rfc3339(c_meta.get("updated_at"))
    l1_start = (requests.get("L1") or {}).get("sent_at")
    criteria["L1"] = (l1_rows.get("status") == "complete" and bool(_texts(l1_rows))
                      and isinstance(c_meta.get("items"), list) and bool(c_meta.get("items"))
                      and updated is not None and l1_start is not None and updated >= int(l1_start))

    l2 = requests.get("L2") or {}
    tasks = observed.get("tasks") or []
    callback = run.get("callback")
    task = tasks[0] if len(tasks) == 1 else {}
    l2_exercised = int(l2.get("cards_seen") or 0) >= 1
    criteria["L2"] = (l2_exercised and l2.get("cards_seen") == 1 and l2.get("cards_accepted") == 1
                      and len(tasks) == 1 and (task.get("callback") or {}).get("display_id") == callback
                      and task.get("completed") is True and bool(task.get("responses"))
                      and (rows.get("L2") or {}).get("status") == "complete")

    w_id = str((run.get("channels") or {}).get("W") or "")
    generation = parse_apply("\n".join(_texts(rows.get("L3_apply") or {})))
    entries = watcher_entries(observed.get("w_channel_metadata") or {})
    stored = entries[0] if len(entries) == 1 and isinstance(entries[0], dict) else {}
    l3_status = parse_status("\n".join(_texts(rows.get("L3_status") or {}))) or {}
    criteria["L3"] = (generation is not None and stored.get("generation") == generation
                      and (rows.get("L3_apply") or {}).get("status") == "complete"
                      and l3_status.get("owner") == w_id and l3_status.get("generation") == generation
                      and l3_status.get("state") not in {None, "stale-generation"})

    l4_status = parse_status("\n".join(_texts(rows.get("L4_status") or {}))) or {}
    criteria["L4"] = (run.get("restarts") == 1 and run.get("restart_ok") is True
                      and generation is not None and l4_status.get("owner") == w_id
                      and l4_status.get("generation") == generation
                      and l4_status.get("state") not in {None, "stale-generation", "controller-missing"})

    l5_text = "\n".join(_texts(rows.get("L5_scan") or {}))
    l5_status = parse_status(l5_text) or {}
    criteria["L5"] = ((rows.get("L5_scan") or {}).get("status") == "complete" and bool(l5_status)
                      and l5_status.get("scans") is not None and l4_status.get("scans") is not None
                      and l5_status["scans"] > l4_status["scans"]
                      and "Watcher control denied" not in l5_text
                      and not AUTH_ERROR.search(str(l5_status.get("last_error") or "")))

    llm = observed.get("llm_spans") or []
    tokens = sum(int(s.get("prompt_tokens") or 0) + int(s.get("completion_tokens") or 0) for s in llm)
    minutes = (float(run.get("finished_at") or 0) - float(run.get("started_at") or 0)) / 60
    criteria["L6"] = (len(run.get("channels") or {}) <= LIMITS["channels"]
                      and len(llm) <= LIMITS["provider_calls"] and tokens <= LIMITS["total_tokens"]
                      and len(tasks) <= LIMITS["target_tasks"] and 0 < minutes <= LIMITS["active_minutes"]
                      and run.get("restarts") == LIMITS["restarts"] and not run.get("stopped")
                      and not any(info.get("timeout") for info in requests.values())
                      and all(int(info.get("sends") or 1) == 1 for info in requests.values()))

    observations = {
        "provider_calls": len(llm), "total_tokens": tokens, "wall_minutes": round(minutes, 2),
        "request_status": {name: (rows.get(name) or {}).get("status") for name in rows},
        "apply_generation": generation, "stored_watcher_entries": len(entries),
        "status_before_restart": l3_status, "status_after_restart": l4_status, "status_after_scan": l5_status,
        "tasks": [{"display_id": t.get("display_id"), "callback": (t.get("callback") or {}).get("display_id"),
                   "completed": t.get("completed"), "status": t.get("status")} for t in tasks],
        "cards": {"seen": l2.get("cards_seen"), "accepted": l2.get("cards_accepted"),
                  "rejected": l2.get("cards_rejected")},
        "c_items": len(c_meta.get("items") or []) if isinstance(c_meta.get("items"), list) else None,
        "phoenix_health": run.get("phoenix_health"), "stopped": run.get("stopped"),
        "l2_exercised": l2_exercised,
    }
    if "L1" not in requests:
        verdict = "did_not_run"
    else:
        verdict = "pass" if all(criteria.values()) else "fail"
    return {"criteria": criteria, "observations": observations, "verdict": verdict}


# --------------------------------------------------------------------------------------------------
# Readiness probes (read-only)
# --------------------------------------------------------------------------------------------------

def _git(directory: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=60)


def _env_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key and not line.lstrip().startswith("#"):
            return value.strip().strip("\"'")
    return ""


def mythic_server_identity() -> dict[str, Any]:
    """The lab Mythic checkout and its running server: booleans and commit ids, nothing else."""
    deployment = _module("sage_rc11_check_deployment", _DEPLOYMENT_PATH)
    mythic = Path(deployment._mythic_dir())
    head = _git(mythic, "rev-parse", "HEAD").stdout.strip()
    contains = _git(mythic, "merge-base", "--is-ancestor", MIN_MYTHIC_COMMIT, "HEAD").returncode == 0
    head_time = _rfc3339(_git(mythic, "log", "-1", "--format=%cI", "HEAD").stdout.strip())
    image = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", "mythic_server"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    created = subprocess.run(["docker", "image", "inspect", "--format", "{{.Created}}", image],
                             capture_output=True, text=True, timeout=60).stdout.strip() if image else ""
    created_time = _rfc3339(re.sub(r"\.\d+", "", created)) if created else None
    build_context = _env_value(mythic / ".env", "MYTHIC_SERVER_USE_BUILD_CONTEXT").casefold() == "true"
    return {"head": head[:12], "contains_min_commit": contains, "server_build_context": build_context,
            "server_image_newer_than_head": bool(head_time and created_time and created_time >= head_time)}


def watcher_route(installed: Path, route: dict[str, str]) -> dict[str, Any]:
    """Watcher keys active in the installed .env.local, by name; route agreement as booleans."""
    path = installed / ".env.local"
    present = {key: bool(_env_value(path, key)) for key in WATCHER_ENV}
    dep = _dep()
    return {"present": present,
            "provider_openai": _env_value(path, "SAGE_WATCHER_PROVIDER").casefold() == "openai",
            "model_matches_route": _env_value(path, "SAGE_WATCHER_MODEL") == route["MODEL"],
            "endpoint_matches_route": dep._normalized(_env_value(path, "SAGE_WATCHER_API_ENDPOINT"))
            == dep._normalized(route["API_ENDPOINT"])}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, dep, reasoning = _base(), _dep(), _reasoning()
    route = dep.read_route(Path(args.route_file))
    deployment = _module("sage_rc11_check_deployment", _DEPLOYMENT_PATH)
    installed = Path(deployment._installed_service_dir())
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    parity = reasoning._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                                  "parity"])
    callback = (await base._query(client, CALLBACK_QUERY, cb=int(args.callback))).get("callback") or []
    watchers = (await base._query(client, WATCHER_CHANNELS_QUERY)).get("chat_channel") or []
    baseline = await base._max_task(client)
    shape = (await base._query(client, TASKS_AFTER_QUERY, after=max(0, baseline - 1))).get("task")
    packages = dep.container_packages()
    installed_now = installed_packages()
    phoenix = Path(args.phoenix_db)
    report = {
        "route": {"model": route["MODEL"], "endpoint": "from the route file (not recorded)"},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "container_packages": {"differing_from_constraints": packages["differing_from_constraints"],
                               **{name: installed_now.get(name) for name in EXPECTED_PACKAGES}},
        "mythic_server": mythic_server_identity(),
        "watcher_channels": len(watchers),
        "callback": {"found": len(callback) == 1, "active": bool(callback and callback[0].get("active")),
                     "payload_type": ((callback[0].get("payload") or {}).get("payloadtype") or {}).get("name")
                     if callback else None},
        "task_query_shape_ok": isinstance(shape, list),
        "phoenix_readable": phoenix.is_file(),
        "phoenix_health": dep.phoenix_health(args.phoenix_url),
        "task_baseline": baseline,
        "credential_source": {
            "installed": dep.installed_credentials(installed, route["API_ENDPOINT"]),
            "container": dep._container_key_probe(route),
            "operator_secrets": await reasoning._operator_secret_names(client),
            "channel_config_sets_key": False,
        },
        "watcher_route": watcher_route(installed, route),
        "limits": LIMITS,
    }
    blockers = []
    if not report["sage_container_running"]:
        blockers.append("Sage container not consuming")
    if report["open_requests"]:
        blockers.append("open chat requests")
    pk = report["container_packages"]
    if pk["differing_from_constraints"] or any(pk.get(n) != v for n, v in EXPECTED_PACKAGES.items()):
        blockers.append("container packages differ from this branch's constraints")
    server = report["mythic_server"]
    if not (server["contains_min_commit"] and server["server_build_context"] and server["server_image_newer_than_head"]):
        blockers.append(f"Mythic server is not built from a checkout containing {MIN_MYTHIC_COMMIT}")
    if report["watcher_channels"]:
        blockers.append("a Sage Watcher channel already exists in the operation")
    if not report["callback"]["active"]:
        blockers.append("the callback is not active")
    if not report["task_query_shape_ok"]:
        blockers.append("the task query was rejected")
    if not report["phoenix_readable"] or report["phoenix_health"] != 200:
        blockers.append("Phoenix unreadable or not answering /healthz")
    blockers.extend(dep.credential_blockers(report["credential_source"], route))
    wr = report["watcher_route"]
    if not all(wr["present"].values()) or not (wr["provider_openai"] and wr["model_matches_route"]
                                               and wr["endpoint_matches_route"]):
        blockers.append("the installed .env.local does not set the Watcher route and bot token for this route")
    verdict = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


def installed_packages() -> dict[str, str]:
    completed = subprocess.run(["docker", "exec", "sage", "python3", "-m", "pip", "list", "--format=freeze",
                                "--disable-pip-version-check"], capture_output=True, text=True, timeout=120)
    packages = {}
    for line in completed.stdout.splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            packages[_name(name)] = version.strip()
    return packages


def record_a1(pip_audit: str) -> dict[str, Any]:
    packages = installed_packages()
    pins = _dep()._branch_constraints()
    tooling = {"pip", "setuptools", "wheel", "uv"}
    differing = {n: [v, pins.get(n)] for n, v in packages.items() if n not in tooling and pins.get(n) != v}
    check = subprocess.run(["docker", "exec", "sage", "python3", "-m", "pip", "check", "--disable-pip-version-check"],
                           capture_output=True, text=True, timeout=120)
    with tempfile.TemporaryDirectory(prefix="sage-rc11-audit-") as scratch:
        listing = Path(scratch) / "installed.txt"
        listing.write_text("".join(f"{n}=={v}\n" for n, v in sorted(packages.items()) if n not in tooling))
        audit = subprocess.run([pip_audit, "-r", str(listing), "--no-deps", "--disable-pip", "--progress-spinner",
                                "off", "-f", "json"], capture_output=True, text=True, timeout=600)
    try:
        report = json.loads(audit.stdout)
        dependencies = report.get("dependencies", report) if isinstance(report, dict) else report
        findings = sum(len(d.get("vulns") or []) for d in dependencies if isinstance(d, dict))
        audited = len(dependencies)
        ran = audited > 0
    except (ValueError, TypeError):
        findings, audited, ran = None, 0, False
    return {"packages": {n: packages.get(n) for n in EXPECTED_PACKAGES}, "count": len(packages),
            "differing_from_constraints": differing, "pip_check_returncode": check.returncode,
            "audit_ran": ran, "audit_packages": audited, "audit_findings": findings,
            "audit_returncode": audit.returncode}


# --------------------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------------------

async def _send(client: Any, run: dict[str, Any], name: str, channel_id: int, prompt: str, timeout: int,
                *, cards: bool = False) -> dict[str, Any]:
    nc = _base().nc
    entry: dict[str, Any] = {"channel_id": channel_id, "sent_at": time.time(), "sends": 1}
    message = await nc.create_message(client, channel_id, prompt)
    request_id = int(message["chat_request_id"])
    entry["request_id"] = request_id
    if cards:
        entry.update(cards_seen=0, cards_accepted=0, cards_rejected=0)
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = max(1.0, deadline - time.monotonic())
            result = await nc.wait_for_request(client, request_id, timeout_seconds=remaining,
                                               stop_on_input_requested=cards)
            if result.get("halt_reason") != "operator_input_requested":
                break
            entry["cards_seen"] += 1
            if entry["cards_seen"] == 1:
                await nc.approve_pending_input_card(client, request_id)
                entry["cards_accepted"] += 1
            else:
                await nc.reject_pending_input_card(client, request_id)
                entry["cards_rejected"] += 1
    except (TimeoutError, asyncio.TimeoutError) as error:
        entry["timeout"] = str(error) or "request timeout"
        await nc.stop_native_chat_request(client, channel_id=channel_id, request_id=request_id)
    await asyncio.sleep(SETTLE_SECONDS)
    entry["finished_at"] = time.time()
    run["requests"][name] = entry
    return entry


async def _new_tasks(client: Any, baseline: int) -> list[dict[str, Any]]:
    return list((await _base()._query(client, TASKS_AFTER_QUERY, after=baseline)).get("task") or [])


def _over_limit(run: dict[str, Any], phoenix: Path, started: float) -> str:
    dep = _dep()
    llm = dep._llm(dep.spans(phoenix, run["phoenix_start"], _base().phoenix_mark(phoenix)))
    if len(llm) > LIMITS["provider_calls"]:
        return "provider call limit"
    if sum(s["prompt_tokens"] + s["completion_tokens"] for s in llm) > LIMITS["total_tokens"]:
        return "token limit"
    if any(s["model"] and s["model"] != run["route"]["MODEL"] for s in llm):
        return "a span names another model"
    if time.monotonic() - started > LIMITS["active_minutes"] * 60:
        return "active time limit"
    return ""


async def _restart_sage(client: Any) -> bool:
    """One Sage restart: the same container and image (no rebuild); wait until Mythic sees it consuming again."""
    base = _base()
    restarted = subprocess.run(["docker", "restart", "sage"], capture_output=True, text=True, timeout=180)
    if restarted.returncode != 0:
        return False
    deadline = time.monotonic() + RESTART_WAIT_SECONDS
    await asyncio.sleep(10)
    while time.monotonic() < deadline:
        rows = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
        if rows and rows[0].get("container_running"):
            await asyncio.sleep(RESTORE_SETTLE_SECONDS)
            return True
        await asyncio.sleep(5)
    return False


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness has not passed in this run file")
    if run.get("requests"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    if gate.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate.stdout.strip()} {gate.stderr.strip()}".strip())
    base, dep = _base(), _dep()
    route = dep.read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, callback=int(args.callback), route={"MODEL": route["MODEL"]},
               a1=record_a1(args.pip_audit), requests={}, channels={}, restarts=0,
               phoenix_health=[dep.phoenix_health(args.phoenix_url)])
    run.update(started_at=time.time(), task_baseline=await base._max_task(client),
               phoenix_start=base.phoenix_mark(phoenix))
    started = time.monotonic()

    async def channel(key: str, mode: str | None) -> int:
        if mode is None:
            created = await base.nc.create_locked_channel(
                client, name="rc11 live check (Sage Watcher)", description="Sage mythic-container rc11 live check",
                model="Sage Watcher", metadata={"config": {}})
        else:
            created = await base.nc.create_locked_channel(
                client, name=f"rc11 live check ({mode})", description="Sage mythic-container rc11 live check",
                metadata=dep.channel_config(route, mode))
        run["channels"][key] = int(created["chat_channel_id"])
        return run["channels"][key]

    def stop_if_needed(name: str, tasks: list[dict[str, Any]]) -> bool:
        entry = run["requests"].get(name) or {}
        if len(tasks) > LIMITS["target_tasks"]:
            run["stopped"] = {"request": name, "reason": "a second target task"}
        elif reason := _over_limit(run, phoenix, started):
            run["stopped"] = {"request": name, "reason": reason}
        elif entry.get("timeout"):
            run["stopped"] = {"request": name, "reason": entry["timeout"]}
        return bool(run.get("stopped"))

    steps = [
        ("L1", "C", "conversation", PROMPTS["L1"], TIMEOUTS["L1"], False),
        ("L2", "S", "supervised", PROMPTS["L2"].format(callback=int(args.callback)), TIMEOUTS["L2"], True),
        ("L3_apply", "W", None, "/watcher apply", TIMEOUTS["W"], False),
        ("L3_status", "W", None, "/watcher status", TIMEOUTS["W"], False),
        ("RESTART", "W", None, "", 0, False),
        ("L4_status", "W", None, "/watcher status", TIMEOUTS["W"], False),
        ("L5_scan", "W", None, "/watcher scan", TIMEOUTS["W"], False),
    ]
    for name, key, mode, prompt, timeout, cards in steps:
        if name == "RESTART":
            run["restarts"] += 1
            run["restart_ok"] = await _restart_sage(client)
            run["phoenix_health"].append(dep.phoenix_health(args.phoenix_url))
            if not run["restart_ok"]:
                run["stopped"] = {"request": name, "reason": "Sage did not return after the restart"}
                break
            continue
        channel_id = run["channels"].get(key) or await channel(key, mode)
        await _send(client, run, name, channel_id, prompt, timeout, cards=cards)
        run["phoenix_health"].append(dep.phoenix_health(args.phoenix_url))
        if stop_if_needed(name, await _new_tasks(client, run["task_baseline"])):
            break
    run["finished_at"] = time.time()
    run["phoenix_end"] = base.phoenix_mark(phoenix)
    return {"channels": run["channels"], "requests": run["requests"], "a1": run["a1"], "stopped": run.get("stopped")}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if "verify" in run:
        raise SystemExit("this run was already read; the result is read once")
    base, dep = _base(), _dep()
    requests = run.get("requests") or {}
    rows = {name: await base.request_rows(client, int(info["request_id"])) for name, info in requests.items()}

    async def stored(key: str) -> dict[str, Any]:
        channel_id = (run.get("channels") or {}).get(key)
        if not channel_id:
            return {}
        row = (await base._query(client, CHANNEL_QUERY, id=int(channel_id))).get("chat_channel_by_pk") or {}
        metadata = (row.get("ai_metadata") or {}).get("channel_metadata")
        return metadata if isinstance(metadata, dict) else {}

    phoenix = Path(args.phoenix_db)
    observed = {
        "rows": rows,
        "c_channel_metadata": await stored("C"),
        "w_channel_metadata": await stored("W"),
        "tasks": await _new_tasks(client, int(run.get("task_baseline") or 0)) if requests else [],
        "llm_spans": dep._llm(dep.spans(phoenix, run["phoenix_start"], run["phoenix_end"])) if requests else [],
    }
    result = evaluate(run, observed)
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
            command.add_argument("--route-file", required=True)
            command.add_argument("--callback", required=True, type=int, help="the live Apollo callback display id")
            command.add_argument("--phoenix-url", default="http://127.0.0.1:6006",
                                 help="the deployed Sage's Phoenix, for /healthz")
        if name == "run":
            command.add_argument("--plan", required=True, help="the line's plan file, for sage-line check-attempt")
            command.add_argument("--authorized-by", required=True, help="the operator's authorization for this attempt")
            command.add_argument("--pip-audit", required=True, help="a pip-audit executable, for A1")
    return parser


def main() -> None:
    os.umask(0o077)
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
