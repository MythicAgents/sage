#!/usr/bin/env python3
"""Live check: reasoning-enabled tool calls recover after an API or effort correction in the same native chat.

One run file (JSON, owner-only) carries every stage. ``readiness`` and ``verify`` only read. ``arm``, ``run`` and
``cleanup`` change the deployment or spend provider tokens and need the operator's authorization.

  readiness --run-file F --route-file R --phoenix-db P
      Read-only. Container deployment, one consuming Sage, no open chat requests, feature source parity, the
      container's adapter versions, the native controls Mythic stores, Phoenix and the Mythic task baseline,
      the direct endpoint's verified-TLS reachability (keyless; expects 401), where the API key comes from (never
      its value), and the capture/metering state. Verdict: ``deployment_required`` (source parity fails),
      ``capture_required`` (deployed, not armed), ``ready_for_live`` or ``not_ready`` with blockers.
  arm --run-file F --authorized-by TEXT
      Starts the private wire receiver, writes the resource budget, adds a marked block to the installed Sage
      ``.env`` (original bytes kept, owner-only) and recreates the Sage container through the deployment helper.
  run --run-file F --route-file R --phoenix-db P --authorized-by TEXT
      Two fresh Conversation channels, steps A1-A4 (gpt-5.6-sol) then B1-B4 (gpt-6.1-sol), each step terminal
      before the next; stops on a target task, budget exhaustion, wall time or wrong identity; always cleans up.
  cleanup --run-file F
      Cancels this set's active requests, restores the installed ``.env`` byte for byte, recreates the container,
      stops the receiver and confirms no open requests. Safe to repeat.
  verify --run-file F --phoenix-db P
      Applies the frozen pass rules once (criteria 1-5 of the check specification).

The route file is KEY=VALUE, owner-only, with no API key: API_ENDPOINT, MODEL_A, MODEL_B, EFFORT.
Daybreak is left at its blank default in every step: Sage sends Off on Responses, and an explicit Off would make
Chat Completions invalid before any request (amendment recorded in the check specification). The wire capture
proves Off on every Responses request.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import signal
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = REPO_ROOT / "Payload_Type" / "sage"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_BASE_PATH = REPO_ROOT / "skills/sage-focused-capability-tests/scripts/check_openai_responses.py"
_RECEIVER_PATH = REPO_ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"
_DEPLOYMENT_PATH = REPO_ROOT / "skills/sage-goad-reset/scripts/sage_deployment.py"

LABEL = "amber-seven"
SEED = ("Remember the label amber-seven. For a read-only status check, use list_callbacks once, then tell me the "
        "label and summarize the returned callback list. Do not run tasks.")
TOOL = ("For a read-only status check, use list_callbacks once, then tell me the label from the previous turn and "
        "summarize the returned callback list. Do not run tasks.")
RECALL = "Repeat my label without using tools."
# (step, channel, api, effort, prompt, expected outcome)
STEPS = (
    ("A1", "A", "responses", "low", SEED, "tool"),
    ("A2", "A", "chat_completions", "low", TOOL, "rejected"),
    ("A3", "A", "responses", "low", TOOL, "tool"),
    ("A4", "A", "responses", "low", RECALL, "recall"),
    ("B1", "B", "responses", "low", SEED, "tool"),
    ("B2", "B", "responses", "none", TOOL, "rejected"),
    ("B3", "B", "responses", "low", TOOL, "tool"),
    ("B4", "B", "responses", "low", RECALL, "recall"),
)
EXPECTED_MODELS = {"A": "gpt-5.6-sol", "B": "gpt-6.1-sol"}
DIRECT_ENDPOINT = "https://api.openai.com/v1"
CEILINGS = {"transport_requests": 32, "provider_tokens": 1_048_576, "output_tokens_per_call": 4096,
            "active_seconds": 20 * 60, "cleanup_seconds": 5 * 60, "target_tasks": 0}
TOKEN_RESERVATION = 65_536  # per call; the largest recent Sage call was ~35k prompt tokens
MAX_STEPS = 4
RETRY_POLICY = "transient-v1"  # keeps a captured 4xx typed for the SDK (allow_non2xx); retries are metered
STEP_TIMEOUT = 300
EXPECTED_ADAPTER = {"langchain-openai": "1.4.2", "openai": "2.54.0", "langchain": "1.3.14"}
TOOLS_ACTION = "set **OpenAI API** to **Responses**"
TOOLS_KEEPS = "keeps reasoning and tools"
EFFORT_ACTION = "set **Reasoning Effort** to a supported value"
REFUSALS = ("Sage replaced this channel's session", "Sage restarted or this channel's session was replaced",
            "Start a new Sage chat")
NOTICE_PREFIXES = ("⚠️ **BloodHound is not connected**", "Applied this chat's updated OpenAI settings",
                   "This chat now uses model")
PARTIAL_WORK = "Partial Work Preserved"
REASONING_ID = re.compile(r"\brs_[0-9a-f]{8,}")
ENV_BEGIN = "# >>> reasoning-tool check (temporary; removed by cleanup)"
ENV_END = "# <<< reasoning-tool check"
CHANGED_FILES = (
    "Payload_Type/sage/ai/langgraph/operator_error.py",
    "Payload_Type/sage/sage_chat/service.py",
    "Payload_Type/sage/sage_chat/models.py",
    "Payload_Type/sage/ai/openai_api.py",
    "Payload_Type/sage/ai/resource_http.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "skills/sage-focused-capability-tests/scripts/check_reasoning_tools.py",
)


# --------------------------------------------------------------------------------------------------
# Route file, run file, evidence
# --------------------------------------------------------------------------------------------------

def read_route(path: Path) -> dict[str, str]:
    """KEY=VALUE route file: owner-only, no API key, the direct endpoint, both named models and effort low."""
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"{path} must be owner-only (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if any("KEY" in key.upper() or "TOKEN" in key.upper() or "SECRET" in key.upper() for key in values):
        raise SystemExit(f"{path} must not contain a credential; the deployment's secret store supplies it")
    missing = [key for key in ("API_ENDPOINT", "MODEL_A", "MODEL_B", "EFFORT") if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    if values["API_ENDPOINT"].rstrip("/") != DIRECT_ENDPOINT:
        raise SystemExit(f"API_ENDPOINT must be {DIRECT_ENDPOINT} (direct OpenAI)")
    if (values["MODEL_A"], values["MODEL_B"]) != (EXPECTED_MODELS["A"], EXPECTED_MODELS["B"]):
        raise SystemExit(f"MODEL_A/MODEL_B must be {EXPECTED_MODELS['A']} and {EXPECTED_MODELS['B']}; no substitute")
    if values["EFFORT"] != "low":
        raise SystemExit("EFFORT must be low (the corrected setting this check verifies)")
    return values


def channel_config(route: dict[str, str], channel: str, *, api: str, effort: str) -> dict[str, Any]:
    """One channel's native configuration: Conversation, no autonomous solve, max_steps 4, Daybreak blank."""
    config = {"provider": "openai", "model": route[f"MODEL_{channel}"], "API_ENDPOINT": route["API_ENDPOINT"],
              "mode": "conversation", "autonomous_solve": False, "max_steps": MAX_STEPS,
              "SAGE_OPENAI_API": api, "SAGE_REASONING_EFFORT": effort,
              "SAGE_PROVIDER_RETRY_POLICY": RETRY_POLICY}
    return {"config": config, "channel_metadata_display": {"display": "expanded; max=15"}}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "head": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "Payload_Type/sage", "skills")),
            "files": {path: _sha256(REPO_ROOT / path) for path in CHANGED_FILES}}


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def evidence_dir(run: dict[str, Any]) -> Path:
    if not run.get("evidence_dir"):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run["evidence_dir"] = str(REPO_ROOT / ".sage_history/2026/11/diagnostics/reasoning-tool-compatibility"
                                  / f"{stamp}-live-check")
    return _private_dir(Path(run["evidence_dir"]))


