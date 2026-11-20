#!/usr/bin/env python3
"""Native container-restart check for existing-chat restoration.

Runs the restoration line's own live check as ordered steps that share one run file, so each
step survives the Sage restarts between them. Every mutating step (channel creation, prompts,
card decisions, deployment, stop/kill) is the operator's authorized live activity; `readiness`
and `status` only read Mythic and the local tree.

Order (see the plan named in the run file):

  readiness --callback N          read-only: Mythic login, container mode/parity, live callback
                                  with `pwd`, frozen source identity, task baseline
  seed --as legacy                before deploying the feature: one conversation chat
  deploy                          deploy the feature branch (database retained)
  seed --as feature [--mode-override supervised]
  restart                         recreate the container (database retained)
  recall                          ask both chats for the label; no task may be created
  card --as accept|reject|down|crash
  restart                         (accept/reject cards only)
  decide --as accept|reject       decide the restored card through Mythic
  down --as down                  stop Sage, accept the card while it is down, start, send a prompt
  crash --as crash                accept, kill Sage once Mythic shows the task, start, ask a question
  verify                          evaluate every recorded case against its rule

Pass rules are written in `verify` before any run and read Mythic's own rows: request/message
status and text, card status, and the callback's task rows. Sage's SQLite is never the proof.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
_NATIVE_CHAT = REPO_ROOT / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
_spec = importlib.util.spec_from_file_location("sage_native_chat_for_restoration", _NATIVE_CHAT)
nc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nc
_spec.loader.exec_module(nc)  # type: ignore[union-attr]

LABEL = "amber-seven"
LABEL_PROMPT = f"Remember the label {LABEL}. Do not run tools."
RECALL_PROMPT = "What label did I ask you to remember? Do not run tools."
STATUS_PROMPT = "Is anything still pending from before? Answer from what you know. Do not run tools."
REFUSAL = "Sage restarted or this channel's session was replaced"
RECOVERED = "decision was recovered"
UNRESOLVED = "will not resend"
CHANGED_FILES = (
    "Payload_Type/sage/sage_chat/service.py",
    "Payload_Type/sage/sage_chat/session.py",
    "Payload_Type/sage/sage_chat/restoration.py",
    "Payload_Type/sage/sage_chat/hitl.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/mythic_tools.py",
    "Payload_Type/sage/ai/langgraph/request_contract.py",
)
BUDGET = {"minutes": 30, "approved_pwd_tasks": 3, "service_restarts": 8, "model_calls": 20,
          "total_tokens": 400_000, "output_tokens_per_call": 4096, "retries": 0}

CALLBACK_QUERY = """
query RestorationCallback($cb: Int!) {
  callback(where: {display_id: {_eq: $cb}, active: {_eq: true}}) {
    display_id last_checkin payload { payloadtype { name } }
  }
}
"""
COMMAND_QUERY = """
query RestorationCommand($cb: Int!) {
  loadedcommands(where: {callback: {display_id: {_eq: $cb}}, command: {cmd: {_eq: "pwd"}}}) { id }
}
"""
TASKS_QUERY = """
query RestorationTasks($after: Int!) {
  task(where: {display_id: {_gt: $after}}, order_by: {display_id: asc}) {
    display_id command_name status completed callback { display_id }
  }
}
"""
MAX_TASK_QUERY = """
query RestorationMaxTask { task(order_by: {display_id: desc}, limit: 1) { display_id } }
"""
CHANNEL_MESSAGES_QUERY = """
query RestorationMessages($ch: Int!, $after: Int!) {
  chat_message(where: {channel_id: {_eq: $ch}, id: {_gt: $after}}, order_by: {id: asc}) {
    id chat_request_id author_type message metadata
  }
}
"""


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {"cases": {}, "restarts": 0}


def _save(path: Path, run: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    return {
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "Payload_Type/sage")),
        "files": {path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
                  for path in CHANGED_FILES},
    }


async def _query(client: Any, query: str, **variables: Any) -> dict[str, Any]:
    result = await nc.mythic.execute_custom_query(client, query, variables=variables)
    return result if isinstance(result, dict) else {}


async def _max_task(client: Any) -> int:
    rows = (await _query(client, MAX_TASK_QUERY)).get("task") or []
    return int(rows[0]["display_id"]) if rows else 0


async def _tasks_after(client: Any, after: int) -> list[dict[str, Any]]:
    return list((await _query(client, TASKS_QUERY, after=after)).get("task") or [])


async def _messages(client: Any, channel_id: int, after: int = 0) -> list[dict[str, Any]]:
    return list((await _query(client, CHANNEL_MESSAGES_QUERY, ch=channel_id, after=after))
                .get("chat_message") or [])


def _deployment(*args: str) -> dict[str, Any]:
    command = [str(REPO_ROOT / ".venv/bin/python"),
               str(REPO_ROOT / "skills/sage-goad-reset/scripts/sage_deployment.py"), *args]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT)
    return {"command": " ".join(args), "returncode": completed.returncode,
            "tail": completed.stdout.strip().splitlines()[-3:]}


def _mythic_dir() -> Path:
    cli = os.environ.get("MYTHIC_CLI", "")
    if not cli:
        raise SystemExit("MYTHIC_CLI is not set; load the repo-root .env first")
    return Path(cli).resolve().parent


def _count_restart(run: dict[str, Any]) -> None:
    run["restarts"] = int(run.get("restarts", 0)) + 1
    if run["restarts"] > BUDGET["service_restarts"]:
        raise SystemExit(f"service restart budget ({BUDGET['service_restarts']}) exhausted")


async def _send(client: Any, channel_id: int, prompt: str, *, wait_card: bool = False) -> dict[str, Any]:
    message = await nc.create_message(client, channel_id, prompt)
    request_id = int(message["chat_request_id"])
    await nc.wait_for_request(client, request_id, timeout_seconds=600,
                              stop_on_input_requested=wait_card)
    snapshot = await nc.fetch_request_snapshot(client, request_id)
    return {"request_id": request_id, "status": snapshot["request"].get("status"),
            "message_id": int(message["chat_message_id"])}


async def _create_channel(client: Any, *, mode: str, name: str) -> int:
    metadata = nc.canary_ai_metadata(max_steps=50 if mode == "conversation" else 200,
                                     session_mode=mode, extra=None)
    created = await nc.create_locked_channel(client, name=name,
                                             description=f"Sage restoration check ({mode})",
                                             metadata=metadata)
    return int(created["chat_channel_id"])


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    callback = int(args.callback)
    rows = (await _query(client, CALLBACK_QUERY, cb=callback)).get("callback") or []
    commands = (await _query(client, COMMAND_QUERY, cb=callback)).get("loadedcommands") or []
    readiness = await _query(client, nc.READINESS_QUERY)
    containers = readiness.get("consuming_container") or []
    report = {
        "callback": rows[0] if len(rows) == 1 else None,
        "pwd_loaded": bool(commands),
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "deployment": _deployment("check"),
        "identity": _identity(),
        "task_baseline": await _max_task(client),
        "budget": BUDGET,
    }
    report["ready"] = bool(report["callback"] and report["pwd_loaded"]
                           and report["sage_container_running"]
                           and report["deployment"]["returncode"] == 0)
    run.setdefault("readiness", report)
    run["callback"] = callback
    run.setdefault("task_baseline", report["task_baseline"])
    return report


async def cmd_seed(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    channel_id = await _create_channel(client, mode="conversation", name=f"Restoration {args.as_}")
    seeded = await _send(client, channel_id, LABEL_PROMPT)
    case = {"channel_id": channel_id, "seed": seeded, "identity": _identity()}
    if args.mode_override:
        case["mode"] = await _send(client, channel_id, f"/mode {args.mode_override}")
        case["mode_override"] = args.mode_override
    run["cases"][args.as_] = case
    return case


async def cmd_deploy(_client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    _count_restart(run)
    result = {"deploy": _deployment("deploy", "--mode", "container"), "identity": _identity()}
    run.setdefault("deployments", []).append(result)
    return result


async def cmd_recall(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    before = await _max_task(client)
    results = {}
    for name in ("legacy", "feature"):
        case = run["cases"].get(name)
        if case:
            case["recall"] = await _send(client, case["channel_id"], RECALL_PROMPT)
            results[name] = case["recall"]
    run["recall_tasks"] = await _tasks_after(client, before)
    return results


async def cmd_card(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    channel_id = await _create_channel(client, mode="supervised", name=f"Restoration card {args.as_}")
    prompt = f"Run pwd once on callback {run['callback']}; do nothing else."
    sent = await _send(client, channel_id, prompt, wait_card=True)
    run["cases"][args.as_] = {"channel_id": channel_id, "card_request": sent,
                              "tasks_before": await _max_task(client)}
    return run["cases"][args.as_]


async def cmd_decide(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    case = run["cases"][args.as_]
    request_id = case["card_request"]["request_id"]
    if args.as_ == "accept":
        decision = await nc.approve_pending_input_card(client, request_id)
    else:
        decision = await nc.reject_pending_input_card(client, request_id)
    await nc.wait_for_request(client, request_id, timeout_seconds=600)
    case["decision"] = decision
    # A second delivery attempt of the same decision must not act again.
    try:
        await nc.approve_pending_input_card(client, request_id)
        case["duplicate"] = "accepted-by-mythic"
    except Exception as exc:  # Mythic refuses a card that is no longer pending
        case["duplicate"] = f"refused: {type(exc).__name__}"
    case["tasks_after"] = await _max_task(client)
    return case


async def cmd_down(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    case = run["cases"][args.as_]
    _count_restart(run)
    subprocess.run(["docker", "compose", "stop", "sage"], cwd=_mythic_dir(), check=True)
    try:
        case["decision_while_down"] = await nc.approve_pending_input_card(
            client, case["card_request"]["request_id"])
    except Exception as exc:  # Mythic stores the decision, then reports the stopped container
        case["decision_while_down"] = f"{type(exc).__name__}: {exc}"
    case["start"] = _deployment("deploy", "--mode", "container")
    case["next_request"] = await _send(client, case["channel_id"], STATUS_PROMPT)
    case["tasks_after"] = await _max_task(client)
    return case


async def cmd_crash(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    case = run["cases"][args.as_]
    _count_restart(run)
    before = case["tasks_before"]
    await nc.approve_pending_input_card(client, case["card_request"]["request_id"])
    deadline = time.monotonic() + 120
    issued: list[dict[str, Any]] = []
    while time.monotonic() < deadline and not issued:
        issued = [t for t in await _tasks_after(client, before) if t.get("command_name") == "pwd"]
        if not issued:
            await asyncio.sleep(0.5)
    subprocess.run(["docker", "kill", "sage"], check=False, capture_output=True)
    case["killed_after_task"] = issued[:1]
    case["start"] = _deployment("deploy", "--mode", "container")
    case["next_request"] = await _send(client, case["channel_id"], STATUS_PROMPT)
    case["tasks_after"] = await _max_task(client)
    return case


def _text(messages: list[dict[str, Any]], request_id: int) -> str:
    return "\n".join(str(m.get("message") or "") for m in messages
                     if m.get("chat_request_id") == request_id)


async def cmd_verify(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    """Pass rules, fixed before the run; each case is read once."""
    callback = run.get("callback")
    pwd = [t for t in await _tasks_after(client, int(run.get("task_baseline", 0)))
           if t.get("command_name") == "pwd" and (t.get("callback") or {}).get("display_id") == callback]
    verdicts: dict[str, Any] = {}
    for name in ("legacy", "feature"):
        case = run["cases"].get(name)
        if not case or "recall" not in case:
            verdicts[name] = "not run"
            continue
        text = _text(await _messages(client, case["channel_id"]), case["recall"]["request_id"])
        verdicts[name] = {
            "pass": LABEL in text and REFUSAL not in text and case["recall"]["status"] == "complete",
            "label_retained": LABEL in text,
        }
    verdicts["recall_created_no_task"] = not run.get("recall_tasks")
    for name, expected in (("accept", 1), ("reject", 0), ("down", 1), ("crash", 1)):
        case = run["cases"].get(name)
        if not case:
            verdicts[name] = "not run"
            continue
        upper = case.get("tasks_after")
        own = [t for t in pwd if int(t["display_id"]) > int(case["tasks_before"])
               and (upper is None or int(t["display_id"]) <= int(upper))]
        detail: dict[str, Any] = {"pwd_tasks": len(own), "expected": expected}
        if name in {"down", "crash"}:
            text = _text(await _messages(client, case["channel_id"]), case["next_request"]["request_id"])
            marker = RECOVERED if name == "down" else UNRESOLVED
            detail["notice"] = marker in text
            detail["pass"] = len(own) == expected and marker in text and REFUSAL not in text
        else:
            detail["duplicate"] = case.get("duplicate")
            detail["pass"] = len(own) == expected and str(case.get("duplicate", "")).startswith("refused")
        verdicts[name] = detail
    if run["cases"].get("feature", {}).get("mode_override"):
        verdicts["mode_override_recorded"] = "check the restored channel's header mode in Mythic"
    verdicts["approved_pwd_tasks"] = len(pwd)
    verdicts["within_task_budget"] = len(pwd) <= BUDGET["approved_pwd_tasks"]
    verdicts["restarts"] = run.get("restarts", 0)
    run["verify"] = verdicts
    return verdicts


COMMANDS = {
    "readiness": cmd_readiness, "seed": cmd_seed, "deploy": cmd_deploy, "restart": cmd_deploy,
    "recall": cmd_recall, "card": cmd_card, "decide": cmd_decide, "down": cmd_down,
    "crash": cmd_crash, "verify": cmd_verify,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--run-file", required=True, type=Path,
                        help="JSON run record shared by every step (keep it under .sage_history)")
    parser.add_argument("--callback", type=int, help="readiness: the verified live callback display id")
    parser.add_argument("--as", dest="as_", default="",
                        help="case name: legacy|feature (seed), accept|reject|down|crash (card/decide/down/crash)")
    parser.add_argument("--mode-override", choices=("conversation", "supervised"), default="")
    parser.add_argument("--readiness", action="store_true", help="alias for the readiness command")
    parser.add_argument("--server", default=nc.DEFAULT_SERVER)
    parser.add_argument("--user", default=nc.DEFAULT_USER)
    parser.add_argument("--password", default=None)
    parser.add_argument("--env-path", default=None)
    return parser


async def _main(args: argparse.Namespace) -> int:
    run = _load(args.run_file)
    client = await nc.login(server=args.server, user=args.user, password=args.password,
                            env_path=args.env_path)
    command = "readiness" if args.readiness else args.command
    result = await COMMANDS[command](client, run, args)
    run.setdefault("steps", []).append({"command": command, "as": args.as_, "at": time.time()})
    _save(args.run_file, run)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
