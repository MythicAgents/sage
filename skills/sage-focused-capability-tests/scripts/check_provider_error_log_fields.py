#!/usr/bin/env python3
"""Live check: a failed provider request logs its status, type, code and parameter as fields in Sage's container log.

The deployed Sage container must run the branch under test. One run file (JSON, owner-only) carries every stage. No
real credential is used: each case's chat sets the **API Key** field to PLACEHOLDER, which ranks above user secrets and
`.env.local`, so every request fails authentication (0 billed tokens, 0 Mythic tasks, no callback).

  readiness --run-file F
      Read-only. Deployment parity with this branch (container mode); Sage consuming; no open chat requests; this
      line's offline tests pass; nothing in the container's dotenv chain or the operator's user secrets sets a value
      that would stop the Anthropic case before its request (SAGE_REASONING_EFFORT, SAGE_PROVIDER_RETRY_POLICY,
      names only). The oracle: from inside the container, a keyed non-inference GET /v1/models at each case's
      endpoint with PLACEHOLDER must return 401; only its status and the body's error type/code/param are recorded.
      Verdict: ready_for_live, deployment_required or not_ready.
  run --run-file F --plan PLAN --authorized-by TEXT
      Runs `sage-line check-attempt PLAN` first and refuses on failure. For each case, in order O then A, it creates
      a fresh Conversation channel with the case's route and sends PROMPT once, waits for the request to end, and
      reads `docker logs` (what `./mythic-cli logs sage` shows) for that request's window. No retry.
  verify --run-file F
      Applies the pass rules below once.

Cases (the plan's route; API Endpoint is set explicitly to each provider's own default so no inherited endpoint can
redirect a request):
  O  provider openai, endpoint https://api.openai.com/v1, model gpt-5.6-sol, OpenAI API Responses.
  A  provider anthropic, endpoint https://api.anthropic.com (the SDK's default), model claude-sonnet-5-5.

Pass rules (frozen with the plan's live check; read once):
  L1 (O) / L2 (A)  In the case's window, exactly one fields line with the case's sdk and status=401, and its type,
     code and param equal the oracle's (`-` for an absent field).
  L3 Each such fields line is exactly `Provider request failed: sdk=… status=… type=… code=… param=…` with bounded
     tokens, and contains neither PLACEHOLDER nor PROMPT, so it carries no provider message text.
  L4 Each case's window still holds Sage's existing `Error during graph execution` line.
  L5 Each case's window holds exactly one `Error during graph execution` line: one record is written to the container
     log once, however many Mythic clients Sage has built since start (plan r2, claim 2).
Not run: if a case's window holds no graph-error line for HTTP 401, its request never reached the provider and the
check did not run. Observations, not pass criteria: the chat card text, the model debug line's provider and base_url,
other fields lines in the window, wall time.
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
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"

PLACEHOLDER = "sage-invalid-placeholder-key"
PROMPT = "Reply with OK."
CASES: dict[str, dict[str, str]] = {
    "O": {"sdk": "openai", "provider": "openai", "model": "gpt-5.6-sol", "endpoint": "https://api.openai.com/v1",
          "openai_api": "responses", "models_url": "https://api.openai.com/v1/models"},
    "A": {"sdk": "anthropic", "provider": "anthropic", "model": "claude-sonnet-5-5",
          "endpoint": "https://api.anthropic.com", "openai_api": "", "models_url": "https://api.anthropic.com/v1/models"},
}
LIMITS = {"minutes": 15, "channels": 2, "provider_requests": 4, "target_tasks": 0}
REQUEST_TIMEOUT = 300
LOG_SETTLE_SECONDS = 5
OFFLINE_TESTS = ("tests/test_provider_error_log_fields.py", "tests/test_provider_wire_round_trips.py")
# Values the Anthropic route refuses before any request (SAGE_REASONING_EFFORT and SAGE_PROVIDER_RETRY_POLICY need
# an OpenAI-family provider), so readiness requires that nothing sets them.
BLOCKING_SETTINGS = ("SAGE_REASONING_EFFORT", "SAGE_PROVIDER_RETRY_POLICY")
FIELDS_LINE = re.compile(r"Provider request failed: sdk=(?P<sdk>openai|anthropic) status=(?P<status>\d{3}) "
                         r"type=(?P<type>[A-Za-z0-9._:/\[\]-]{1,64}) code=(?P<code>[A-Za-z0-9._:/\[\]-]{1,64}) "
                         r"param=(?P<param>[A-Za-z0-9._:/\[\]-]{1,64})")
GRAPH_ERROR = "Error during graph execution:"
DEBUG_LINE = re.compile(r"Initializing model with provider=(?P<provider>[^,]+), .*base_url=(?P<base_url>[^,]+),")

_CONTAINER_PROBE = r"""
import json, os, ssl, sys
sys.path.insert(0, "/Mythic")
from dotenv import dotenv_values
import dotenv_bootstrap as boot
names = sys.argv[1].split(",")
environ = dict(os.environ)
for path in boot.dotenv_paths("/Mythic"):
    boot.apply_dotenv(dotenv_values(path), environ)