# --------------------------------------------------------------------------------------------------
# Installed .env block (pure; the original bytes are restored exactly)
# --------------------------------------------------------------------------------------------------

def env_block(container_dir: str, fifo_dir: str) -> str:
    return "\n".join([
        ENV_BEGIN,
        f"SAGE_OBSERVATION_DIRECTORY={fifo_dir}",
        f"SAGE_RESOURCE_BUDGET_CONFIG={container_dir}/budget.json",
        f"SAGE_RESOURCE_BUDGET_LEDGER={container_dir}/ledger.jsonl",
        f"SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION={TOKEN_RESERVATION}",
        f"SAGE_EVAL_MAX_OUTPUT_TOKENS={CEILINGS['output_tokens_per_call']}",
        ENV_END,
        "",
    ])


def block_keys(block: str) -> set[str]:
    return {line.split("=", 1)[0] for line in block.splitlines() if "=" in line and not line.startswith("#")}


def preempted_keys(local_env: bytes, block: str) -> list[str]:
    """Check keys that the installed ``.env.local`` already sets: it loads first, so it would win."""
    keys = block_keys(block)
    found = []
    for line in local_env.decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in keys and value.strip():
            found.append(key.strip())
    return sorted(found)


def insert_block(original: bytes, block: str) -> bytes:
    if ENV_BEGIN.encode() in original:
        raise SystemExit("the installed .env already carries a check block; run cleanup first")
    prefix = original if not original or original.endswith(b"\n") else original + b"\n"
    return prefix + block.encode()


# With a ledger configured, Sage also meters its Mythic control plane (RabbitMQ RPC, GraphQL) as
# `control_requests`, from the moment it starts. Those are not a protocol ceiling; the limit only has to be
# out of reach. The ledger's calendar starts at its first event, which is Sage's startup, not the run, so it
# spans the arm-to-run window; the run itself enforces the protocol's 20-minute active ceiling and verify checks it.
LEDGER_CALENDAR_SECONDS = 4 * 3600
CONTROL_REQUEST_LIMIT = 1_000_000


