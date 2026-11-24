#!/usr/bin/env python3
"""Live check: the upgraded model SDKs serve native chat over the default client, with verified TLS and tracing.

The line moved the OpenAI SDK to 3.x (httpx2 default client), the Anthropic SDK, langchain-core, langchain-openai,
langchain-anthropic, pydantic-ai-slim (beneath Phoenix), dnspython and OpenTelemetry. This check runs the deployed
container on its default provider client: no capture, no metering, no retry policy (those use Sage's own transports).
One run file (JSON, owner-only) carries every stage.

  readiness --run-file F --route-file R --phoenix-db P [--phoenix-url U]
      Read-only. Container deployment parity with this branch; Sage consuming; no open chat requests; the container's
      installed packages equal this branch's constraints.txt; Phoenix readable and answering /healthz; the Mythic
      task baseline. The credential, by name and status only: the container environment sets no key; the installed
      .env.local holds exactly one active API_KEY whose block's active endpoint is the route's and no other active
      provider endpoint; .env sets no credential; inside the container, Sage's own dotenv and TLS bootstrap resolve
      that key and a keyed, verified-TLS GET /models returns 200 listing the route's model; the run operator has no
      API_KEY user secret; the channel configuration carries no key. Verdict: ready_for_live, deployment_required
      or not_ready with blockers.
  run --run-file F --route-file R --phoenix-db P --plan PLAN --authorized-by TEXT [--phoenix-url U]
      Runs `sage-line check-attempt PLAN` first and refuses on failure. Then, each request terminal before the next:
      Conversation channel: C1 then C2. Supervised channel (Autonomous Solve off): S1, rejecting every approval card.
      Stops (cancelling the open request) on a target task, a limit or a wrong model. No automatic retry.
  verify --run-file F --phoenix-db P
      Applies the pass rules below once, reading Mythic's rows and the live Phoenix spans in place.

The route file is KEY=VALUE, owner-only, with no API key: API_ENDPOINT and MODEL. The channel configuration sets
provider openai, that model and endpoint, and leaves OpenAI API unset (Chat Completions).

Pass rules (frozen with the plan's live check; read once):
  P1 Every LLM span in the set ends OK with no exception event, names the route's model, and no span in the set
     records an SDK, HTTP-client or TLS exception; the stored channel configurations name the route's endpoint; the
     container's openai is 3.x.
  P2 At least one Conversation request has a provider tool call whose id a later provider request in the same
     request consumes as Sage's tool result, and the request completes with a native answer row.
  P3 C2's first LLM span carries a tool call and its tool result whose id C1's spans produced, and ends OK.
  P4 S1 reaches a native terminal state without a provider or adapter error.
  P5 Every request has spans in Phoenix, and Phoenix /healthz answered 200 at every probe.
  P6 Zero Mythic tasks were created across the set.
If no request made a provider tool call, the check did not run (verdict did_not_run): it is not an attempt.
Observations, not pass criteria: token use, wall time, which tool the model chose and which agent called it, wording,
approval cards seen and rejected, exception types outside P1's set, SDK retries (not observable client-side).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = REPO_ROOT / "Payload_Type" / "sage"
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"

PROMPTS = {
    "C1": "List the callbacks Mythic has right now, with host and user.",
    "C2": "Which of those runs with the highest integrity?",
    "S1": "What should we do next?",
}
LIMITS = {"channels": 2, "requests": 3, "provider_calls": 20, "total_tokens": 300_000, "active_minutes": 20,
          "target_tasks": 0}
MAX_STEPS = 6            # per request; three requests stay inside the provider-call limit
REQUEST_TIMEOUT = 300
MAX_CARD_REJECTIONS = 3
SETTLE_SECONDS = 3       # Sage exports each span synchronously; a short settle before the closing Phoenix mark
TERMINAL = {"complete", "error", "cancelled"}
NOTICE_PREFIXES = ("⚠️ **BloodHound is not connected**",)
TRANSPORT_EXCEPTION = re.compile(
    r"(^|\.)(openai|anthropic|httpx2?|httpcore2?|ssl|langchain_openai|langchain_anthropic)\.|"
    r"APIConnectionError|APITimeoutError|APIStatusError|BadRequestError|AuthenticationError|PermissionDeniedError|"
    r"NotFoundError|RateLimitError|InternalServerError|SSL\w*Error|ConnectError|ConnectTimeout|ReadTimeout|"
    r"RemoteProtocolError|OpenAI\w*Error|Anthropic\w*Error")
ADAPTER_ERROR_TEXT = ("Invalid `http_client` argument", "Connection error", "CERTIFICATE_VERIFY_FAILED",
                      "The model provider rejected")
CREDENTIAL_NAMES = ("API_KEY", "OPENAI_API_KEY")


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
    return _module("sage_dependency_check_base", _SCRIPTS / "check_openai_responses.py")


def _reasoning():
    """The in-container key probe and the operator-secret lookup of the reasoning-tools check."""
    return _module("sage_dependency_check_reasoning", _SCRIPTS / "check_reasoning_tools.py")


# --------------------------------------------------------------------------------------------------
# Route, configuration and identity
# --------------------------------------------------------------------------------------------------

def read_route(path: Path) -> dict[str, str]:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"{path} must be owner-only (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if any(key.upper().endswith("API_KEY") for key in values):
        raise SystemExit(f"{path} must not contain an API key")
    missing = [key for key in ("API_ENDPOINT", "MODEL") if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    return values


def channel_config(route: dict[str, str], mode: str) -> dict[str, Any]:
    config = {"provider": "openai", "model": route["MODEL"], "API_ENDPOINT": route["API_ENDPOINT"], "mode": mode,
              "autonomous_solve": False, "max_steps": MAX_STEPS}
    return {"config": config,
            "channel_metadata_display": {"display": _base().nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


def _normalized(endpoint: str) -> str:
    return endpoint.strip().rstrip("/")


def _branch_constraints() -> dict[str, str]:
    pins = {}
    for line in (RUNTIME / "constraints.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            name, version = line.split("==", 1)
            pins[re.sub(r"[-_.]+", "-", name.lower())] = version.strip()
    return pins


def container_packages() -> dict[str, Any]:
    completed = subprocess.run(["docker", "exec", "sage", "python3", "-m", "pip", "list", "--format=freeze",
                                "--disable-pip-version-check"], capture_output=True, text=True, timeout=120)
    installed = {}
    for line in completed.stdout.splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            installed[re.sub(r"[-_.]+", "-", name.lower())] = version.strip()
    pins = _branch_constraints()
    tooling = {"pip", "setuptools", "wheel", "uv"}
    differing = {name: [version, pins.get(name)] for name, version in installed.items()
                 if name not in tooling and pins.get(name) != version}
    return {"count": len(installed), "differing_from_constraints": differing,
            "openai": installed.get("openai"), "langchain-openai": installed.get("langchain-openai"),
            "anthropic": installed.get("anthropic"), "pydantic-ai-slim": installed.get("pydantic-ai-slim")}


def installed_credentials(installed: Path, endpoint: str) -> dict[str, Any]:
    """Active credential and provider assignments in the installed dotenv files: names and booleans only."""
    report: dict[str, Any] = {}
    for name in (".env.local", ".env"):  # Sage's dotenv order (dotenv_bootstrap.DOTENV_FILENAMES)
        path = installed / name
        credentials, endpoints, providers, eval_flags = [], [], [], []
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip().startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("\"'")
                if not separator or not value:
                    continue
                if key in CREDENTIAL_NAMES:
                    credentials.append(key)
                elif key == "API_ENDPOINT":
                    endpoints.append(_normalized(value) == _normalized(endpoint))
                elif key == "provider":
                    providers.append(value.casefold() == "openai")
                elif key.startswith("SAGE_EVAL_"):
                    eval_flags.append(key)
        report[name] = {"present": path.exists(), "credentials": credentials, "endpoint_matches_route": endpoints,
                        "provider_is_openai": providers, "active_eval_flags": eval_flags}
    return report


def credential_blockers(report: dict[str, Any], route: dict[str, str]) -> list[str]:
    blockers = []
    local, env = (report.get("installed") or {}).get(".env.local") or {}, (report.get("installed") or {}).get(".env") or {}
    if local.get("credentials") != ["API_KEY"]:
        blockers.append("installed .env.local must hold exactly one active API_KEY")
    if local.get("endpoint_matches_route") != [True] or local.get("provider_is_openai") != [True]:
        blockers.append("installed .env.local must have exactly one active provider block: openai at the route's endpoint")
    if env.get("credentials") or env.get("endpoint_matches_route") or env.get("provider_is_openai"):
        blockers.append("installed .env sets a credential or provider")
    probe = report.get("container") or {}
    if probe.get("container_env"):
        blockers.append("the container environment sets a key")
    if probe.get("source") != ".env.local:API_KEY":
        blockers.append("Sage would not load the key from the installed .env.local")
    if probe.get("status") != 200 or probe.get("models") != {route["MODEL"]: True} or not probe.get("tls_verified"):
        blockers.append("keyed verified-TLS model listing did not return 200 with the route's model")
    secrets = report.get("operator_secrets") or {}
    if not secrets.get("resolved"):
        blockers.append("the run operator's user secrets could not be resolved")
    elif "API_KEY" in (secrets.get("names") or []):
        blockers.append("an API_KEY user secret would shadow the installed key")
    if report.get("channel_config_sets_key"):
        blockers.append("the channel configuration sets a key")
    return blockers


def _container_key_probe(route: dict[str, str]) -> dict[str, Any]:
    probe = _reasoning()._CONTAINER_KEY_PROBE
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", probe,
                                route["API_ENDPOINT"], route["MODEL"]], capture_output=True, text=True, timeout=90)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


def phoenix_health(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=5) as response:
            return int(response.status)
    except Exception:  # noqa: BLE001 - any failure is "not 200"
        return None


# --------------------------------------------------------------------------------------------------
# Phoenix spans (read-only, in place)
# --------------------------------------------------------------------------------------------------

def spans(db: Path, after: int, until: int) -> list[dict[str, Any]]:
    """Every span in (after, until]: kind, status, exception types, and for LLM spans the model, tokens, the tool call
    ids the output produced, and the tool call ids and tool-result ids the input carried."""
    base = _base()
    with base._phoenix(db) as conn:
        rows = conn.execute(
            "SELECT id, span_kind, status_code, events, attributes, COALESCE(llm_token_count_prompt, 0), "
            "COALESCE(llm_token_count_completion, 0) FROM spans WHERE id > ? AND id <= ? ORDER BY id",
            (after, until)).fetchall()
    result = []
    for span_id, kind, status, events, attributes, prompt, completion in rows:
        try:
            events = json.loads(events) if isinstance(events, str) else (events or [])
        except ValueError:
            events = []
        exceptions = [str((event.get("attributes") or {}).get("exception.type") or "")
                      for event in events if isinstance(event, dict) and event.get("name") == "exception"]
        entry: dict[str, Any] = {"id": span_id, "kind": kind, "status": status, "exceptions": exceptions}
        if kind == "LLM":
            try:
                attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
            except ValueError:
                attrs = {}
            llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
            produced = {value for path, value in base._walk(llm.get("output_messages") or [])
                        if isinstance(value, str) and path.endswith("tool_call.id")}
            input_calls = {value for path, value in base._walk(llm.get("input_messages") or [])
                           if isinstance(value, str) and path.endswith("tool_call.id")}
            results = {value for path, value in base._walk(llm.get("input_messages") or [])
                       if isinstance(value, str) and path.endswith("tool_call_id")}
            entry.update(model=str(llm.get("model_name") or ""), prompt_tokens=int(prompt),
                         completion_tokens=int(completion), produced_calls=sorted(produced),
                         input_calls=sorted(input_calls), consumed_results=sorted(results))
        result.append(entry)
    return result


def _llm(window: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [span for span in window if span["kind"] == "LLM"]


# --------------------------------------------------------------------------------------------------
# Pass rules (pure; tested offline)
# --------------------------------------------------------------------------------------------------

def _answers(rows: dict[str, Any]) -> list[str]:
    texts = [str(m.get("message") or "").strip() for m in rows.get("messages", [])]
    return [text for text in texts if text and not text.startswith(NOTICE_PREFIXES)]


def evaluate(run: dict[str, Any], windows: dict[str, list[dict[str, Any]]], rows: dict[str, dict[str, Any]],
             tasks_after: int) -> dict[str, Any]:
    route = run["route"]
    every = [span for name in windows for span in windows[name]]
    llm_all = _llm(every)
    transport = sorted({name for span in every for name in span["exceptions"] if TRANSPORT_EXCEPTION.search(name)})
    tool_calls_made = any(span["produced_calls"] for span in llm_all)

    def round_trip(name: str) -> list[str]:
        window = _llm(windows.get(name, []))
        produced = {call for span in window for call in span["produced_calls"]}
        consumed = {call for span in window for call in span["consumed_results"]}
        return sorted(produced & consumed)

    conversation = {name: round_trip(name) for name in ("C1", "C2") if name in windows}
    p2_requests = [name for name, calls in conversation.items()
                   if calls and rows.get(name, {}).get("status") == "complete" and _answers(rows[name])]
    c1_calls = {call for span in _llm(windows.get("C1", [])) for call in span["produced_calls"]}
    c2_first = (_llm(windows.get("C2", [])) or [None])[0]
    carried = sorted(c1_calls & set(c2_first["input_calls"]) & set(c2_first["consumed_results"])) if c2_first else []
    s1_text = "\n".join(_answers(rows.get("S1", {"messages": []})))
    s1_transport = sorted({name for span in windows.get("S1", []) for name in span["exceptions"]
                           if TRANSPORT_EXCEPTION.search(name)})
    health = run.get("phoenix_health") or []
    criteria = {
        "P1": bool(llm_all) and all(span["status"] == "OK" and not span["exceptions"] for span in llm_all)
        and all(span["model"] == route["MODEL"] for span in llm_all) and not transport
        and run.get("stored_endpoints") == {name: True for name in run.get("channels", {})}
        and str(run.get("container_openai") or "").startswith("3."),
        "P2": bool(p2_requests),
        "P3": bool(carried) and c2_first is not None and c2_first["status"] == "OK" and not c2_first["exceptions"],
        "P4": rows.get("S1", {}).get("status") in TERMINAL and not s1_transport
        and not any(text in s1_text for text in ADAPTER_ERROR_TEXT),
        "P5": all(windows.get(name) for name in PROMPTS) and bool(health) and all(code == 200 for code in health),
        "P6": tasks_after == run.get("task_baseline") and not run.get("stopped_for_task"),
    }
    observations = {
        "provider_calls": len(llm_all),
        "total_tokens": sum(s["prompt_tokens"] + s["completion_tokens"] for s in llm_all),
        "wall_seconds": round(float(run.get("finished_at", 0)) - float(run.get("started_at", 0)), 1),
        "conversation_round_trips": conversation, "p2_requests": p2_requests, "c2_carried_c1_calls": carried,
        "request_status": {name: rows[name].get("status") for name in rows},
        "cards_rejected": run.get("cards_rejected", 0), "transport_exception_types": transport,
        "all_exception_types": sorted({name for span in every for name in span["exceptions"]}),
        "stopped": run.get("stopped"), "sdk_retries": "not observable client-side",
    }
    if not tool_calls_made:
        verdict = "did_not_run"
    else:
        verdict = "pass" if all(criteria.values()) else "fail"
    return {"criteria": criteria, "observations": observations, "verdict": verdict}


# --------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------

async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, reasoning = _base(), _reasoning()
    route = read_route(Path(args.route_file))
    deployment = _module("sage_dependency_check_deployment", _DEPLOYMENT_PATH)
    installed = Path(deployment._installed_service_dir())
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    parity = reasoning._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                                  "parity"])
    phoenix = Path(args.phoenix_db)
    report = {
        "route": {"model": route["MODEL"], "endpoint": "from the route file (not recorded)"},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "container_packages": container_packages(),
        "phoenix_readable": phoenix.is_file(),
        "phoenix_health": phoenix_health(args.phoenix_url),
        "task_baseline": await base._max_task(client),
        "credential_source": {
            "installed": installed_credentials(installed, route["API_ENDPOINT"]),
            "container": _container_key_probe(route),
            "operator_secrets": await reasoning._operator_secret_names(client),
            "channel_config_sets_key": any("API_KEY" in channel_config(route, mode)["config"]
                                           for mode in ("conversation", "supervised")),
        },
        "limits": LIMITS, "max_steps": MAX_STEPS,
    }
    blockers = []
    if not report["sage_container_running"]:
        blockers.append("Sage container not consuming")
    if report["open_requests"]:
        blockers.append("open chat requests")
    packages = report["container_packages"]
    if packages["differing_from_constraints"] or not str(packages.get("openai") or "").startswith("3."):
        blockers.append("container packages differ from this branch's constraints")
    if not report["phoenix_readable"] or report["phoenix_health"] != 200:
        blockers.append("Phoenix unreadable or not answering /healthz")
    blockers.extend(credential_blockers(report["credential_source"], route))
    verdict = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


def _over_limit(run: dict[str, Any], phoenix: Path, started: float) -> str:
    llm_all = _llm(spans(phoenix, run["phoenix_start"], _base().phoenix_mark(phoenix)))
    if len(llm_all) > LIMITS["provider_calls"]:
        return "provider call limit"
    if sum(s["prompt_tokens"] + s["completion_tokens"] for s in llm_all) > LIMITS["total_tokens"]:
        return "token limit"
    if any(s["model"] and s["model"] != run["route"]["MODEL"] for s in llm_all):
        return "a span names another model"
    if time.monotonic() - started > LIMITS["active_minutes"] * 60:
        return "active time limit"
    return ""


async def _request(client: Any, run: dict[str, Any], name: str, channel_id: int, phoenix: Path,
                   phoenix_url: str) -> dict[str, Any]:
    nc = _base().nc
    mark = _base().phoenix_mark(phoenix)
    message = await nc.create_message(client, channel_id, PROMPTS[name])
    request_id = int(message["chat_request_id"])
    entry: dict[str, Any] = {"request_id": request_id, "channel_id": channel_id, "phoenix_after": mark}
    deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        while True:
            remaining = max(1.0, deadline - time.monotonic())
            result = await nc.wait_for_request(client, request_id, timeout_seconds=remaining,
                                               stop_on_input_requested=(name == "S1"))
            if result.get("halt_reason") != "operator_input_requested":
                break
            if run.get("cards_rejected", 0) >= MAX_CARD_REJECTIONS:
                raise TimeoutError("approval card limit")
            await nc.reject_pending_input_card(client, request_id)
            run["cards_rejected"] = run.get("cards_rejected", 0) + 1
    except (TimeoutError, asyncio.TimeoutError) as error:
        entry["timeout"] = str(error) or "request timeout"
        await nc.stop_native_chat_request(client, channel_id=channel_id, request_id=request_id)
    await asyncio.sleep(SETTLE_SECONDS)
    entry["phoenix_until"] = _base().phoenix_mark(phoenix)
    entry["tasks_after"] = await _base()._max_task(client)
    run.setdefault("phoenix_health", []).append(phoenix_health(phoenix_url))
    return entry


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness has not passed in this run file")
    if run.get("requests"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    if gate.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate.stdout.strip()} {gate.stderr.strip()}".strip())
    base = _base()
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, started_at=time.time(), route={"MODEL": route["MODEL"]},
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix),
               container_openai=container_packages().get("openai"), requests={}, channels={},
               phoenix_health=[phoenix_health(args.phoenix_url)])
    started = time.monotonic()
    sequence = (("conversation", ("C1", "C2")), ("supervised", ("S1",)))
    for mode, names in sequence:
        if run.get("stopped"):
            break
        created = await base.nc.create_locked_channel(
            client, name=f"Dependency advisories live check ({mode})",
            description="Sage dependency advisories live check", metadata=channel_config(route, mode))
        channel_id = int(created["chat_channel_id"])
        run["channels"][mode] = channel_id
        stored = ((await base._query(client, base.CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {})
        stored_endpoint = ((stored.get("ai_metadata") or {}).get("config") or {}).get("API_ENDPOINT") or ""
        run.setdefault("stored_endpoints", {})[mode] = _normalized(stored_endpoint) == _normalized(
            route["API_ENDPOINT"])
        for name in names:
            entry = await _request(client, run, name, channel_id, phoenix, args.phoenix_url)
            run["requests"][name] = entry
            if entry["tasks_after"] != run["task_baseline"]:
                run["stopped"], run["stopped_for_task"] = {"request": name, "reason": "a target task"}, True
            elif reason := _over_limit(run, phoenix, started):
                run["stopped"] = {"request": name, "reason": reason}
            elif entry.get("timeout"):
                run["stopped"] = {"request": name, "reason": entry["timeout"]}
            if run.get("stopped"):
                break
    run["finished_at"] = time.time()
    run["phoenix_end"] = base.phoenix_mark(phoenix)
    run["phoenix_health"].append(phoenix_health(args.phoenix_url))
    return {"channels": run["channels"], "requests": run["requests"], "stopped": run.get("stopped")}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if "verify" in run:
        raise SystemExit("this run was already read; the result is read once")
    base = _base()
    phoenix = Path(args.phoenix_db)
    requests = run.get("requests") or {}
    windows = {name: spans(phoenix, info["phoenix_after"], info["phoenix_until"]) for name, info in requests.items()}
    rows = {name: await base.request_rows(client, int(info["request_id"])) for name, info in requests.items()}
    result = evaluate(run, windows, rows, await base._max_task(client))
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
