#!/usr/bin/env python3
"""Live check for OpenAI Responses support and Daybreak controls in one native Sage chat.

Steps share one run file (JSON). ``readiness`` and ``verify`` only read; ``run`` is authorized live activity:
it creates one conversation channel, sends four prompts and edits that channel's configuration once.

  readiness --run-file F --route-file R [--phoenix-db P]
      Mythic login; Sage chat container running and consuming; no open chat requests; deployed source
      identity; installed adapter versions; the offline Responses tests pass; the route file names the direct
      OpenAI endpoint, the exact model and a safety identifier (the API key is never in the route file or the
      channel: the deployment's API_KEY user secret or container environment supplies it).
  run --run-file F --route-file R --phoenix-db P --authorized-by TEXT
      1. Chat Completions on purpose: the label prompt must fail with the configuration action.
      2. The channel's configuration is corrected to Responses + Red; the label prompt is sent again.
      3. The read-only tool prompt (list_callbacks) bypasses the deterministic inventory shortcut.
      4. The label is repeated without tools.
      Stops at the first step that breaks a ceiling (time, provider calls, tokens, any target task).
  verify --run-file F --phoenix-db P
      Applies the pass rules below once, reading Mythic's own rows and the live Phoenix trace.

Pass rules (written before any run; each is read once):
  P1 step 1 ends in one error terminal that names "set **OpenAI API** to **Responses**"; no replaced/restart
     refusal appears in the channel.
  P2 step 2 completes with real model text and the settings notice; no refusal.
  P3 step 3 completes; Phoenix shows a model-produced function call whose call id is consumed by a later
     model request in the same step as a tool result; Mythic shows the list_callbacks card once.
  P4 step 4 completes and its answer contains the label (history preserved across the correction).
  P5 Phoenix model spans in steps 2-4 name the configured model; every request ends terminal.
  P6 zero target tasks created; provider calls, tokens and wall time stayed inside the ceilings.
Observations, not pass criteria: wording, latency, callback count, reasoning tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[3]
_NATIVE_CHAT = REPO_ROOT / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
_spec = importlib.util.spec_from_file_location("sage_native_chat_for_openai_check", _NATIVE_CHAT)
nc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nc
_spec.loader.exec_module(nc)  # type: ignore[union-attr]

LABEL = "amber-seven"
PROMPTS = {
    "chat_completions": f"Remember the label {LABEL}. Explain what an API endpoint is in one sentence.",
    "corrected": f"Remember the label {LABEL}. Explain what an API endpoint is in one sentence.",
    "tool": ("For a read-only status check, use list_callbacks once, then tell me the label from the previous "
             "turn and summarize the returned callback list. Do not run tasks."),
    "repeat": "Repeat my label without using tools.",
}
STEPS = tuple(PROMPTS)
ACTION = "set **OpenAI API** to **Responses**"
REFUSALS = ("Sage replaced this channel's session", "Sage restarted or this channel's session was replaced")
SETTINGS_NOTICE = "Applied this chat's updated OpenAI settings"
CEILINGS = {"provider_calls": 16, "total_tokens": 1_048_576, "output_tokens_per_call": 4096,
            "minutes": 15, "target_tasks": 0, "retries": 0}
MAX_STEPS = 4  # per request; with four requests this keeps graph model steps inside the call ceiling
EXPECTED_ADAPTER = {"langchain-openai": "1.4.2", "openai": "2.54.0", "langchain": "1.3.14"}
CHANGED_FILES = (
    "Payload_Type/sage/ai/openai_api.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/operator_error.py",
    "Payload_Type/sage/sage_chat/config.py",
    "Payload_Type/sage/sage_chat/models.py",
    "Payload_Type/sage/sage_chat/service.py",
    "Payload_Type/sage/sage_chat/session.py",
)
OFFLINE_TESTS = (
    "tests/test_openai_responses_route.py",
    "tests/test_openai_responses_native_chat.py",
    "tests/test_openai_responses_consumers.py",
)
TERMINAL = {"complete", "error", "cancelled"}

UPDATE_CHANNEL = """
mutation SageOpenAICheckUpdate($channelId: Int!, $metadata: jsonb!) {
  chatUpdateChannel(channel_id: $channelId, ai_metadata: $metadata) { status error }
}
"""
CHANNEL_QUERY = "query SageOpenAICheckChannel($id: Int!) { chat_channel_by_pk(id: $id) { id ai_metadata } }"
MAX_TASK_QUERY = "query SageOpenAICheckMaxTask { task(order_by: {display_id: desc}, limit: 1) { display_id } }"
OPEN_REQUESTS_QUERY = """
query SageOpenAICheckOpen { chat_request(where: {status: {_in: ["pending", "streaming"]}}) { id channel_id status } }
"""
REQUEST_MESSAGES_QUERY = """
query SageOpenAICheckMessages($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message metadata }
  chat_request_by_pk(id: $rid) { id status }
}
"""


# --------------------------------------------------------------------------------------------------
# Files and identity
# --------------------------------------------------------------------------------------------------

def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {"steps": {}}


def _save(path: Path, run: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def read_route(path: Path) -> dict[str, str]:
    """The private route file: KEY=VALUE lines for API_ENDPOINT, model and SAGE_OPENAI_SAFETY_IDENTIFIER.

    Must be owner-only and must not hold an API key: the key stays in the deployment's secret store.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"{path} must be owner-only (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if any(key.upper().endswith("API_KEY") for key in values):
        raise SystemExit(f"{path} must not contain an API key; use the API_KEY user secret or container env")
    missing = [key for key in ("API_ENDPOINT", "model", "SAGE_OPENAI_SAFETY_IDENTIFIER") if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    return values


def route_summary(route: dict[str, str]) -> dict[str, Any]:
    return {"endpoint": route["API_ENDPOINT"], "model": route["model"], "safety_identifier": "configured",
            "direct_openai": route["API_ENDPOINT"].rstrip("/") == "https://api.openai.com/v1"}


def channel_config(route: dict[str, str], *, api: str, access_program: str = "") -> dict[str, Any]:
    config = {"provider": "openai", "model": route["model"], "API_ENDPOINT": route["API_ENDPOINT"],
              "mode": "conversation", "autonomous_solve": False, "max_steps": MAX_STEPS,
              "SAGE_OPENAI_API": api, "SAGE_OPENAI_SAFETY_IDENTIFIER": route["SAGE_OPENAI_SAFETY_IDENTIFIER"]}
    if access_program:
        config["SAGE_OPENAI_ACCESS_PROGRAM"] = access_program
    return {"config": config,
            "channel_metadata_display": {"display": nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "head": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "Payload_Type/sage")),
            "files": {path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
                      for path in CHANGED_FILES}}


