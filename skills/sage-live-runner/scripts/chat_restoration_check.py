#!/usr/bin/env python3
"""Native container-restart check for continuing restored chats.

Runs the restoration line's own live check as ordered steps that share one run file, so each
step survives the Sage restarts between them. Every mutating step (channel creation, prompts,
card decisions, deployment, stop/kill) is the operator's authorized live activity; `readiness`
and `verify` only read Mythic and the local tree.

Order (see the plan named in the run file):

  readiness --callback N          read-only: Mythic login, container mode/parity, live callback
                                  with `pwd`, frozen source identity, task baseline, no open requests
  start-old / seed --as legacy    pre-feature Sage: one conversation chat with the label
  deploy                          deploy the feature branch (database retained)
  seed --as feature --mode-override supervised
  restart                         recreate the container (database retained)
  recall                          ask both chats for the label; no task may be created
  propose                         the restored feature chat is asked to run pwd once; its card is
                                  case `queued_accept` (the model seam after a restart)
  card --as queued_reject         a second supervised card
  queued                          restart with both cards pending; once Mythic shows Sage running, accept
                                  the first and wait for its request to end, then reject the second
  card --as down_accept / card --as down_reject
  down                            stop Sage; decide both cards while Mythic records it stopped; start and
                                  send nothing; select Continue on each Continue/Stop card
  card --as crash / crash         accept, kill Sage once Mythic shows the task, start; the request must end
                                  with no operator action; `/mode` then reports the task
  verify                          evaluate every recorded case against its rule

Pass rules are written in `verify` before any run and read Mythic's own rows: request status,
message text and card metadata, and the callback's task rows. Sage's SQLite is never the proof.
Each case's tasks are the ones Mythic created between its own answer and its request's end, and
its request's own rows must name them; cases are answered one at a time, and a `pwd` task that
belongs to no case, or to more than one, fails the run.
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
UNRESOLVED = "will not resend"
LIFECYCLE_ERROR = "Request lifecycle reconciliation failed"
STALE = ("no longer active", "already handled")
CONTINUE_TITLE = "Continue saved decision?"
NEXT_PROMPT = "/mode"  # read-only: proves the channel accepts a prompt without a model call
CASE_IDS = ("legacy", "feature", "queued_accept", "queued_reject", "down_accept", "down_reject", "crash")
ROUTE = "bedrock-claude-4-6-sonnet"  # the plan's frozen route; checked against Phoenix by the run session
CHANGED_FILES = (
    "Payload_Type/sage/sage_chat/service.py",
    "Payload_Type/sage/sage_chat/session.py",
    "Payload_Type/sage/sage_chat/restoration.py",
    "Payload_Type/sage/sage_chat/hitl.py",
    "Payload_Type/sage/sage_chat/streaming.py",
    "Payload_Type/sage/sage_chat/watcher_control.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/request_events.py",
    "Payload_Type/sage/ai/langgraph/mythic_tools.py",
    "Payload_Type/sage/ai/langgraph/request_contract.py",
)
BUDGET = {"minutes": 35, "approved_pwd_tasks": 3, "service_restarts": 8, "model_calls": 30,
          "total_tokens": 560_000, "output_tokens_per_call": 4096, "retries": 0}
TERMINAL = {"complete", "error", "cancelled"}

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
REQUESTS_QUERY = """
query RestorationRequests($ids: [Int!]!) { chat_request(where: {id: {_in: $ids}}) { id status } }
"""
OPEN_REQUESTS_QUERY = """
query RestorationOpen { chat_request(where: {status: {_in: ["pending", "streaming"]}}) { id channel_id status } }
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


