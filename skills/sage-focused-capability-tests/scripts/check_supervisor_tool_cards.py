#!/usr/bin/env python3
"""Live check: a tool the Supervisor runs itself shows one tool card in Mythic.

The deployed Sage container must run the branch under test. One run file (JSON, owner-only) carries every stage.

  readiness --run-file F --route-file R --phoenix-db P [--phoenix-url U]
      Read-only. Deployment parity with this branch (container mode); Sage consuming; no open chat requests; the
      prompt is not one Sage answers with its deterministic callback-inventory read; at least one active callback
      (so P1's preview rule is not vacuous); Phoenix readable and answering /healthz; the Mythic task baseline;
      this line's offline tests pass. The route credential, by name and status only: exactly one active provider
      block in the installed .env.local, a keyed verified-TLS GET /models from inside the container that lists the
      route's model, no shadowing API_KEY user secret. Verdict: ready_for_live, deployment_required or not_ready.
  run --run-file F --route-file R --phoenix-db P --plan PLAN --authorized-by TEXT
      Runs `sage-line check-attempt PLAN` first and refuses on failure. In a fresh Conversation channel it sends
      PROMPT once. If the request's gate is closed it sends PROMPT once more in a new channel, and no more. It stops
      on any new Mythic task or a limit. No retry.
  verify --run-file F --phoenix-db P
      Applies the pass rules below once to the first gated request, reading Mythic's rows and Phoenix in place.

Gate (the check runs only if it opened): in the request's Phoenix window, a Supervisor LLM span emitted a
`list_callbacks` call and a `list_callbacks` TOOL span under the Supervisor carries that call id. If neither request
opened the gate, the check did not run (verdict not_run) and the line cannot land.

Pass rules (frozen with the plan's live check; read once; all from Mythic's chat rows for the gated request):
  P1 Exactly one tool_use row for the gated call id: tool_name list_callbacks, no delegation_id, tool status
     completed, row status complete. Its result preview names the display id and host of every callback the TOOL
     span returned.
  P2 No tool_use row for respond_to_user or any transfer_*. The final report appears in exactly one row, and no
     other assistant row repeats it or carries a tool-call payload.
  P3 The request is complete, and no row says "Request lifecycle reconciliation failed".
Observations, not pass criteria: model calls, tokens, latency, the answer's wording.
"""

from __future__ import annotations

import argparse
import asyncio
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