def _adapter_versions() -> dict[str, str]:
    from importlib.metadata import version
    return {name: version(name) for name in EXPECTED_ADAPTER}


def _deployment_check() -> dict[str, Any]:
    command = [str(REPO_ROOT / ".venv/bin/python"),
               str(REPO_ROOT / "skills/sage-goad-reset/scripts/sage_deployment.py"), "check"]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT)
    return {"returncode": completed.returncode, "tail": completed.stdout.strip().splitlines()[-3:]}


def _offline_tests() -> dict[str, Any]:
    completed = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", *OFFLINE_TESTS],
                               capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type/sage")
    return {"returncode": completed.returncode, "summary": completed.stdout.strip().splitlines()[-1:]}


# --------------------------------------------------------------------------------------------------
# Phoenix (read-only, in place; never a copy of a WAL database)
# --------------------------------------------------------------------------------------------------

def _phoenix(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(db.resolve()), safe='/:')}?mode=ro", uri=True)


def phoenix_mark(db: Path) -> int:
    with _phoenix(db) as conn:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM spans").fetchone()[0])


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, path)
    else:
        yield path, value


def model_spans(db: Path, after: int, until: int | None = None) -> list[dict[str, Any]]:
    """LLM spans in (after, until]: model name, token counts, produced call ids and consumed tool-result ids."""
    query = ("SELECT id, attributes, COALESCE(llm_token_count_prompt, 0), COALESCE(llm_token_count_completion, 0) "
             "FROM spans WHERE id > ? AND span_kind = 'LLM'" + (" AND id <= ?" if until else "") + " ORDER BY id")
    with _phoenix(db) as conn:
        rows = conn.execute(query, (after, until) if until else (after,)).fetchall()
    spans = []
    for span_id, attributes, prompt, completion in rows:
        try:
            attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
        except ValueError:
            attrs = {}
        llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
        produced, consumed = set(), set()
        for path, value in _walk(llm.get("output_messages") or []):
            if isinstance(value, str) and path.endswith("tool_call.id"):
                produced.add(value)
        for path, value in _walk(llm.get("input_messages") or []):
            if isinstance(value, str) and path.endswith("tool_call_id"):
                consumed.add(value)
        spans.append({"id": span_id, "model": str(llm.get("model_name") or ""),
                      "prompt_tokens": int(prompt), "completion_tokens": int(completion),
                      "produced_calls": sorted(produced), "consumed_results": sorted(consumed),
                      "invocation_parameters": str(llm.get("invocation_parameters") or "")[:2000]})
    return spans