def budget_config() -> dict[str, Any]:
    return {"schema": "sage-shared-resource-budget-v1",
            "resources": {"provider_requests": {"limit": CEILINGS["transport_requests"] + 1, "cleanup_reserve": 1},
                          "provider_tokens": {"limit": CEILINGS["provider_tokens"] + 1, "cleanup_reserve": 1},
                          "control_requests": {"limit": CONTROL_REQUEST_LIMIT, "cleanup_reserve": 1000}},
            "calendar_seconds": LEDGER_CALENDAR_SECONDS,
            "cleanup_seconds": CEILINGS["cleanup_seconds"]}


def provider_reservations(ledger_lines: list[str]) -> int:
    """Provider reservations recorded in a ledger (control-plane reservations are not provider calls)."""
    count = 0
    for line in ledger_lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "reservation" and "provider_requests" in (event.get("amounts") or {}):
            count += 1
    return count


# --------------------------------------------------------------------------------------------------
# Wire evidence (pure, tested offline)
# --------------------------------------------------------------------------------------------------

def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value)
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def exchanges(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each captured model request with its response, in request order, with the fields the rules read."""
    responses = {}
    requests = []
    for record in records:
        inner = record.get("record") or {}
        payload = inner.get("payload") or {}
        if record.get("kind") == "model_request":
            requests.append(payload)
        elif record.get("kind") == "model_response":
            responses[payload.get("request_id")] = payload
    out = []
    for payload in requests:
        body = _decode(payload.get("body_base64")) or {}
        url = str(payload.get("url") or "")
        api = "responses" if url.rstrip("/").endswith("/responses") else (
            "chat_completions" if url.rstrip("/").endswith("/chat/completions") else "other")
        effort = ((body.get("reasoning") or {}).get("effort") if api == "responses"
                  else body.get("reasoning_effort"))
        tools = [str((tool.get("function") or {}).get("name") or tool.get("name") or "")
                 for tool in body.get("tools") or [] if isinstance(tool, dict)]
        response = responses.get(payload.get("request_id")) or {}
        reply = _decode(response.get("body_base64")) or {}
        out.append({
            "request_id": payload.get("request_id"), "url": url, "api": api, "model": body.get("model"),
            "effort": effort, "tools": tools, "store": body.get("store"),
            "access_program": ((body.get("access_programs") or {}).get("cyber")
                               if isinstance(body.get("access_programs"), dict) else None),
            "max_output": body.get("max_output_tokens", body.get("max_completion_tokens", body.get("max_tokens"))),
            "input": body.get("input") if api == "responses" else body.get("messages"),
            "status": response.get("status_code"), "outcome": response.get("outcome"),
            "reply": reply if isinstance(reply, dict) else {},
        })
    return out


def _calls(reply: dict[str, Any]) -> list[tuple[str, str]]:
    return [(str(item.get("call_id") or ""), str(item.get("name") or ""))
            for item in reply.get("output") or [] if isinstance(item, dict) and item.get("type") == "function_call"]


def _reply_text(reply: dict[str, Any]) -> str:
    parts = []
    for item in reply.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            parts.extend(str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict))
    return "".join(parts).strip()


def _input_outputs(items: Any) -> dict[str, str]:
    """call_id -> function_call_output text in a Responses input (immediately after its call)."""
    found = {}
    items = items if isinstance(items, list) else []
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("type") == "function_call" and index + 1 < len(items):
            following = items[index + 1]
            if (isinstance(following, dict) and following.get("type") == "function_call_output"
                    and following.get("call_id") == item.get("call_id")):
                found[str(item.get("call_id"))] = str(following.get("output") or "")
    return found


def _input_text(items: Any) -> str:
    return json.dumps(items if items is not None else [], ensure_ascii=False)


def _usage(reply: dict[str, Any]) -> tuple[int, int]:
    usage = reply.get("usage") or {}
    total = usage.get("total_tokens")
    output = usage.get("output_tokens", usage.get("completion_tokens"))
    return (int(total) if isinstance(total, int) else 0, int(output) if isinstance(output, int) else 0)


def rejection_kind(exchange: dict[str, Any]) -> str:
    """The production classifier's reading of a captured 400 (``""`` when it is not an effort rejection)."""
    sys.path.insert(0, str(RUNTIME))
    from ai.langgraph.operator_error import _effort_rejection

    error = exchange["reply"].get("error") if isinstance(exchange["reply"].get("error"), dict) else {}
    return _effort_rejection(int(exchange.get("status") or 0), str(error.get("code") or ""),
                             str(error.get("param") or ""), str(error.get("message") or ""))


def independent_rejection(exchange: dict[str, Any], kind: str) -> bool:
    """The check's own reading of the provider's 400, independent of the classifier under test."""
    error = exchange["reply"].get("error") if isinstance(exchange["reply"].get("error"), dict) else {}
    text = " ".join(str(error.get("message") or "").casefold().split())
    names_effort = bool(re.search(r"reasoning[ _.]effort", text)) or str(error.get("param") or "") in {
        "reasoning_effort", "reasoning.effort"}
    if int(exchange.get("status") or 0) != 400 or not names_effort:
        return False
    if kind == "tools_with_effort":
        return "tools" in text and "chat/completions" in text
    return (bool(re.search(r"\bnone\b", text)) and "support" in text and "tools" not in text)


def native_answers(rows: dict[str, Any]) -> list[str]:
    texts = [str(m.get("message") or "").strip() for m in rows.get("messages", [])
             if "tool_use" not in (m.get("metadata") or {})]
    return [text for text in texts if text and not text.startswith(NOTICE_PREFIXES)]


def evaluate(run: dict[str, Any], wire: dict[str, list[dict[str, Any]]], rows: dict[str, dict[str, Any]],
             totals: dict[str, Any]) -> dict[str, Any]:
    """Criteria 1-5 of the check specification, each with its counts, from captured wire and Mythic rows."""
    plan = {step: (channel, api, effort, kind) for step, channel, api, effort, _prompt, kind in STEPS}
    steps_run = [step for step, *_ in STEPS if step in (run.get("steps") or {})]
    complete_set = steps_run == [step for step, *_ in STEPS]
    detail: dict[str, Any] = {}

    identity_ok = complete_set
    for step in steps_run:
        channel, api, effort, kind = plan[step]
        found = wire.get(step, [])
        problems = []
        if not found:
            problems.append("no captured request")
        for item in found:
            if item["api"] != api or item["model"] != EXPECTED_MODELS[channel] or item["effort"] != effort:
                problems.append(f"identity {item['api']}/{item['model']}/{item['effort']}")
            if "list_callbacks" not in item["tools"]:
                problems.append("tools omitted")
            if api == "responses" and (item["store"] is not False or item["access_program"] != "standard"):
                problems.append(f"store={item['store']} daybreak={item['access_program']}")
            if not str(item["url"]).startswith(DIRECT_ENDPOINT):
                problems.append("endpoint")
        detail.setdefault("identity", {})[step] = problems or "ok"
        identity_ok = identity_ok and not problems

    def rejected(step: str, kind: str, action: str, extra: str = "") -> tuple[bool, dict[str, Any]]:
        found = wire.get(step, [])
        texts = "\n".join(str(m.get("message") or "") for m in rows.get(step, {}).get("messages", []))
        refused = [item for item in found if item["status"] == 400]
        checks = {
            "provider_400": bool(refused) and len(refused) == len(found),
            "classified": bool(refused) and all(rejection_kind(item) == kind for item in refused),
            "provider_said_so": bool(refused) and all(independent_rejection(item, kind) for item in refused),
            "error_terminal": rows.get(step, {}).get("status") == "error",
            "action": texts.count(action) == 1 and (extra in texts if extra else True),
            "no_partial_work": PARTIAL_WORK not in texts,
            "no_task": (run["steps"].get(step) or {}).get("tasks_after") == run.get("task_baseline"),
        }
        return all(checks.values()), checks

    a2_ok, a2 = rejected("A2", "tools_with_effort", TOOLS_ACTION, TOOLS_KEEPS)
    b2_ok, b2 = rejected("B2", "effort_value", EFFORT_ACTION)
    detail["rejections"] = {"A2": a2, "B2": b2}

    def round_trip(step: str) -> tuple[bool, dict[str, Any]]:
        found = [item for item in wire.get(step, []) if item["status"] == 200]
        calls = [(index, call_id) for index, item in enumerate(found)
                 for call_id, name in _calls(item["reply"]) if name == "list_callbacks" and call_id]
        consumed = [(index, call_id) for index, call_id in calls
                    if any(call_id in _input_outputs(later["input"]) and _input_outputs(later["input"])[call_id]
                           for later in found[index + 1:])]
        final = _reply_text(found[-1]["reply"]) if found else ""
        answers = native_answers(rows.get(step, {}))
        ends_in_answer = bool(found) and not _calls(found[-1]["reply"]) and bool(final)
        same_identity = len({(item["model"], item["effort"]) for item in found}) == 1
        native = bool(answers) and answers.count(answers[-1]) == 1
        matches = bool(final) and bool(answers) and (final[:60] in answers[-1] or answers[-1][:60] in final)
        checks = {"call_produced": bool(calls), "result_consumed_later": bool(consumed),
                  "answer_after_result": ends_in_answer and (not consumed or consumed[0][0] < len(found) - 1),
                  "same_model_effort": same_identity, "one_native_answer": native, "answer_from_provider": matches,
                  "complete": rows.get(step, {}).get("status") == "complete"}
        return all(checks.values()), checks

    trips = {step: round_trip(step) for step in ("A1", "A3", "B1", "B3")}
    detail["round_trips"] = {step: checks for step, (_ok, checks) in trips.items()}

    def continuity(step: str, earlier: str) -> tuple[bool, dict[str, Any]]:
        found = wire.get(step, [])
        first = found[0] if found else {"input": None}
        history = _input_text(first["input"])
        earlier_calls = [call_id for item in wire.get(earlier, []) for call_id, _ in _calls(item["reply"])]
        texts = "\n".join(str(m.get("message") or "") for m in rows.get(step, {}).get("messages", []))
        answers = native_answers(rows.get(step, {}))
        checks = {"label_in_history": LABEL in history,
                  "earlier_call_and_result": bool(earlier_calls) and all(c in history for c in earlier_calls)
                  and "function_call_output" in history,
                  "no_refusal": not any(refusal in texts for refusal in REFUSALS),
                  "one_answer": len(answers) >= 1 and answers.count(answers[-1]) == 1,
                  "no_reasoning_leak": "encrypted_content" not in texts and not REASONING_ID.search(texts),
                  "complete": rows.get(step, {}).get("status") == "complete"}
        return all(checks.values()), checks

    continuity_checks = {"A3": continuity("A3", "A1"), "A4": continuity("A4", "A1"),
                         "B3": continuity("B3", "B1"), "B4": continuity("B4", "B1")}
    detail["continuity"] = {step: checks for step, (_ok, checks) in continuity_checks.items()}

    every = [item for step in steps_run for item in wire.get(step, [])]
    tokens = sum(_usage(item["reply"])[0] for item in every if item["status"] == 200)
    unsettled = sum(1 for item in every if item["status"] != 200) * TOKEN_RESERVATION
    max_output = max((_usage(item["reply"])[1] for item in every), default=0)
    terminal = all((rows.get(step, {}).get("status") in {"complete", "error"}) for step in steps_run)
    tasks = all((run["steps"][step] or {}).get("tasks_after") == run.get("task_baseline") for step in steps_run)
    paired = (all(item["status"] is not None or item["outcome"] is not None for item in every)
              and int(totals.get("unpaired", 0)) == 0)
    containment = {
        "all_terminal": terminal and complete_set, "task_baseline_unchanged": tasks,
        "transport_requests": int(totals.get("transport_requests", len(every))),
        "provider_tokens_charged": tokens + unsettled, "max_output_tokens": max_output,
        "active_seconds": round(float(run.get("finished_at", 0)) - float(run.get("started_at", 0)), 1),
        "captures_paired": paired, "stopped": run.get("stopped"),
    }
    containment_ok = (containment["all_terminal"] and tasks and paired and not run.get("stopped")
                      and containment["transport_requests"] <= CEILINGS["transport_requests"]
                      and containment["provider_tokens_charged"] <= CEILINGS["provider_tokens"]
                      and max_output <= CEILINGS["output_tokens_per_call"]
                      and containment["active_seconds"] <= CEILINGS["active_seconds"])
    detail["containment"] = containment

    criteria = {
        "1_identity": identity_ok,
        "2_both_rejections": a2_ok and b2_ok,
        "3_both_recovered_seams": all(ok for ok, _ in trips.values()),
        "4_continuity_output": all(ok for ok, _ in continuity_checks.values()),
        "5_containment_evidence": containment_ok,
    }
    return {"criteria": criteria, "detail": detail, "verdict": "pass" if all(criteria.values()) else "fail"}


# --------------------------------------------------------------------------------------------------
# Receiver process (host side; Sage in the container runs as the same uid over the bind mount)
# --------------------------------------------------------------------------------------------------

def _serve(record: Path, parent: Path, ready: Path) -> int:
    receiver_module = _module("sage_reasoning_check_receiver", _RECEIVER_PATH)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))

    def handler(event):
        return {"time_ns": event["time_ns"], "payload": event["payload"]}

    with receiver_module.ObservationReceiver(record, handler, temporary_parent=parent) as receiver:
        ready.write_text(json.dumps({"directory": str(receiver.directory), "pid": os.getpid()}))
        while not stop["now"] and not (parent / "stop").exists():
            try:
                receiver.serve_one(1.0)
            except receiver_module.ObservationReceiverTimeout:
                continue
            except receiver_module.ObservationReceiverError as error:
                (parent / "receiver-failed").write_text(str(getattr(error, "reason", error)))
                return 1
    return 0