PROMPT = "Which Mythic callbacks are alive right now?"
LIMITS = {"requests": 2, "provider_calls": 8, "total_tokens": 150_000, "minutes": 10, "target_tasks": 0}
MAX_STEPS = 6
REQUEST_TIMEOUT = 240
SETTLE_SECONDS = 3
OFFLINE_TESTS = ("tests/test_supervisor_tool_cards.py",)
RECONCILE_FAILURE = "Request lifecycle reconciliation failed"
PAYLOAD_MARKERS = ("Tool Request", '"tool_calls"', "function_call", '"tool_use"')
CALLBACKS_QUERY = "query SageToolCardCallbacks { callback(where: {active: {_eq: true}}) { display_id host } }"
TASKS_AFTER_QUERY = """
query SageToolCardTasks($after: Int!) { task(where: {display_id: {_gt: $after}}) { display_id command_name } }
"""
ROWS_QUERY = """
query SageToolCardRows($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message metadata status }
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
    """Native-chat, Mythic and Phoenix helpers of the OpenAI Responses check."""
    return _module("sage_tool_card_check_base", _SCRIPTS / "check_openai_responses.py")


def _dep():
    """Route, credential and Phoenix health helpers of the dependency-advisories check."""
    return _module("sage_tool_card_check_dependency", _SCRIPTS / "check_dependency_advisories.py")


def _reasoning():
    return _module("sage_tool_card_check_reasoning", _SCRIPTS / "check_reasoning_tools.py")


# --------------------------------------------------------------------------------------------------
# Phoenix (read-only, in place)
# --------------------------------------------------------------------------------------------------

def _json(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else (value or {})
    except ValueError:
        return {}


def window_spans(db: Path, after: int, until: int) -> list[dict[str, Any]]:
    """LLM and TOOL spans in (after, until]: agent, model, tokens, emitted calls (LLM), name, call id, output (TOOL)."""
    with _base()._phoenix(db) as conn:
        rows = conn.execute(
            "SELECT id, span_kind, name, attributes, COALESCE(llm_token_count_prompt, 0), "
            "COALESCE(llm_token_count_completion, 0) FROM spans WHERE id > ? AND id <= ? "
            "AND span_kind IN ('LLM', 'TOOL') ORDER BY id", (after, until)).fetchall()
    spans = []
    for span_id, kind, name, attributes, prompt, completion in rows:
        attrs = _json(attributes)
        entry = {"id": span_id, "kind": kind, "name": name,
                 "agent": str((attrs.get("metadata") or {}).get("lc_agent_name") or "")}
        if kind == "LLM":
            llm = attrs.get("llm") or {}
            calls = []
            for message in llm.get("output_messages") or []:
                for item in ((message or {}).get("message") or {}).get("tool_calls") or []:
                    call = (item or {}).get("tool_call") or {}
                    calls.append({"id": str(call.get("id") or ""), "name": str((call.get("function") or {}).get("name") or "")})
            entry.update(model=str(llm.get("model_name") or ""), prompt_tokens=int(prompt),
                         completion_tokens=int(completion), calls=calls)
        else:
            data = (_json((attrs.get("output") or {}).get("value")) or {}).get("data") or {}
            entry.update(tool_call_id=str(data.get("tool_call_id") or ""), content=str(data.get("content") or ""))
        spans.append(entry)
    return spans


def gate(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """The first Supervisor-emitted list_callbacks call whose TOOL span ran under the Supervisor, else closed."""
    tools = {s["tool_call_id"]: s for s in spans
             if s["kind"] == "TOOL" and s["name"] == "list_callbacks" and s["agent"] == "Supervisor"}
    for span in spans:
        if span["kind"] == "LLM" and span["agent"] == "Supervisor":
            for call in span["calls"]:
                if call["name"] == "list_callbacks" and call["id"] in tools:
                    tool = tools[call["id"]]
                    return {"open": True, "call_id": call["id"], "llm_span": span["id"], "tool_span": tool["id"],
                            "tool_content": tool["content"]}
    return {"open": False}


def _llm_totals(spans: list[dict[str, Any]]) -> tuple[int, int]:
    llm = [s for s in spans if s["kind"] == "LLM"]
    return len(llm), sum(s["prompt_tokens"] + s["completion_tokens"] for s in llm)


# --------------------------------------------------------------------------------------------------
# Pass rules (pure)
# --------------------------------------------------------------------------------------------------

def _tool_use(row: dict[str, Any]) -> dict[str, Any] | None:
    metadata = row.get("metadata") or {}
    return metadata.get("tool_use") if metadata.get("special_type") == "tool_use" else None


def _callbacks(content: str) -> list[dict[str, Any]]:
    rows = _json(content)
    rows = rows.get("callbacks") if isinstance(rows, dict) else rows
    return [row for row in rows or [] if isinstance(row, dict)]


def evaluate(gated: dict[str, Any], rows: dict[str, Any]) -> dict[str, Any]:
    messages = list(rows.get("messages") or [])
    cards = [(row, _tool_use(row)) for row in messages if _tool_use(row) is not None]
    mine = [(row, card) for row, card in cards if str(card.get("tool_call_id") or "") == gated["call_id"]]
    preview = str(mine[0][1].get("result_preview") or "") if len(mine) == 1 else ""
    callbacks = _callbacks(gated.get("tool_content", ""))
    p1 = bool(len(mine) == 1 and mine[0][1].get("tool_name") == "list_callbacks"
              and "delegation_id" not in mine[0][1] and not (mine[0][0].get("metadata") or {}).get("delegation_id")
              and mine[0][1].get("status") == "completed" and mine[0][0].get("status") == "complete"
              and all(str(cb.get("id", cb.get("display_id"))) in preview and str(cb.get("host")) in preview
                      for cb in callbacks))
    texts = [(row, str(row.get("message") or "").strip()) for row in messages if _tool_use(row) is None]
    assistant = [(row, text) for row, text in texts if text and row.get("author_type") != "operator"]
    final = assistant[-1][1] if assistant else ""
    control_cards = [card.get("tool_name") for _, card in cards
                     if card.get("tool_name") == "respond_to_user" or str(card.get("tool_name")).startswith("transfer_")]
    p2 = bool(final and not control_cards and sum(final in text for _, text in assistant) == 1
              and not any(marker in text or gated["call_id"] in text for _, text in assistant
                          for marker in PAYLOAD_MARKERS))
    p3 = rows.get("status") == "complete" and not any(RECONCILE_FAILURE in text for _, text in texts)
    return {"criteria": {"P1": p1, "P2": p2, "P3": p3},
            "detail": {"matching_card_rows": len(mine), "card_rows": len(cards), "control_cards": control_cards,
                       "callbacks_in_tool_span": len(callbacks), "final_rows": sum(final in t for _, t in assistant),
                       "request_status": rows.get("status")},
            "answer": final}


# --------------------------------------------------------------------------------------------------
# Mythic and readiness
# --------------------------------------------------------------------------------------------------

async def _rows(client: Any, request_id: int) -> dict[str, Any]:
    data = await _base()._query(client, ROWS_QUERY, rid=request_id)
    return {"status": (data.get("chat_request_by_pk") or {}).get("status"),
            "messages": list(data.get("chat_message") or [])}


async def _new_tasks(client: Any, baseline: int) -> list[dict[str, Any]]:
    return list((await _base()._query(client, TASKS_AFTER_QUERY, after=baseline)).get("task") or [])


def _channel_metadata(route: dict[str, str]) -> dict[str, Any]:
    config = {"provider": "openai", "model": route["MODEL"], "API_ENDPOINT": route["API_ENDPOINT"],
              "mode": "conversation", "autonomous_solve": False, "max_steps": MAX_STEPS}
    return {"config": config, "channel_metadata_display": {"display": _base().nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


def _prompt_reaches_supervisor() -> bool:
    sys.path.insert(0, str(REPO_ROOT / "Payload_Type" / "sage"))
    from ai.langgraph.model import _looks_like_scoped_callback_inventory_prompt
    return not _looks_like_scoped_callback_inventory_prompt(PROMPT)


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, dep, reasoning = _base(), _dep(), _reasoning()
    route = dep.read_route(Path(args.route_file))
    deployment = _module("sage_tool_card_check_deployment", _DEPLOYMENT_PATH)
    installed = Path(deployment._installed_service_dir())
    parity = reasoning._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                                  "parity"])
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    tests = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", *OFFLINE_TESTS],
                           capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type" / "sage")
    phoenix = Path(args.phoenix_db)
    report = {
        "route": {"model": route["MODEL"], "endpoint": "from the route file (not recorded)"},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "prompt_reaches_supervisor": _prompt_reaches_supervisor(),
        "active_callbacks": len((await base._query(client, CALLBACKS_QUERY)).get("callback") or []),
        "phoenix_readable": phoenix.is_file(),
        "phoenix_health": dep.phoenix_health(args.phoenix_url),
        "task_baseline": await base._max_task(client),
        "offline_tests": {"returncode": tests.returncode, "summary": tests.stdout.strip().splitlines()[-1:]},
        "credential_source": {
            "installed": dep.installed_credentials(installed, route["API_ENDPOINT"]),
            "container": dep._container_key_probe(route),
            "operator_secrets": await reasoning._operator_secret_names(client),
            "channel_config_sets_key": False,
        },
        "limits": LIMITS,
    }
    blockers = []
    for ok, reason in ((report["sage_container_running"], "Sage container not consuming"),
                       (not report["open_requests"], "open chat requests"),
                       (report["prompt_reaches_supervisor"], "the prompt takes the deterministic inventory route"),
                       (report["active_callbacks"] > 0, "no active callback"),
                       (report["phoenix_readable"] and report["phoenix_health"] == 200, "Phoenix unreadable or down"),
                       (tests.returncode == 0, "this line's offline tests fail")):
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

def _over_limit(run: dict[str, Any], phoenix: Path, started: float) -> str:
    calls, tokens = _llm_totals(window_spans(phoenix, run["phoenix_start"], _base().phoenix_mark(phoenix)))
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
    if run.get("requests"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate_check = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    run["check_attempt"] = {"returncode": gate_check.returncode, "output": gate_check.stdout.strip()[-400:]}
    if gate_check.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate_check.stdout.strip()} {gate_check.stderr.strip()}")
    route = _dep().read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    run.update(authorized_by=args.authorized_by, started_at=time.time(), route={"MODEL": route["MODEL"]},
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix), requests=[])
    started = time.monotonic()
    for attempt in range(LIMITS["requests"]):
        created = await nc.create_locked_channel(client, name=f"Supervisor tool card check {attempt + 1}",
                                                 description="Sage Supervisor tool card live check",
                                                 metadata=_channel_metadata(route))
        channel_id = int(created["chat_channel_id"])
        mark = base.phoenix_mark(phoenix)
        message = await nc.create_message(client, channel_id, PROMPT)
        entry: dict[str, Any] = {"channel_id": channel_id, "request_id": int(message["chat_request_id"]),
                                 "phoenix_after": mark, "sent_at": time.time()}
        try:
            await nc.wait_for_request(client, entry["request_id"], timeout_seconds=REQUEST_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError) as error:
            entry["timeout"] = str(error) or "request timeout"
            await nc.stop_native_chat_request(client, channel_id=channel_id, request_id=entry["request_id"])
        await asyncio.sleep(SETTLE_SECONDS)
        entry.update(finished_at=time.time(), phoenix_until=base.phoenix_mark(phoenix))
        entry["gate"] = gate(window_spans(phoenix, mark, entry["phoenix_until"]))
        run["requests"].append(entry)
        tasks = await _new_tasks(client, run["task_baseline"])
        stop = "a Mythic task was created" if tasks else _over_limit(run, phoenix, started)
        if stop:
            run["stopped"] = {"after_request": attempt + 1, "reason": stop, "tasks": tasks}
            break
        if entry["gate"]["open"]:
            break
    run["finished_at"], run["phoenix_end"] = time.time(), base.phoenix_mark(phoenix)
    return {"requests": run["requests"], "stopped": run.get("stopped")}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    phoenix = Path(args.phoenix_db)
    requests = run.get("requests") or []
    gated = next((entry for entry in requests if (entry.get("gate") or {}).get("open")), None)
    calls, tokens = _llm_totals(window_spans(phoenix, run.get("phoenix_start", 0), run.get("phoenix_end", 0)))
    observations = {"requests_sent": len(requests), "provider_calls": calls, "total_tokens": tokens,
                    "wall_seconds": round(float(run.get("finished_at", 0)) - float(run.get("started_at", 0)), 1),
                    "stopped": run.get("stopped")}
    if gated is None:
        result = {"verdict": "not_run", "reason": "no request opened the gate", "observations": observations}
    else:
        outcome = evaluate(gated["gate"], await _rows(client, int(gated["request_id"])))
        observations.update(detail=outcome["detail"], answer=outcome["answer"][:600],
                            latency_seconds=round(gated["finished_at"] - gated["sent_at"], 1))
        result = {"verdict": "pass" if all(outcome["criteria"].values()) else "fail",
                  "gated_request": gated["request_id"], "criteria": outcome["criteria"], "observations": observations}
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
        command.add_argument("--phoenix-db", required=True,
                             help="the deployed Sage's live phoenix.db, read in place (read-only)")
        if name in {"readiness", "run"}:
            command.add_argument("--route-file", required=True,
                                 help="owner-only KEY=VALUE file with API_ENDPOINT and MODEL (no key)")
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