async def _wait_container(client: Any, running: bool, timeout: float = 240) -> bool:
    """Wait until Mythic's own record of the Sage chat container shows the wanted state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = (await _query(client, nc.READINESS_QUERY)).get("consuming_container") or []
        if rows and bool(rows[0].get("container_running")) == running:
            return True
        await asyncio.sleep(3)
    return False


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
        "open_requests": (await _query(client, OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "budget": BUDGET,
        "route": ROUTE,
        "case_ids": list(CASE_IDS),
    }
    report["ready"] = bool(report["callback"] and report["pwd_loaded"]
                           and report["sage_container_running"]
                           and report["deployment"]["returncode"] == 0
                           and not report["open_requests"])
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


async def cmd_start_old(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    """Start the installed (pre-feature) Sage so a legacy chat can be seeded before deploy."""
    _count_restart(run)
    cli = os.environ.get("MYTHIC_CLI", "")
    completed = subprocess.run([cli, "start", "sage"], capture_output=True, text=True,
                               cwd=_mythic_dir())
    result = {"returncode": completed.returncode, "serving": await _wait_container(client, True)}
    run["started_at"] = run.get("started_at") or time.time()
    return result


async def cmd_deploy(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    _count_restart(run)
    result = {"deploy": _deployment("deploy", "--mode", "container"), "identity": _identity()}
    result["serving"] = await _wait_container(client, True)
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


async def _card_case(client: Any, run: dict[str, Any], name: str, channel_id: int) -> dict[str, Any]:
    prompt = f"Run pwd once on callback {run['callback']}; do nothing else."
    tasks_before = await _max_task(client)
    sent = await _send(client, channel_id, prompt, wait_card=True)
    run["cases"][name] = {"channel_id": channel_id, "card_request": sent, "tasks_before": tasks_before}
    return run["cases"][name]


async def cmd_propose(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    """The restored feature chat (supervised via `/mode`) proposes the action after the restart."""
    feature = run["cases"]["feature"]
    return await _card_case(client, run, "queued_accept", int(feature["channel_id"]))


async def cmd_card(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    channel_id = await _create_channel(client, mode="supervised", name=f"Restoration card {args.as_}")
    return await _card_case(client, run, args.as_, channel_id)


def _input_cards(messages: list[dict[str, Any]], request_id: int) -> list[dict[str, Any]]:
    """Input cards of one request: approval cards and Continue/Stop controls, with their status."""
    cards = []
    for message in messages:
        metadata = message.get("metadata") or {}
        card = metadata.get("input_requested") if isinstance(metadata, dict) else None
        if message.get("chat_request_id") != request_id or not isinstance(card, dict):
            continue
        data = card.get("data") or {}
        context = data.get("sage_approval_context") or {}
        control = data.get("sage_continuation") or {}
        cards.append({"message_id": message["id"], "status": card.get("status"),
                      "kind": "control" if control else "approval",
                      "approval_id": context.get("approval_id") or control.get("source_approval_id"),
                      "action_digest": context.get("action_digest"),
                      "actions": list(data.get("actions") or []),
                      "response": card.get("response") or {}})
    return cards


async def _wait_status(client: Any, request_id: int, done, timeout: float = 240) -> dict[str, Any]:
    """Poll Mythic's own request snapshot until ``done(snapshot)`` holds or the time runs out."""
    deadline = time.monotonic() + timeout
    snapshot = await nc.fetch_request_snapshot(client, request_id)
    while not done(snapshot) and time.monotonic() < deadline:
        await asyncio.sleep(3)
        snapshot = await nc.fetch_request_snapshot(client, request_id)
    return snapshot


async def _select_continue(client: Any, request_id: int) -> dict[str, Any]:
    """Select Continue on the request's one pending Continue/Stop card (the UI's own mutation)."""
    snapshot = await nc.fetch_request_snapshot(client, request_id)
    pending = [m for m in nc._pending_input_requested_messages(snapshot["messages"])
               if "sage_continuation" in (((m.get("metadata") or {}).get("input_requested") or {}).get("data") or {})]
    if len(pending) != 1:
        raise RuntimeError(f"expected one pending Continue/Stop card in request {request_id}, saw {len(pending)}")
    result = await nc.mythic.execute_custom_query(client, nc.CHAT_INPUT_RESPONSE_MUTATION, variables={
        "messageId": int(pending[0]["id"]), "action": "select", "response": None, "choiceId": "continue"})
    return {"message_id": int(pending[0]["id"]), "result": result.get("chatInputResponse")}


