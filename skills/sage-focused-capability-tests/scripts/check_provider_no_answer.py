#!/usr/bin/env python3
"""Live check: a model provider Sage cannot reach, or that never answers, is reported as such, not as a rejection.

The deployed Sage container (host networking) must run the branch under test with the node idle watchdog at its
300 s default. One run file (JSON, owner-only) carries every stage. Each segment runs in a fresh Conversation channel
with the prompt PROMPT:

  L1  blackholed endpoint: provider Custom (``openai_compatible``), API Endpoint BLACKHOLE, Model CHECK_MODEL, the
      chat's API Key field set to PLACEHOLDER_KEY, so no real key leaves Sage.
  L2  silent provider: the same, with API Endpoint ``http://127.0.0.1:<port>/v1``, a listener this script runs on the
      host that accepts, logs the request line (never a header) and never replies.
  L3  refusal survives: provider LiteLLM Gateway (``litellm``), the route file's gateway endpoint and the installed
      credential, Model MISSING_MODEL.

  readiness --run-file F --route-file R --phoenix-db P [--phoenix-url U]
      Read-only. Deployment parity (container mode) and host networking; SAGE_NODE_IDLE_TIMEOUT unset in the container
      and in the installed dotenv files; Sage consuming; no open chat requests; Phoenix readable and answering;
      the Mythic task baseline; this line's offline tests pass. From inside the container: BLACKHOLE's port 443
      times out (not refused) and a host loopback listener accepts and stays silent. The L3 credential, by name and
      status only: exactly one active provider block in the installed .env.local at the route file's gateway, a keyed
      verified-TLS GET /models from inside the container returns 200, lists the route file's MODEL and does not list
      MISSING_MODEL; no shadowing API_KEY user secret. Verdict: ready_for_live, deployment_required or not_ready.
  run --run-file F --route-file R --phoenix-db P --plan PLAN --authorized-by TEXT
      Runs `sage-line check-attempt PLAN` first and refuses on failure. Runs L1, L2, L3 in order. A segment whose gate
      is closed is retried once in a new channel, and no more. Stops on any new Mythic task, any LLM span that
      reports tokens, or the time limit. No other retry.
  verify --run-file F
      Applies the pass rules below once to each segment's first gated request, reading Mythic's rows and Sage's
      channel session records (read-only, in place).

Gates (the check ran only if every segment's gate opened; otherwise verdict not_run and the line cannot land):
  L1  Phoenix holds a Supervisor ``model`` node span in the request's window (the provider call was attempted).
  L2  The listener logged a POST to /v1/chat/completions or /v1/responses during the request.
  L3  The request's notice names an HTTP 4xx status (the gateway answered).

Pass rules (frozen with the plan's live check; read once). A request's notice is the text of every row Sage wrote for
it, tool cards excluded; elapsed is Mythic's request update time minus the operator message's creation time.
  P1 (L1) The request ends ``error`` within 90 s; the notice says the endpoint could not be reached and names
     BLACKHOLE, provider ``openai_compatible`` and model CHECK_MODEL.
  P2 (L2) The request ends ``error`` between 290 s and 360 s; the notice says the provider gave no answer, names a wait
     of at least 290 s, provider ``openai_compatible``, model CHECK_MODEL and the listener's endpoint.
  P3 (L1, L2) Neither notice contains "rejected", "**Model**", "`/model`" or PLACEHOLDER_KEY; each channel's session
     record is still active.
  P4 (L3) The notice says "rejected", names an HTTP 4xx status and advises **Model**, and contains neither "could not
     be reached" nor "no answer".
Observations, not pass criteria: time to terminal per segment, notice wording, provider calls, listener requests.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"

PROMPT = "Say hello in one word."
BLACKHOLE = "https://198.51.100.1/v1"
CHECK_MODEL = "no-answer-check"
MISSING_MODEL = "sage-no-such-model"
PLACEHOLDER_KEY = "sage-no-answer-check-placeholder"
SEGMENTS = ("L1", "L2", "L3")
LIMITS = {"requests": 3, "gate_retries_per_segment": 1, "inference_tokens": 0, "minutes": 15, "target_tasks": 0}
WAIT_SECONDS = {"L1": 150, "L2": 420, "L3": 150}  # the check stops a request still open after this
P1_MAX, P2_MIN, P2_MAX, P2_WAIT_MIN = 90, 290, 360, 290
MAX_STEPS = 2
SETTLE_SECONDS = 3
OFFLINE_TESTS = ("tests/test_provider_no_answer.py",)
FORBIDDEN = ("rejected", "**Model**", "`/model`", PLACEHOLDER_KEY)
PROVIDER_PATHS = ("/v1/chat/completions", "/v1/responses")
HTTP_4XX = re.compile(r"\bHTTP 4\d\d\b")
WAITED = re.compile(r"gave no answer within (\d+) seconds")
ROWS_QUERY = """
query SageNoAnswerRows($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message metadata status }
  chat_request_by_pk(id: $rid) { id status updated_at request_message_id }
}
"""
MESSAGE_QUERY = "query SageNoAnswerMessage($id: Int!) { chat_message_by_pk(id: $id) { id created_at } }"
TASKS_AFTER_QUERY = "query SageNoAnswerTasks($after: Int!) { task(where: {display_id: {_gt: $after}}) { display_id } }"

_BLACKHOLE_PROBE = r"""
import json, socket, sys
try:
    socket.create_connection((sys.argv[1], 443), timeout=15).close()
    print(json.dumps({"result": "connected"}))