out = {"settings_set": sorted(n for n in names if environ.get(n)), "oracles": {}}
import httpx
from ssl_bootstrap import configure_sage_ssl_cert_file
bundle = configure_sage_ssl_cert_file()
context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
key = sys.argv[2]
for case, url in json.loads(sys.argv[3]).items():
    headers = ({"Authorization": "Bearer " + key} if case == "O"
               else {"x-api-key": key, "anthropic-version": "2023-06-01"})
    entry = {"status": None}
    try:
        response = httpx.get(url, headers=headers, timeout=20, verify=context)
        entry["status"] = response.status_code
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        entry.update(type=error.get("type"), code=error.get("code"), param=error.get("param"))
    except Exception as exc:
        entry["error"] = type(exc).__name__
    out["oracles"][case] = entry
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
    """Native-chat and Mythic helpers of the OpenAI Responses check."""
    return _module("sage_error_fields_check_base", _SCRIPTS / "check_openai_responses.py")


def _reasoning():
    return _module("sage_error_fields_check_reasoning", _SCRIPTS / "check_reasoning_tools.py")


# --------------------------------------------------------------------------------------------------
# Pass rules (pure)
# --------------------------------------------------------------------------------------------------

def _token(value: Any) -> str:
    return str(value) if value not in (None, "") else "-"


def expected_fields(case: str, oracle: dict[str, Any]) -> dict[str, str]:
    return {"sdk": CASES[case]["sdk"], "status": "401", "type": _token(oracle.get("type")),
            "code": _token(oracle.get("code")), "param": _token(oracle.get("param"))}


def _fields_text(line: str) -> str:
    start = line.find("Provider request failed:")
    return line[start:].rstrip() if start >= 0 else ""


def window_outcome(case: str, lines: list[str], oracle: dict[str, Any]) -> dict[str, Any]:
    """Apply L1/L2, L3, L4 and L5 to one case's container-log window."""
    expected = expected_fields(case, oracle)
    fields = [_fields_text(line) for line in lines if "Provider request failed:" in line]
    matched = [(text, FIELDS_LINE.fullmatch(text)) for text in fields]
    case_lines = [(text, found) for text, found in matched
                  if found and found["sdk"] == expected["sdk"] and found["status"] == "401"]
    fields_ok = len(case_lines) == 1 and {k: case_lines[0][1][k] for k in expected} == expected
    clean = bool(case_lines) and all(PLACEHOLDER not in text and PROMPT not in text for text, _ in case_lines)
    graph = [line for line in lines if GRAPH_ERROR in line]
    debug = [found.groupdict() for found in (DEBUG_LINE.search(line) for line in lines) if found]
    return {"fields": fields_ok, "clean": clean, "graph_error": bool(graph), "graph_error_once": len(graph) == 1,
            "ran": any("401" in line for line in graph),
            "detail": {"expected": expected, "case_lines": [text for text, _ in case_lines],
                       "other_fields_lines": [text for text, found in matched if (text, found) not in case_lines],
                       "graph_error_lines": len(graph), "model_debug": debug[-1:]}}


def verdict(outcomes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    criteria = {"L1": outcomes["O"]["fields"], "L2": outcomes["A"]["fields"],
                "L3": outcomes["O"]["clean"] and outcomes["A"]["clean"],
                "L4": outcomes["O"]["graph_error"] and outcomes["A"]["graph_error"],
                "L5": outcomes["O"]["graph_error_once"] and outcomes["A"]["graph_error_once"]}
    not_run = [case for case, outcome in outcomes.items() if not outcome["ran"]]
    return {"verdict": "not_run" if not_run else ("pass" if all(criteria.values()) else "fail"),
            "criteria": criteria, "not_run": not_run}


def oracle_blockers(oracles: dict[str, Any]) -> list[str]:
    blockers = []
    for case in CASES:
        entry = oracles.get(case) or {}
        if entry.get("status") != 401:
            blockers.append(f"case {case}: GET /v1/models with the placeholder returned {entry.get('status')} "
                            f"({entry.get('error') or 'no error'}), not 401")
        elif not entry.get("type"):
            blockers.append(f"case {case}: the 401 body names no error type")
    return blockers


# --------------------------------------------------------------------------------------------------
# Readiness, run and verify
# --------------------------------------------------------------------------------------------------

def container_probe() -> dict[str, Any]:
    urls = json.dumps({case: spec["models_url"] for case, spec in CASES.items()})
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", _CONTAINER_PROBE,
                                ",".join(BLOCKING_SETTINGS), PLACEHOLDER, urls],
                               capture_output=True, text=True, timeout=120)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, reasoning = _base(), _reasoning()
    parity = reasoning._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                                  "parity"])
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    tests = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", *OFFLINE_TESTS],
                           capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type" / "sage")
    probe = container_probe()
    secrets = await reasoning._operator_secret_names(client)
    report = {
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "offline_tests": {"returncode": tests.returncode, "summary": tests.stdout.strip().splitlines()[-1:]},
        "container_settings_set": probe.get("settings_set"),
        "operator_secrets": secrets,
        "oracles": probe.get("oracles") or {},
        "task_baseline": await base._max_task(client),
        "limits": LIMITS,
    }
    blocking_secrets = sorted(set(secrets.get("names") or []) & set(BLOCKING_SETTINGS))
    blockers = [reason for ok, reason in (
        (report["sage_container_running"], "Sage container not consuming"),
        (not report["open_requests"], "open chat requests"),
        (tests.returncode == 0, "this line's offline tests fail"),
        ("error" not in probe, "the container probe did not report"),
        (not probe.get("settings_set"), f"the container sets {probe.get('settings_set')}, which stops case A"),
        (bool(secrets.get("resolved")), "user secrets are unreadable"),
        (not blocking_secrets, f"a user secret sets {blocking_secrets}, which stops case A"),
    ) if not ok]
    blockers.extend(oracle_blockers(report["oracles"]))
    result = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, result
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": result, "blockers": blockers})
    run["readiness"] = report
    return report