# --------------------------------------------------------------------------------------------------
# Mythic
# --------------------------------------------------------------------------------------------------

async def _query(client: Any, query: str, **variables: Any) -> dict[str, Any]:
    result = await nc.mythic.execute_custom_query(client, query, variables=variables)
    return result if isinstance(result, dict) else {}


async def _max_task(client: Any) -> int:
    rows = (await _query(client, MAX_TASK_QUERY)).get("task") or []
    return int(rows[0]["display_id"]) if rows else 0


async def request_rows(client: Any, request_id: int) -> dict[str, Any]:
    data = await _query(client, REQUEST_MESSAGES_QUERY, rid=request_id)
    return {"status": (data.get("chat_request_by_pk") or {}).get("status"),
            "messages": list(data.get("chat_message") or [])}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    route = read_route(Path(args.route_file))
    readiness = await _query(client, nc.READINESS_QUERY)
    containers = readiness.get("consuming_container") or []
    report = {
        "route": route_summary(route),
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await _query(client, OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "deployment": _deployment_check(),
        "identity": _identity(),
        "adapter": _adapter_versions(),
        "offline_tests": _offline_tests(),
        "phoenix_readable": bool(args.phoenix_db) and Path(args.phoenix_db).is_file(),
        "task_baseline": await _max_task(client),
        "ceilings": CEILINGS,
    }
    if report["phoenix_readable"]:
        report["phoenix_mark"] = phoenix_mark(Path(args.phoenix_db))
    report["ready"] = bool(
        report["route"]["direct_openai"] and report["sage_container_running"] and not report["open_requests"]
        and report["deployment"]["returncode"] == 0 and not report["identity"]["dirty"]
        and report["adapter"] == EXPECTED_ADAPTER and report["offline_tests"]["returncode"] == 0
        and report["phoenix_readable"]
    )
    run["readiness"] = report
    return report


def _over_ceiling(spans: list[dict[str, Any]], started: float) -> str:
    if len(spans) > CEILINGS["provider_calls"]:
        return "provider call ceiling"
    if sum(s["prompt_tokens"] + s["completion_tokens"] for s in spans) > CEILINGS["total_tokens"]:
        return "token ceiling"
    if any(s["completion_tokens"] > CEILINGS["output_tokens_per_call"] for s in spans):
        return "output token ceiling"
    if time.monotonic() - started > CEILINGS["minutes"] * 60:
        return "wall-time ceiling"
    return ""


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not (run.get("readiness") or {}).get("ready"):
        raise SystemExit("readiness has not passed in this run file")
    if run.get("steps"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, started_at=time.time(), identity=_identity(),
               task_baseline=await _max_task(client), phoenix_start=phoenix_mark(phoenix))
    started = time.monotonic()
    created = await nc.create_locked_channel(client, name="OpenAI Responses live check",
                                             description="Sage OpenAI Responses and Daybreak live check",
                                             metadata=channel_config(route, api="chat_completions"))
    channel_id = int(created["chat_channel_id"])
    run["channel_id"] = channel_id
    for step in STEPS:
        if step == "corrected":
            update = await _query(client, UPDATE_CHANNEL, channelId=channel_id,
                                  metadata=channel_config(route, api="responses", access_program="daybreak_red"))
            stored = ((await _query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {})
            run["config_update"] = {"result": update.get("chatUpdateChannel"),
                                    "stored_api": ((stored.get("ai_metadata") or {}).get("config") or {})
                                    .get("SAGE_OPENAI_API")}
        mark = phoenix_mark(phoenix)
        message = await nc.create_message(client, channel_id, PROMPTS[step])
        request_id = int(message["chat_request_id"])
        await nc.wait_for_request(client, request_id, timeout_seconds=300)
        run["steps"][step] = {"request_id": request_id, "phoenix_after": mark, "phoenix_until": phoenix_mark(phoenix),
                              "tasks_after": await _max_task(client)}
        spans = model_spans(phoenix, run["phoenix_start"])
        stop = _over_ceiling(spans, started)
        if run["steps"][step]["tasks_after"] != run["task_baseline"]:
            stop = "a target task was created"
        if stop:
            run["stopped"] = {"step": step, "reason": stop}
            break
    run["finished_at"] = time.time()
    return {"channel_id": channel_id, "steps": run["steps"], "stopped": run.get("stopped")}


def _texts(rows: dict[str, Any]) -> list[str]:
    return [str(m.get("message") or "") for m in rows["messages"]]


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    phoenix = Path(args.phoenix_db)
    steps = run.get("steps") or {}
    rows = {step: await request_rows(client, int(info["request_id"])) for step, info in steps.items()}
    spans = {step: model_spans(phoenix, info["phoenix_after"], info["phoenix_until"]) for step, info in steps.items()}
    every = model_spans(phoenix, run.get("phoenix_start", 0))
    all_text = "\n".join(text for step in rows for text in _texts(rows[step]))
    no_refusal = not any(refusal in all_text for refusal in REFUSALS)

    def status(step: str) -> str:
        return str(rows.get(step, {}).get("status") or "")

    first = "\n".join(_texts(rows.get("chat_completions", {"messages": []})))
    tool_spans = spans.get("tool", [])
    produced = {call for span in tool_spans for call in span["produced_calls"]}
    consumed = {call for span in tool_spans for call in span["consumed_results"]}
    tool_cards = [m for m in rows.get("tool", {"messages": []})["messages"]
                  if "list_callbacks" in json.dumps(m.get("metadata") or {})]
    criteria = {
        "P1": status("chat_completions") == "error" and first.count(ACTION) >= 1 and no_refusal,
        "P2": status("corrected") == "complete" and SETTINGS_NOTICE in "\n".join(_texts(rows.get("corrected", {"messages": []})))
        and bool(spans.get("corrected")) and no_refusal,
        "P3": status("tool") == "complete" and bool(produced & consumed) and bool(tool_cards),
        "P4": status("repeat") == "complete" and LABEL in "\n".join(_texts(rows.get("repeat", {"messages": []}))),
        "P5": all(s["model"].startswith(run.get("readiness", {}).get("route", {}).get("model", "?"))
                  for step in ("corrected", "tool", "repeat") for s in spans.get(step, []))
        and all(status(step) in TERMINAL for step in STEPS if step in steps),
        "P6": not run.get("stopped") and all(info["tasks_after"] == run.get("task_baseline") for info in steps.values())
        and _over_ceiling(every, time.monotonic()) == ""  # calls and tokens; wall time is checked next
        and float(run.get("finished_at", 0)) - float(run.get("started_at", 0)) <= CEILINGS["minutes"] * 60,
    }
    observations = {
        "provider_calls": len(every),
        "total_tokens": sum(s["prompt_tokens"] + s["completion_tokens"] for s in every),
        "max_completion_tokens": max((s["completion_tokens"] for s in every), default=0),
        "wall_seconds": round(float(run.get("finished_at", 0)) - float(run.get("started_at", 0)), 1),
        "tool_cards": len(tool_cards),
        "call_ids_produced_and_consumed": sorted(produced & consumed),
        "invocation_parameters_sample": (spans.get("corrected") or [{}])[0].get("invocation_parameters", "")[:600],
    }
    verdict = "pass" if all(criteria.values()) else "fail"
    run["verify"] = {"criteria": criteria, "observations": observations, "verdict": verdict}
    return run["verify"]


async def _main(args: argparse.Namespace) -> int:
    run_path = Path(args.run_file)
    run = _load(run_path)
    client = await nc.login()
    handlers = {"readiness": cmd_readiness, "run": cmd_run, "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        _save(run_path, run)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("readiness", "run", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--run-file", required=True)
        command.add_argument("--phoenix-db", required=name != "readiness",
                             help="the deployed Sage's live phoenix.db, read in place (read-only)")
        if name in {"readiness", "run"}:
            command.add_argument("--route-file", required=True)
        if name == "run":
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this bounded attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