def read_wire(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------------------------------
# Live stages
# --------------------------------------------------------------------------------------------------

def _base():
    return _module("sage_reasoning_check_base", _BASE_PATH)


def _deployment():
    return _module("sage_reasoning_check_deployment", _DEPLOYMENT_PATH)


def _run_json(command: list[str], timeout: int = 300) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout)
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip().startswith("{") else {}
    except ValueError:
        payload = {}
    return {"returncode": completed.returncode, "payload": payload,
            "tail": completed.stdout.strip().splitlines()[-3:]}


def _container_adapters() -> dict[str, str]:
    script = ("import json,importlib.metadata as m;print(json.dumps({n:m.version(n) for n in "
              + json.dumps(sorted(EXPECTED_ADAPTER)) + "}))")
    completed = subprocess.run(["docker", "exec", "sage", "python3", "-c", script], capture_output=True, text=True,
                               timeout=60)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": completed.stderr.strip()[-200:]}


def _endpoint_reachability(endpoint: str) -> dict[str, Any]:
    """Keyless GET of /models with verified TLS: reachability only (expects 401), never inference."""
    sys.path.insert(0, str(RUNTIME))
    import httpx

    from ssl_bootstrap import configure_sage_ssl_cert_file

    bundle = configure_sage_ssl_cert_file()
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    try:
        response = httpx.get(endpoint.rstrip("/") + "/models", timeout=15, verify=context)
        return {"status": response.status_code, "tls_verified": True, "custom_bundle": bool(bundle)}
    except Exception as error:
        return {"error": type(error).__name__}