async def _duplicate(client: Any, request_id: int) -> str:
    """A second answer to the request's cards must be refused or change nothing."""
    try:
        await nc.approve_pending_input_card(client, request_id)
        return "accepted-by-mythic"
    except Exception as exc:  # Mythic refuses a card that is no longer pending or a closed request
        return f"refused: {type(exc).__name__}"


async def _finish(client: Any, case: dict[str, Any]) -> None:
    request_id = case["card_request"]["request_id"]
    ended = await _wait_status(client, request_id, lambda snap: snap["request"].get("status") in TERMINAL, 600)
    case["tasks_at_terminal"] = await _max_task(client)
    case["request_status"] = ended["request"].get("status")
    case["duplicate"] = await _duplicate(client, request_id)
    case["tasks_after"] = await _max_task(client)
    case["next_request"] = await _send(client, case["channel_id"], NEXT_PROMPT)


async def cmd_queued(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    """Restart with both cards pending; decide each once Mythic shows Sage running, one at a time."""
    _count_restart(run)
    cases = {name: run["cases"][name] for name in ("queued_accept", "queued_reject")}
    subprocess.run(["docker", "compose", "stop", "sage"], cwd=_mythic_dir(), check=True)
    await _wait_container(client, False)
    starting = subprocess.Popen(
        [str(REPO_ROOT / ".venv/bin/python"), str(REPO_ROOT / "skills/sage-goad-reset/scripts/sage_deployment.py"),
         "deploy", "--mode", "container"], cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    running = await _wait_container(client, True, timeout=300)
    for name, case in cases.items():
        request_id = case["card_request"]["request_id"]
        decide = nc.approve_pending_input_card if name == "queued_accept" else nc.reject_pending_input_card
        case["tasks_at_answer"] = await _max_task(client)
        try:
            case["decision"] = await decide(client, request_id)
        except Exception as exc:
            case["decision"] = f"{type(exc).__name__}: {exc}"
        case["decided_at"] = time.time()
        case["mythic_saw_running"] = running
        # The next case is answered only after this request ends, so each window is its own.
        await _finish(client, case)
    case_returncode = starting.wait(timeout=600)
    for case in cases.values():
        case["start_returncode"] = case_returncode
    return cases


async def cmd_down(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    """Decide both cards while Mythic records Sage stopped; start, send nothing, then Continue."""
    _count_restart(run)
    cases = {name: run["cases"][name] for name in ("down_accept", "down_reject")}
    subprocess.run(["docker", "compose", "stop", "sage"], cwd=_mythic_dir(), check=True)
    # Mythic must record the container as stopped, or it queues the decision for later delivery
    # instead of storing it undelivered, which is the case under test.
    if not await _wait_container(client, False):
        raise SystemExit("Mythic never recorded the Sage container as stopped; decisions not sent")
    for name, case in cases.items():
        request_id = case["card_request"]["request_id"]
        decide = nc.approve_pending_input_card if name == "down_accept" else nc.reject_pending_input_card
        try:
            case["decision_while_down"] = await decide(client, request_id)
        except Exception as exc:  # Mythic stores the decision, then reports the stopped container
            case["decision_while_down"] = f"{type(exc).__name__}: {exc}"
    start = _deployment("deploy", "--mode", "container")
    serving = await _wait_container(client, True)
    for case in cases.values():
        case["start"], case["serving"] = start, serving
        request_id = case["card_request"]["request_id"]
        # No prompt: Sage's on-start must add one Continue/Stop card to the same request.
        offered = await _wait_status(client, request_id, lambda snap: any(
            c["kind"] == "control" and c["status"] == "pending"
            for c in _input_cards(snap["messages"], request_id)))
        case["cards_at_offer"] = _input_cards(offered["messages"], request_id)
        case["tasks_before_answer"] = case["tasks_at_answer"] = await _max_task(client)
        if any(c["kind"] == "control" and c["status"] == "pending" for c in case["cards_at_offer"]):
            case["continue"] = await _select_continue(client, request_id)
        await _finish(client, case)
    return cases


async def cmd_crash(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    case = run["cases"][args.as_]
    _count_restart(run)
    before = case["tasks_before"]
    case["tasks_at_answer"] = await _max_task(client)
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
    case["serving"] = await _wait_container(client, True)
    # No operator action: Sage's on-start reconciliation must end the orphaned request.
    ended = await _wait_status(client, case["card_request"]["request_id"],
                               lambda snap: snap["request"].get("status") in TERMINAL)
    case["request_status_after_start"] = ended["request"].get("status")
    case["tasks_at_terminal"] = await _max_task(client)
    case["next_request"] = await _send(client, case["channel_id"], NEXT_PROMPT)
    case["tasks_after"] = await _max_task(client)
    return case


def _text(messages: list[dict[str, Any]], request_id: Any) -> str:
    return "\n".join(str(m.get("message") or "") for m in messages
                     if m.get("chat_request_id") == request_id)


def _named_tasks(messages: list[dict[str, Any]], request_id: int, callback: Any) -> set[int]:
    """Mythic task ids the request's own task cards name (`mythic-task:<callback>:<task>`)."""
    prefix = f"mythic-task:{callback}:"
    named = set()
    for m in messages:
        tool = (m.get("metadata") or {}).get("tool_use") or {}
        call_id = str(tool.get("tool_call_id") or "")
        if m.get("chat_request_id") == request_id and call_id.startswith(prefix):
            try:
                named.add(int(call_id[len(prefix):]))
            except ValueError:
                continue
    return named


def _decision_refused(decision: Any) -> bool:
    """Mythic did not accept the queued decision (an exception, or a response other than success)."""
    return not isinstance(decision, dict) or (decision.get("response") or {}).get("status") != "success"


def _mode_source(run: dict[str, Any], case: dict[str, Any]) -> str:
    feature = run["cases"].get("feature") or {}
    if case.get("channel_id") == feature.get("channel_id"):
        override = feature.get("mode_override")
        return f"/mode {override}" if override else "configured conversation"
    return "configured supervised"  # `card` creates a supervised-configured channel


ATTRIBUTED = ("queued_accept", "queued_reject", "down_accept", "down_reject", "crash")
MODE_SOURCES = {"queued_accept": "/mode supervised", "down_accept": "configured supervised"}


def _attribution(pwd: list[dict[str, Any]], cases: dict[str, Any]) -> dict[int, list[str]]:
    """Each pwd task's owners: the cases whose (answer, terminal] window contains it."""
    windows = {}
    for name in ATTRIBUTED:
        case = cases.get(name) or {}
        lower, upper = case.get("tasks_at_answer"), case.get("tasks_at_terminal")
        if case.get("card_request") and lower is not None and upper is not None:
            windows[name] = (int(lower), int(upper))
    return {int(t["display_id"]): [name for name, (lower, upper) in windows.items()
                                   if lower < int(t["display_id"]) <= upper] for t in pwd}


def _tool_statuses(messages: list[dict[str, Any]], request_id: int) -> list[str]:
    return [str(((m.get("metadata") or {}).get("tool_use") or {}).get("status") or "")
            for m in messages if m.get("chat_request_id") == request_id
            and (m.get("metadata") or {}).get("special_type") == "tool_use"]


async def cmd_verify(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    """Pass rules, fixed before the run; each case is read once."""
    callback = run.get("callback")
    pwd = [t for t in await _tasks_after(client, int(run.get("task_baseline", 0)))
           if t.get("command_name") == "pwd" and (t.get("callback") or {}).get("display_id") == callback]
    request_ids = [int(c["card_request"]["request_id"]) for c in run["cases"].values() if c.get("card_request")]
    request_ids += [int(c["next_request"]["request_id"]) for c in run["cases"].values() if c.get("next_request")]
    statuses = {int(r["id"]): r.get("status") for r in
                ((await _query(client, REQUESTS_QUERY, ids=request_ids)).get("chat_request") or [])}
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
    owners = _attribution(pwd, run["cases"])
    for name, expected in (("queued_accept", 1), ("queued_reject", 0), ("down_accept", 1),
                           ("down_reject", 0), ("crash", 1)):
        case = run["cases"].get(name)
        if not case or not case.get("card_request"):
            verdicts[name] = "not run"
            continue
        own = [t for t in pwd if owners[int(t["display_id"])] == [name]]
        messages = await _messages(client, case["channel_id"])
        detail: dict[str, Any] = {"pwd_tasks": len(own), "task_ids": [int(t["display_id"]) for t in own],
                                  "expected": expected, "mode_source": _mode_source(run, case)}
        if name == "crash":
            detail.update(_verify_crash(case, own, messages, statuses))
        else:
            detail.update(_verify_resolved(name, case, own, expected, messages, statuses, callback))
            if name in MODE_SOURCES:
                detail["mode_source_ok"] = detail["mode_source"] == MODE_SOURCES[name]
                if detail["pass"] is True and not detail["mode_source_ok"]:
                    detail["pass"] = False
        verdicts[name] = detail
    verdicts["unattributed_pwd_tasks"] = sorted(task for task, names in owners.items() if len(names) != 1)
    verdicts["approved_pwd_tasks"] = len(pwd)
    verdicts["within_task_budget"] = len(pwd) <= BUDGET["approved_pwd_tasks"]
    verdicts["restarts"] = run.get("restarts", 0)
    verdicts["within_restart_budget"] = int(run.get("restarts", 0)) <= BUDGET["service_restarts"]
    # Any pwd task not assigned to exactly one case fails the run, whatever the cases say.
    verdicts["pass"] = (
        not verdicts["unattributed_pwd_tasks"]
        and all(isinstance(verdicts[name], dict) and verdicts[name].get("pass") is True
                for name in ("legacy", "feature", *ATTRIBUTED))
        and verdicts["recall_created_no_task"] and verdicts["within_task_budget"]
        and verdicts["within_restart_budget"]
    )
    run["verify"] = verdicts
    return verdicts


def _verify_resolved(name: str, case: dict[str, Any], own: list[dict[str, Any]], expected: int,
                     messages: list[dict[str, Any]], statuses: dict[int, Any], callback: Any) -> dict[str, Any]:
    """One task (accept) or none (reject), the request complete with no lifecycle error, no Continue/Stop
    card left pending, a stale notice never shown for the winning decision, and the next prompt accepted."""
    request_id = int(case["card_request"]["request_id"])
    cards = _input_cards(messages, request_id)
    approvals = [c for c in cards if c["kind"] == "approval"]
    controls = [c for c in cards if c["kind"] == "control"]
    text = _text(messages, request_id)
    next_request = case.get("next_request") or {}
    checks: dict[str, Any] = {
        "request_status": statuses.get(request_id),
        "lifecycle_error": LIFECYCLE_ERROR in text,
        "stale_notice": any(word in text for word in STALE),
        "pending_cards_left": [c["message_id"] for c in cards if c["status"] == "pending"],
        "one_approval_card": len(approvals) == 1,
        "controls": [(c["message_id"], c["status"]) for c in controls],
        "next_request_status": statuses.get(int(next_request.get("request_id") or 0)),
        "duplicate": case.get("duplicate"),
        "tasks_named_by_request": {int(t["display_id"]) for t in own} <= _named_tasks(messages, request_id, callback),
    }
    common = (len(own) == expected and checks["tasks_named_by_request"]
              and checks["request_status"] == "complete"
              and not checks["lifecycle_error"] and not checks["pending_cards_left"]
              and checks["one_approval_card"] and checks["next_request_status"] == "complete"
              and not str(case.get("duplicate", "")).startswith("accepted"))
    if name.endswith("reject"):
        # Code-owned denial evidence: the guarded call's card ends without completing.
        checks["tool_denied"] = bool(_tool_statuses(messages, request_id)) and "completed" not in _tool_statuses(
            messages, request_id)
        common = common and checks["tool_denied"]
    if name == "queued_accept":
        actions = approvals[0]["actions"] if approvals else []
        args = (actions[0].get("arguments") or {}) if len(actions) == 1 else {}
        checks["seam_exact_proposal"] = (len(actions) == 1 and args.get("command") == "pwd"
                                         and args.get("callback_display_id") == callback)
        common = common and checks["seam_exact_proposal"]
    if name.startswith("queued"):
        # Either delivery order counts; the order is Mythic's and is recorded as an observation.
        checks["delivery_order"] = "startup_offer_first" if controls else "original_first"
        checks["control_unanswered"] = all(c["status"] == "cancelled" for c in controls)
        if name == "queued_accept":
            checks["task_output"] = len(own) == 1 and bool(own[0].get("completed"))
            common = common and checks["task_output"]
        checks["decision_refused"] = _decision_refused(case.get("decision"))
        not_run = checks["decision_refused"] or case.get("mythic_saw_running") is False
        checks["pass"] = "not run" if not_run else (
            common and checks["control_unanswered"] and not checks["stale_notice"])
    else:
        before = case.get("tasks_before_answer")
        early = [t for t in own if before is not None and int(t["display_id"]) <= int(before)]
        checks["tasks_before_continue"] = len(early)
        checks["continued_once"] = len(controls) == 1 and controls[0]["status"] == "selected" and (
            (controls[0]["response"].get("choice") or {}).get("id") == "continue")
        offered = bool(controls)
        checks["pass"] = (common and checks["continued_once"] and not early
                          and not checks["stale_notice"]) if offered else False
    return checks


def _verify_crash(case: dict[str, Any], own: list[dict[str, Any]], messages: list[dict[str, Any]],
                  statuses: dict[int, Any]) -> dict[str, Any]:
    """The orphaned request ends with no operator action; the next prompt reports the exact task."""
    next_request = case.get("next_request") or {}
    text = _text(messages, next_request.get("request_id"))
    killed = case.get("killed_after_task") or [{}]
    task_id = killed[0].get("display_id")
    states_task = (task_id is not None and f"Mythic task {task_id}" in text) or "unknown" in text
    checks = {
        "request_status_after_start": case.get("request_status_after_start"),
        "notice": UNRESOLVED in text,
        "states_task": states_task,
        "next_request_status": statuses.get(int(next_request.get("request_id") or 0),
                                            next_request.get("status")),
    }
    checks["pass"] = (checks["request_status_after_start"] in TERMINAL and len(own) == 1
                      and checks["notice"] and states_task
                      and checks["next_request_status"] == "complete" and REFUSAL not in text)
    return checks


COMMANDS = {
    "readiness": cmd_readiness, "start-old": cmd_start_old, "seed": cmd_seed, "deploy": cmd_deploy, "restart": cmd_deploy,
    "recall": cmd_recall, "propose": cmd_propose, "card": cmd_card, "queued": cmd_queued, "down": cmd_down,
    "crash": cmd_crash, "verify": cmd_verify,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--run-file", required=True, type=Path,
                        help="JSON run record shared by every step (keep it under .sage_history)")
    parser.add_argument("--callback", type=int, help="readiness: the verified live callback display id")
    parser.add_argument("--as", dest="as_", default="",
                        help="case name: legacy|feature (seed), queued_reject|down_accept|down_reject|crash (card/crash)")
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