def channel_metadata(case: str) -> dict[str, Any]:
    spec = CASES[case]
    config = {"provider": spec["provider"], "model": spec["model"], "API_ENDPOINT": spec["endpoint"],
              "API_KEY": PLACEHOLDER, "mode": "conversation", "autonomous_solve": False, "max_steps": 4}
    if spec["openai_api"]:
        config["SAGE_OPENAI_API"] = spec["openai_api"]
    return {"config": config, "channel_metadata_display": {"display": _base().nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


def container_log(since: float, until: float) -> list[str]:
    completed = subprocess.run(["docker", "logs", "--since", f"{since:.3f}", "--until", f"{until:.3f}", "sage"],
                               capture_output=True, text=True, timeout=60)
    return (completed.stdout + completed.stderr).splitlines()


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    nc = base.nc
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness is not ready_for_live in this run file")
    if run.get("cases"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    run["check_attempt"] = {"returncode": gate.returncode, "output": gate.stdout.strip()[-400:]}
    if gate.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate.stdout.strip()} {gate.stderr.strip()}")
    run.update(authorized_by=args.authorized_by, started_at=time.time(), task_baseline=await base._max_task(client),
               cases={})
    deadline = time.monotonic() + LIMITS["minutes"] * 60
    for case in CASES:
        created = await nc.create_locked_channel(client, name=f"Provider error fields check: {case}",
                                                 description=f"Provider error log fields live check {case}",
                                                 metadata=channel_metadata(case))
        channel = int(created["chat_channel_id"])
        since = time.time() - 1
        message = await nc.create_message(client, channel, PROMPT)
        entry: dict[str, Any] = {"channel_id": channel, "request_id": int(message["chat_request_id"]),
                                 "since": since}
        run["cases"][case] = entry
        try:
            snapshot = await nc.wait_for_request(
                client, entry["request_id"],
                timeout_seconds=max(1.0, min(REQUEST_TIMEOUT, deadline - time.monotonic())))
            entry["status"] = (snapshot.get("request") or {}).get("status")
        except (TimeoutError, asyncio.TimeoutError) as error:
            entry["timeout"] = str(error) or "request timeout"
            await nc.stop_native_chat_request(client, channel_id=channel, request_id=entry["request_id"])
        await asyncio.sleep(LOG_SETTLE_SECONDS)
        entry["until"] = time.time()
        entry["log"] = [line[-600:] for line in container_log(entry["since"], entry["until"])
                        if "Provider request failed:" in line or GRAPH_ERROR in line
                        or "Initializing model with provider=" in line]
        rows = await base.request_rows(client, entry["request_id"])
        texts = [str(row.get("message") or "") for row in rows["messages"] if row.get("author_type") != "operator"]
        entry["card_text"] = texts[-1][:400] if texts else ""
    run["finished_at"] = time.time()
    run["new_tasks"] = await base._max_task(client) - int(run["task_baseline"])
    return {case: {k: v for k, v in entry.items() if k != "log"} for case, entry in run["cases"].items()}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verify"):
        raise SystemExit("this run was already read; the result is read once")
    oracles = (run.get("readiness") or {}).get("oracles") or {}
    outcomes = {case: window_outcome(case, run["cases"][case]["log"], oracles.get(case) or {}) for case in CASES}
    result = verdict(outcomes)
    result.update(detail={case: outcome["detail"] for case, outcome in outcomes.items()},
                  observations={"card_text": {case: run["cases"][case].get("card_text") for case in CASES},
                                "new_tasks": run.get("new_tasks"),
                                "wall_seconds": round(float(run["finished_at"]) - float(run["started_at"]), 1)},
                  verified_at=time.time())
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
        if name == "run":
            command.add_argument("--plan", required=True, help="the line's plan file, for sage-line check-attempt")
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this bounded attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