def _credential_source(installed: Path) -> dict[str, Any]:
    """Where the provider key comes from, by name only; no value is read into the result."""
    names = ("API_KEY", "OPENAI_API_KEY")
    in_file = []
    for env_file in (installed / ".env.local", installed / ".env"):  # Sage's dotenv order; first set wins
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                key, _, value = line.partition("=")
                if key.strip() in names and value.strip():
                    in_file.append(f"{env_file.name}:{key.strip()}")
    probe = subprocess.run(["docker", "exec", "sage", "sh", "-c",
                            'for n in API_KEY OPENAI_API_KEY; do eval "v=\\${$n}"; [ -n "$v" ] && echo $n; done'],
                           capture_output=True, text=True, timeout=30)
    return {"installed_env": sorted(set(in_file)), "container_env": probe.stdout.split(),
            "user_secret": "not inspected (a configured API_KEY user secret also qualifies)"}


def _capture_state(run: dict[str, Any]) -> dict[str, Any]:
    capture = run.get("capture") or {}
    if not capture:
        return {"armed": False}
    parent = Path(capture["host_dir"])
    wire = read_wire(Path(capture["record"]))
    started = [r for r in wire if r.get("kind") == "process_start"]
    after = [r for r in started if int((r.get("record") or {}).get("time_ns") or 0) >= int(capture["armed_ns"])]
    ledger = Path(capture["host_dir"]) / "ledger.jsonl"
    lines = ledger.read_text().splitlines() if ledger.exists() else []
    return {"armed": True, "receiver_alive": _pid_alive(int(capture["receiver_pid"])),
            "receiver_failed": (parent / "receiver-failed").exists(),
            "process_start_after_arm": len(after), "events": len(wire),
            "budget_valid": (parent / "budget.json").exists(),
            "ledger_events": len(lines), "provider_reservations": provider_reservations(lines),
            "env_block_present": ENV_BEGIN.encode() in (Path(capture["installed"]) / ".env").read_bytes()}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    route = read_route(Path(args.route_file))
    deployment = _deployment()
    installed = deployment._installed_service_dir()
    readiness = await base._query(client, base.nc.READINESS_QUERY)
    containers = readiness.get("consuming_container") or []
    parity = _run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container", "parity"])
    check = _run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container", "check"])
    report = {
        "route": {"endpoint": route["API_ENDPOINT"], "models": [route["MODEL_A"], route["MODEL_B"]],
                  "effort": route["EFFORT"], "daybreak": "blank (Sage sends Off on Responses)"},
        "deployment_check": {"returncode": check["returncode"], "tail": check["tail"]},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "identity": _identity(),
        "container_adapter": _container_adapters(),
        "phoenix_readable": bool(args.phoenix_db) and Path(args.phoenix_db).is_file(),
        "task_baseline": await base._max_task(client),
        "endpoint": _endpoint_reachability(route["API_ENDPOINT"]),
        "credential_source": _credential_source(installed),
        "capture": _capture_state(run),
        "ceilings": CEILINGS, "token_reservation": TOKEN_RESERVATION, "retry_policy": RETRY_POLICY,
        "check_sha256": _sha256(Path(__file__)),
    }
    if report["phoenix_readable"]:
        report["phoenix_mark"] = base.phoenix_mark(Path(args.phoenix_db))
    blockers = []
    if report["deployment_check"]["returncode"] != 0:
        blockers.append("deployment check")
    if not report["sage_container_running"]:
        blockers.append("Sage container not consuming")
    if report["open_requests"]:
        blockers.append("open chat requests")
    if report["identity"]["dirty"]:
        blockers.append("working tree dirty")
    if report["container_adapter"] != EXPECTED_ADAPTER:
        blockers.append("container adapter versions")
    if not report["phoenix_readable"]:
        blockers.append("Phoenix unreadable")
    if report["endpoint"].get("status") != 401:
        blockers.append("endpoint reachability")
    if not (report["credential_source"]["installed_env"] or report["credential_source"]["container_env"]):
        report["credential_source"]["note"] = "no key in the installed .env or container env; a user secret must supply it"
    capture = report["capture"]
    if not report["parity"]["ready"]:
        verdict = "deployment_required"
    elif blockers:
        verdict = "not_ready"
    elif not capture.get("armed"):
        verdict = "capture_required"
    elif not (capture["receiver_alive"] and not capture["receiver_failed"] and capture["process_start_after_arm"]
              and capture["budget_valid"] and capture["env_block_present"]
              and capture["provider_reservations"] == 0 and report["sage_container_running"]):
        verdict = "not_ready"
        blockers.append("capture or budget not live")
    else:
        verdict = "ready_for_live"
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