except TimeoutError:
    print(json.dumps({"result": "timed out"}))
except OSError as error:
    print(json.dumps({"result": type(error).__name__, "errno": error.errno}))
"""
_LOOPBACK_PROBE = r"""
import json, socket, sys
sock = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
sock.sendall(b"GET /readiness-probe HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
sock.settimeout(3)
try:
    print(json.dumps({"accepted": True, "replied": bool(sock.recv(1))}))
except TimeoutError:
    print(json.dumps({"accepted": True, "replied": False}))
"""
_SESSION_PROBE = r"""
import json, os, sqlite3, sys
from urllib.parse import quote
sys.path.insert(0, "/Mythic")
from dotenv import dotenv_values
import dotenv_bootstrap as boot
for path in boot.dotenv_paths("/Mythic"):
    boot.apply_dotenv(dotenv_values(path), os.environ)
from runtime_db import database_path
conn = sqlite3.connect(f"file:{quote(str(database_path()), safe='/:')}?mode=ro", uri=True)
out = {}
for channel in sys.argv[1:]:
    row = conn.execute("SELECT payload FROM sage_chat_channel_sessions WHERE channel_id=?", (int(channel),)).fetchone()
    out[channel] = json.loads(row[0]).get("status") if row else None
print(json.dumps(out))
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
    """Native-chat, Mythic and Phoenix helpers of the OpenAI Responses check."""
    return _module("sage_no_answer_check_base", _SCRIPTS / "check_openai_responses.py")


def _dep():
    """Route, credential and Phoenix health helpers of the dependency-advisories check."""
    return _module("sage_no_answer_check_dependency", _SCRIPTS / "check_dependency_advisories.py")


def _reasoning():
    return _module("sage_no_answer_check_reasoning", _SCRIPTS / "check_reasoning_tools.py")


def _container_json(probe: str, *argv: str, timeout: int = 90) -> dict[str, Any]:
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", probe, *argv],
                               capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


# --------------------------------------------------------------------------------------------------
# The silent listener (L2 and readiness)
# --------------------------------------------------------------------------------------------------

class SilentListener:
    """Accepts on host loopback, logs each connection's request line and never replies."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        self.port = int(self.sock.getsockname()[1])
        self.log: list[dict[str, Any]] = []
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._held.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        data = b""
        try:
            while b"\r\n" not in data and len(data) < 8192:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        self.log.append({"at": time.time(), "request_line": data.split(b"\r\n", 1)[0].decode("latin-1")[:200]})

    def close(self) -> None:
        self._stop.set()
        for conn in [self.sock, *self._held]:
            try:
                conn.close()
            except OSError:
                pass


# --------------------------------------------------------------------------------------------------
# Phoenix (read-only, in place)
# --------------------------------------------------------------------------------------------------

def window_spans(db: Path, after: int, until: int) -> list[dict[str, Any]]:
    with _base()._phoenix(db) as conn:
        rows = conn.execute(
            "SELECT id, span_kind, name, attributes, COALESCE(llm_token_count_prompt, 0) + "
            "COALESCE(llm_token_count_completion, 0) FROM spans WHERE id > ? AND id <= ? ORDER BY id",
            (after, until)).fetchall()
    spans = []
    for span_id, kind, name, attributes, tokens in rows:
        try:
            attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
        except ValueError:
            attrs = {}
        metadata = attrs.get("metadata") or {}
        spans.append({"id": span_id, "kind": kind, "name": name, "tokens": int(tokens),
                      "agent": str(metadata.get("lc_agent_name") or ""), "node": str(metadata.get("langgraph_node") or "")})
    return spans


def supervisor_model_span(spans: list[dict[str, Any]]) -> int | None:
    return next((s["id"] for s in spans if s["name"] == "model" and s["agent"] == "Supervisor" and s["node"] == "model"),
                None)


# --------------------------------------------------------------------------------------------------
# Pass rules (pure)
# --------------------------------------------------------------------------------------------------

def notice_text(rows: dict[str, Any]) -> str:
    texts = []
    for row in rows.get("messages") or []:
        metadata = row.get("metadata") or {}
        if row.get("author_type") == "operator" or metadata.get("special_type") == "tool_use":
            continue
        text = str(row.get("message") or "").strip()
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def _clean(notice: str) -> bool:
    folded = notice.casefold()
    return not any(term.casefold() in folded for term in FORBIDDEN)


def evaluate(segments: dict[str, dict[str, Any]], listener_endpoint: str) -> dict[str, Any]:
    """``segments``: per segment, ``status``, ``elapsed`` (s), ``notice`` and ``session`` for its gated request."""
    l1, l2, l3 = (segments[name] for name in SEGMENTS)
    route = ("provider openai_compatible", f"`{CHECK_MODEL}`")
    waited = WAITED.search(l2["notice"])
    p1 = (l1["status"] == "error" and l1["elapsed"] is not None and l1["elapsed"] <= P1_MAX
          and "could not be reached" in l1["notice"] and BLACKHOLE in l1["notice"]
          and all(part in l1["notice"] for part in route))
    p2 = (l2["status"] == "error" and l2["elapsed"] is not None and P2_MIN <= l2["elapsed"] <= P2_MAX
          and waited is not None and int(waited.group(1)) >= P2_WAIT_MIN and listener_endpoint in l2["notice"]
          and all(part in l2["notice"] for part in route))
    p3 = all(_clean(seg["notice"]) and seg["session"] == "active" for seg in (l1, l2))
    p4 = ("rejected" in l3["notice"] and HTTP_4XX.search(l3["notice"]) is not None and "**Model**" in l3["notice"]
          and "could not be reached" not in l3["notice"] and "no answer" not in l3["notice"])
    return {"P1": bool(p1), "P2": bool(p2), "P3": bool(p3), "P4": bool(p4)}


# --------------------------------------------------------------------------------------------------
# Mythic and Sage reads
# --------------------------------------------------------------------------------------------------

def _stamp(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


async def request_view(client: Any, request_id: int) -> dict[str, Any]:
    base = _base()
    data = await base._query(client, ROWS_QUERY, rid=request_id)
    request = data.get("chat_request_by_pk") or {}
    sent = None
    if request.get("request_message_id"):
        message = (await base._query(client, MESSAGE_QUERY, id=int(request["request_message_id"])))
        sent = _stamp((message.get("chat_message_by_pk") or {}).get("created_at"))
    ended = _stamp(request.get("updated_at"))
    rows = {"status": request.get("status"), "messages": list(data.get("chat_message") or [])}
    return {"status": rows["status"], "notice": notice_text(rows),
            "elapsed": round(ended - sent, 1) if sent is not None and ended is not None else None}


def session_status(channel_ids: list[int]) -> dict[str, Any]:
    return _container_json(_SESSION_PROBE, *(str(c) for c in channel_ids))


def segment_config(segment: str, route: dict[str, str], listener_endpoint: str) -> dict[str, Any]:
    if segment == "L3":
        config = {"provider": "litellm", "model": MISSING_MODEL, "API_ENDPOINT": route["API_ENDPOINT"]}
    else:
        config = {"provider": "openai_compatible", "model": CHECK_MODEL, "API_KEY": PLACEHOLDER_KEY,
                  "API_ENDPOINT": BLACKHOLE if segment == "L1" else listener_endpoint}
    config.update(mode="conversation", autonomous_solve=False, max_steps=MAX_STEPS)
    return {"config": config, "channel_metadata_display": {"display": _base().nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


# --------------------------------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------------------------------

def _idle_timeout_settings(installed: Path) -> dict[str, Any]:
    container = subprocess.run(["docker", "exec", "sage", "printenv", "SAGE_NODE_IDLE_TIMEOUT"],
                               capture_output=True, text=True, timeout=30).stdout.strip()
    files = []
    for name in (".env.local", ".env"):
        path = installed / name
        if path.exists() and any(line.split("=", 1)[0].strip() == "SAGE_NODE_IDLE_TIMEOUT"
                                 for line in path.read_text().splitlines() if not line.strip().startswith("#")):
            files.append(name)
    return {"container_env_set": bool(container), "dotenv_files_setting_it": files}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, dep, reasoning = _base(), _dep(), _reasoning()
    route = dep.read_route(Path(args.route_file))
    deployment = _module("sage_no_answer_check_deployment", _DEPLOYMENT_PATH)
    installed = Path(deployment._installed_service_dir())
    parity = reasoning._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                                  "parity"])
    network = subprocess.run(["docker", "inspect", "sage", "--format", "{{.HostConfig.NetworkMode}}"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    tests = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", *OFFLINE_TESTS],
                           capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type" / "sage")
    listener = SilentListener()
    try:
        loopback = _container_json(_LOOPBACK_PROBE, str(listener.port))
        time.sleep(0.5)
        loopback["logged"] = [entry["request_line"] for entry in listener.log]
    finally:
        listener.close()
    key_probe = _container_json(reasoning._CONTAINER_KEY_PROBE, route["API_ENDPOINT"], route["MODEL"], MISSING_MODEL)
    models = key_probe.get("models") or {}
    phoenix = Path(args.phoenix_db)
    report = {
        "route": {"gateway_model": route["MODEL"], "endpoint": "from the route file (not recorded)"},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "network_mode": network,
        "idle_timeout": _idle_timeout_settings(installed),
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "phoenix_readable": phoenix.is_file(),
        "phoenix_health": dep.phoenix_health(args.phoenix_url),
        "task_baseline": await base._max_task(client),
        "offline_tests": {"returncode": tests.returncode, "summary": tests.stdout.strip().splitlines()[-1:]},
        "blackhole": _container_json(_BLACKHOLE_PROBE, BLACKHOLE.split("//", 1)[1].split("/", 1)[0]),
        "loopback": loopback,
        "missing_model_listed": models.get(MISSING_MODEL),
        "credential_source": {
            "installed": dep.installed_credentials(installed, route["API_ENDPOINT"]),
            "container": {**key_probe, "models": {route["MODEL"]: models.get(route["MODEL"])}},
            "operator_secrets": await reasoning._operator_secret_names(client),
            "channel_config_sets_key": False,  # L3 sets none; L1 and L2 send only the placeholder
        },
        "limits": LIMITS,
    }
    blockers = []
    for ok, reason in (
            (network == "host", "the Sage container is not on host networking"),
            (not any(report["idle_timeout"].values()), "SAGE_NODE_IDLE_TIMEOUT is set"),
            (report["sage_container_running"], "Sage container not consuming"),
            (not report["open_requests"], "open chat requests"),
            (report["phoenix_readable"] and report["phoenix_health"] == 200, "Phoenix unreadable or down"),
            (tests.returncode == 0, "this line's offline tests fail"),
            (report["blackhole"].get("result") == "timed out", "the blackhole address does not time out"),
            (loopback.get("accepted") is True and loopback.get("replied") is False and loopback["logged"],
             "the host loopback listener is not reachable and silent from the container"),
            (report["missing_model_listed"] is False, "the gateway lists the missing model")):
        if not ok:
            blockers.append(reason)
    blockers.extend(dep.credential_blockers(report["credential_source"], route))
    verdict = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


# --------------------------------------------------------------------------------------------------
# Run and verify
# --------------------------------------------------------------------------------------------------

def _gate(segment: str, spans: list[dict[str, Any]], posts: list[str], view: dict[str, Any]) -> dict[str, Any]:
    if segment == "L1":
        span = supervisor_model_span(spans)
        return {"open": span is not None, "model_span": span}
    if segment == "L2":
        hits = [line for line in posts if line.startswith("POST ") and line.split()[1:2]
                and line.split()[1].split("?")[0] in PROVIDER_PATHS]
        return {"open": bool(hits), "provider_requests": hits}
    return {"open": HTTP_4XX.search(view["notice"]) is not None}


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    nc = base.nc
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness is not ready_for_live in this run file")
    if run.get("requests"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate_check = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    run["check_attempt"] = {"returncode": gate_check.returncode, "output": gate_check.stdout.strip()[-400:]}
    if gate_check.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate_check.stdout.strip()} {gate_check.stderr.strip()}")
    route = _dep().read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    listener = SilentListener()
    run.update(authorized_by=args.authorized_by, started_at=time.time(), listener_endpoint=listener.endpoint,
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix), requests=[])
    started = time.monotonic()
    try:
        for segment in SEGMENTS:
            for attempt in range(1 + LIMITS["gate_retries_per_segment"]):
                created = await nc.create_locked_channel(
                    client, name=f"Provider no-answer check {segment}.{attempt + 1}",
                    description="Sage provider no-answer live check",
                    metadata=segment_config(segment, route, listener.endpoint))
                channel_id = int(created["chat_channel_id"])
                mark, logged = base.phoenix_mark(phoenix), len(listener.log)
                message = await nc.create_message(client, channel_id, PROMPT)
                entry: dict[str, Any] = {"segment": segment, "channel_id": channel_id,
                                         "request_id": int(message["chat_request_id"]), "sent_at": time.time()}
                try:
                    await nc.wait_for_request(client, entry["request_id"], timeout_seconds=WAIT_SECONDS[segment],
                                              poll_interval_seconds=1.0)
                except (TimeoutError, asyncio.TimeoutError) as error:
                    entry["timeout"] = str(error) or "request timeout"
                    await nc.stop_native_chat_request(client, channel_id=channel_id, request_id=entry["request_id"])
                await asyncio.sleep(SETTLE_SECONDS)
                entry.update(finished_at=time.time(), phoenix_after=mark, phoenix_until=base.phoenix_mark(phoenix))
                spans = window_spans(phoenix, mark, entry["phoenix_until"])
                posts = [item["request_line"] for item in listener.log[logged:]]
                entry["listener_requests"] = posts
                entry["gate"] = _gate(segment, spans, posts, await request_view(client, entry["request_id"]))
                run["requests"].append(entry)
                tasks = list((await base._query(client, TASKS_AFTER_QUERY, after=run["task_baseline"])).get("task") or [])
                tokens = sum(s["tokens"] for s in window_spans(phoenix, run["phoenix_start"], base.phoenix_mark(phoenix))
                             if s["kind"] == "LLM")
                stop = ("a Mythic task was created" if tasks else "an LLM span reported tokens" if tokens
                        else "time limit" if time.monotonic() - started > LIMITS["minutes"] * 60 else "")
                if stop:
                    run["stopped"] = {"after_request": entry["request_id"], "reason": stop, "tasks": tasks}
                    return {"requests": run["requests"], "stopped": run["stopped"]}
                if entry["gate"]["open"]:
                    break
    finally:
        listener.close()
        run["finished_at"], run["phoenix_end"] = time.time(), base.phoenix_mark(phoenix)
    return {"requests": run["requests"], "stopped": run.get("stopped")}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    requests = run.get("requests") or []
    gated = {segment: next((e for e in requests if e["segment"] == segment and (e.get("gate") or {}).get("open")), None)
             for segment in SEGMENTS}
    observations: dict[str, Any] = {"requests_sent": len(requests), "stopped": run.get("stopped"),
                                    "gate_retries": {s: sum(e["segment"] == s for e in requests) - 1 for s in SEGMENTS}}
    missing = [segment for segment, entry in gated.items() if entry is None]
    if missing:
        result = {"verdict": "not_run", "reason": f"gate closed for {', '.join(missing)}", "observations": observations}
    else:
        sessions = session_status([gated[s]["channel_id"] for s in ("L1", "L2")])
        segments = {}
        for segment, entry in gated.items():
            view = await request_view(client, int(entry["request_id"]))
            view["session"] = sessions.get(str(entry["channel_id"]))
            segments[segment] = view
            observations[segment] = {"request_id": entry["request_id"], "status": view["status"],
                                     "elapsed_seconds": view["elapsed"], "session": view["session"],
                                     "notice": view["notice"][:1200], "listener_requests": entry["listener_requests"],
                                     "script_seconds": round(entry["finished_at"] - entry["sent_at"], 1)}
        criteria = evaluate(segments, run.get("listener_endpoint", ""))
        result = {"verdict": "pass" if all(criteria.values()) else "fail", "criteria": criteria,
                  "observations": observations}
    result["verified_at"] = time.time()
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
        if name in {"readiness", "run"}:
            command.add_argument("--phoenix-db", required=True,
                                 help="the deployed Sage's live phoenix.db, read in place (read-only)")
            command.add_argument("--route-file", required=True,
                                 help="owner-only KEY=VALUE file with the gateway's API_ENDPOINT and a MODEL it lists "
                                      "(no key)")
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