async def cmd_arm(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("capture"):
        raise SystemExit("already armed; run cleanup first")
    if (run.get("readiness") or {}).get("verdict") != "capture_required":
        raise SystemExit("readiness must report capture_required (feature deployed, parity passing) before arming")
    deployment = _deployment()
    installed = Path(deployment._installed_service_dir())
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    host_dir = _private_dir(installed / f".reasoning-check-{stamp}")
    container_dir = f"/Mythic/{host_dir.name}"
    evidence = evidence_dir(run)
    record, ready = evidence / "wire.jsonl", host_dir / "receiver-ready.json"
    (host_dir / "budget.json").write_text(json.dumps(budget_config(), sort_keys=True))
    os.chmod(host_dir / "budget.json", 0o600)
    receiver = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_receive", "--record", str(record), "--parent",
         str(host_dir), "--ready", str(ready)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(evidence / "receiver.log", "ab"),
        start_new_session=True)
    deadline = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not ready.exists():
        receiver.terminate()
        raise SystemExit("the wire receiver did not start")
    fifo_host = Path(json.loads(ready.read_text())["directory"])
    fifo_container = f"{container_dir}/{fifo_host.name}"
    env_path = installed / ".env"
    block = env_block(container_dir, fifo_container)
    local = installed / ".env.local"
    preempted = preempted_keys(local.read_bytes() if local.exists() else b"", block)
    if preempted:
        (host_dir / "stop").write_text("")
        raise SystemExit(f"the installed .env.local already sets {', '.join(preempted)}; it would override the check")
    original = env_path.read_bytes()
    backup = evidence / "installed-env.original"
    backup.write_bytes(original)
    os.chmod(backup, 0o600)
    armed_ns = time.time_ns()
    run["capture"] = {"host_dir": str(host_dir), "container_dir": container_dir, "record": str(record),
                      "receiver_pid": receiver.pid, "installed": str(installed), "env_backup": str(backup),
                      "env_original_sha256": hashlib.sha256(original).hexdigest(), "armed_ns": armed_ns,
                      "authorized_by": args.authorized_by}
    env_path.write_bytes(insert_block(original, block))
    started = deployment._start_container()
    run["capture"]["start"] = {"returncode": started.get("returncode")}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not any(
            r.get("kind") == "process_start" and int((r.get("record") or {}).get("time_ns") or 0) >= armed_ns
            for r in read_wire(record)):
        time.sleep(2)
    return {"armed": True, "start_returncode": started.get("returncode"), "capture": _capture_state(run)}


async def cmd_cleanup(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    actions: dict[str, Any] = {}
    for channel_id in (run.get("channels") or {}).values():
        rows = (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or []
        for row in rows:
            if int(row.get("channel_id") or 0) == int(channel_id):
                await base.nc.mythic.execute_custom_query(client, base.nc.CANCEL_REQUEST_MUTATION,
                                                          variables={"request_id": int(row["id"])})
                actions.setdefault("cancelled", []).append(int(row["id"]))
    capture = run.get("capture") or {}
    if capture:
        env_path = Path(capture["installed"]) / ".env"
        original = Path(capture["env_backup"]).read_bytes()
        if hashlib.sha256(original).hexdigest() != capture["env_original_sha256"]:
            raise SystemExit("the saved .env backup does not match its recorded hash; restore it by hand")
        if env_path.read_bytes() != original:
            env_path.write_bytes(original)
        actions["env_restored"] = hashlib.sha256(env_path.read_bytes()).hexdigest() == capture["env_original_sha256"]
        actions["start"] = _deployment()._start_container().get("returncode")
        (Path(capture["host_dir"]) / "stop").write_text("")
        pid = int(capture["receiver_pid"])
        deadline = time.monotonic() + 15
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
        actions["receiver_stopped"] = not _pid_alive(pid)
        ledger = Path(capture["host_dir"]) / "ledger.jsonl"
        if ledger.exists():
            (Path(run["evidence_dir"]) / "ledger.jsonl").write_bytes(ledger.read_bytes())
    actions["open_requests_after"] = (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or []
    run["cleanup"] = {**actions, "at": time.time()}
    if capture and actions.get("env_restored") and actions.get("receiver_stopped"):
        run["capture_released"] = run.pop("capture")
    return run["cleanup"]


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness has not reported ready_for_live in this run file")
    if run.get("steps"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    base = _base()
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    record = Path(run["capture"]["record"])
    run.update(authorized_by=args.authorized_by, started_at=time.time(), identity=_identity(),
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix),
               steps={}, channels={})
    started = time.monotonic()
    try:
        for step, channel, api, effort, prompt, _kind in STEPS:
            config = channel_config(route, channel, api=api, effort=effort)
            if channel not in run["channels"]:
                created = await base.nc.create_locked_channel(
                    client, name=f"Reasoning tools live check {channel}",
                    description="Sage reasoning-enabled tool recovery live check", metadata=config)
                run["channels"][channel] = int(created["chat_channel_id"])
            else:
                await base._query(client, base.UPDATE_CHANNEL, channelId=run["channels"][channel], metadata=config)
            wire_before, mark = len(read_wire(record)), base.phoenix_mark(phoenix)
            message = await base.nc.create_message(client, run["channels"][channel], prompt)
            request_id = int(message["chat_request_id"])
            await base.nc.wait_for_request(client, request_id, timeout_seconds=STEP_TIMEOUT)
            run["steps"][step] = {"request_id": request_id, "wire_after": wire_before, "wire_until": len(read_wire(record)),
                                  "phoenix_after": mark, "phoenix_until": base.phoenix_mark(phoenix),
                                  "tasks_after": await base._max_task(client)}
            items = exchanges(read_wire(record)[wire_before:run["steps"][step]["wire_until"]])
            stop = ""
            if run["steps"][step]["tasks_after"] != run["task_baseline"]:
                stop = "a target task was created"
            elif any(item["model"] != EXPECTED_MODELS[channel] or item["api"] != api for item in items):
                stop = "wrong model or route"
            elif len(exchanges(read_wire(record))) > CEILINGS["transport_requests"]:
                stop = "transport request ceiling"
            elif time.monotonic() - started > CEILINGS["active_seconds"]:
                stop = "active wall-time ceiling"
            if stop:
                run["stopped"] = {"step": step, "reason": stop}
                break
    finally:
        run["finished_at"] = time.time()
        run["cleanup_result"] = await cmd_cleanup(client, run, args)
    return {"channels": run["channels"], "steps": run["steps"], "stopped": run.get("stopped"),
            "cleanup": run["cleanup_result"]}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verify"):
        raise SystemExit("this run was already read; a result is read once")
    if not run.get("steps"):
        raise SystemExit("this run file holds no attempt")
    base = _base()
    capture = run.get("capture_released") or run.get("capture") or {}
    records = read_wire(Path(capture["record"]))
    wire = {step: exchanges(records[info["wire_after"]:info["wire_until"]]) for step, info in run["steps"].items()}
    rows = {step: await base.request_rows(client, int(info["request_id"])) for step, info in run["steps"].items()}
    all_items = exchanges(records)
    totals = {"transport_requests": len(all_items),
              "unpaired": sum(1 for item in all_items if item["status"] is None and item["outcome"] is None)}
    result = evaluate(run, wire, rows, totals)
    phoenix = Path(args.phoenix_db)
    result["observations"] = {
        "phoenix_llm_spans": {step: len(base.model_spans(phoenix, info["phoenix_after"], info["phoenix_until"]))
                              for step, info in run["steps"].items()},
        "reasoning_items": sum(1 for item in all_items for out in item["reply"].get("output") or []
                               if isinstance(out, dict) and out.get("type") == "reasoning"),
        "answers": {step: (native_answers(rows[step]) or [""])[-1][:300] for step in rows},
    }
    run["verify"] = result
    return result


async def _main(args: argparse.Namespace) -> int:
    if args.command == "_receive":
        return _serve(Path(args.record), Path(args.parent), Path(args.ready))
    base = _base()
    run_path = Path(args.run_file)
    run = base._load(run_path)
    client = await base.nc.login()
    handlers = {"readiness": cmd_readiness, "arm": cmd_arm, "run": cmd_run, "cleanup": cmd_cleanup,
                "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        base._save(run_path, run)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("readiness", "arm", "run", "cleanup", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--run-file", required=True)
        if name in {"readiness", "run", "verify"}:
            command.add_argument("--phoenix-db", required=True,
                                 help="the deployed Sage's live phoenix.db, read in place (read-only)")
        if name in {"readiness", "run"}:
            command.add_argument("--route-file", required=True)
        if name in {"arm", "run"}:
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this deployment change or attempt")
    receive = sub.add_parser("_receive", help=argparse.SUPPRESS)
    receive.add_argument("--record", required=True)
    receive.add_argument("--parent", required=True)
    receive.add_argument("--ready", required=True)
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
