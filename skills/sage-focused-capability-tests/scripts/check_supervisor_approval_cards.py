#!/usr/bin/env python3
"""Live check: Accept and Reject on a pending Supervisor approval card, from the stock Mythic UI and the native helpers.

The Sage container runs this branch plus one declared test patch (`apply_test_guard`): `read_engagement_state` joins
the shared `GUARDED_TOOLS`, and each entry to that reader appends one line to `/Mythic/check_reader_entries.log`, a
count kept outside Sage's chat state. The model is `test-model` served by this script's loopback stand-in (`stub`):
the container uses host networking, the channel sets the endpoint and a placeholder key, and nothing reaches an
external provider. One run file (JSON, owner-only) carries every stage. Stages, in order:

  deploy   --run-file F --worktree-root DIR
      Records the deployed tree's digest, then deploys a scratch worktree at this branch's HEAD with the test patch
      (certs/ and tools/ copied in first) through `sage_deployment.py --mode container deploy`, and proves parity.
  stub     --port P --log L
      Serves the stand-in on 127.0.0.1:P until stopped; one JSONL line per request (no message text).
  readiness --run-file F --stub-port P --stub-log L --phoenix-db DB
      Read-only. Parity with the worktree; the worktree differs from HEAD only by the test patch; Sage consuming; no
      open chat requests; host networking; the stand-in answers from inside the container and lists test-model; the
      Mythic checkout's UI source is unmodified and its UI image id is recorded; Phoenix readable; the task baseline;
      this line's offline tests pass. Verdict ready_for_live or not_ready.
  case --run-file F --name N [--plan PLAN --authorized-by TEXT]   (first case only: --plan and --authorized-by)
      The first case runs `sage-line check-attempt PLAN` and refuses on failure. Each case opens a fresh supervised
      channel and sends its prompt once. helper-accept / helper-reject: wait for the card, record the pre-decision
      state, submit the decision with the native helper, then observe. ui-accept / ui-reject / ui-stop: wait for the
      card, record the pre-decision state and stop; the operator's click in the stock UI comes next. worker: the
      Sandbox calculation; accepts only a sandbox_exec card from the Sandbox agent, with the native helper.
  observe  --run-file F --name N [--browser-evidence TEXT]
      After a UI click: waits for the request to end and records it. For a decision case it then attempts a second
      decision on the resolved card twice (the native helper, then Mythic's mutation directly) and re-reads the card
      and the reader count after a settle wait. For ui-stop it then sends `/mode` on the same channel.
  cleanup  --run-file F
      Cancels any request this run opened that is still active, with the native cancel action.
  verify   --run-file F --phoenix-db DB
      Applies the pass rules below once.
  restore  --run-file F --worktree-root DIR
      Redeploys the recorded prior commit from a scratch worktree and proves parity.

Pass rules (frozen with the plan's live check; read once):
  D1..D4 (helper-accept, helper-reject, ui-accept, ui-reject), each:
    pre   Exercised only if, before the decision, the request was `pending`, its only AI row was exactly one unresolved
          approval card for read_engagement_state with no delegation, and the reader count had not moved.
    P1    After the decision the card is `accepted` (accept) or `rejected` (reject), the reader count moved by exactly
          1 (accept) or 0 (reject), and the request is `complete`.
    P2    Both second-decision attempts were refused (the helper before any mutation; Mythic with "no longer pending"),
          and after the settle wait the card's status and the reader count are unchanged.
  R1 ui-stop: exercised as `pre` above. After the Stop click the request is `cancelled` with Sage's acknowledgement,
     the reader count did not move, and `/mode` on the same channel then completes.
  R2 worker: exactly one sandbox_exec card from the Sandbox agent was accepted, exactly one completed sandbox_exec
     tool row has status ok and stdout `4`, and the request is `complete`. Exercised only if the card's request was
     `streaming` at the decision.
  G  Every LLM span in the run window names test-model, every stand-in request carried the placeholder key, 0 Mythic
     tasks were created, at most 10 requests were opened, and the run took at most 20 minutes.
  The check passes when D1..D4, R1, R2 and G pass. A rule whose precondition did not arise is unexercised, which
  leaves the check unexercised, not failed.
Observations, not pass criteria: stand-in request count, Phoenix span count, tool_use rows, closings, wall time.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT = Path("skills") / "sage-goad-reset" / "scripts" / "sage_deployment.py"

READER = "read_engagement_state"
MODEL = "test-model"
PLACEHOLDER_KEY = "sage-check-placeholder-not-a-credential"
LEDGER_PROMPT = "Read the empty test ledger once."
LEDGER_CLOSING = "The ledger check is finished."
SANDBOX_PROMPT = "Use the Sandbox to calculate 2 + 2 in Python and report the result."
SANDBOX_MARK = "2 + 2"
SANDBOX_CODE = "print(2 + 2)"
SANDBOX_CLOSING = "The Sandbox printed 4."
DEFAULT_CLOSING = "Nothing more to do."
IDLE_COMMAND = "/mode"
COUNTER_FILE = "check_reader_entries.log"
DECISION_CASES = ("helper-accept", "helper-reject", "ui-accept", "ui-reject")
CASES = (*DECISION_CASES, "ui-stop", "worker")
LIMITS = {"minutes": 20, "requests": 10, "target_tasks": 0, "external_model_calls": 0}
CARD_TIMEOUT = 180
FINISH_TIMEOUT = 180
SETTLE_SECONDS = 10
MAX_STEPS = 8
OFFLINE_TESTS = ("tests/test_native_chat_pending_card_decisions.py", "tests/test_supervisor_approval_card_decisions.py")
MYTHIC_UI_SOURCE = "MythicReactUI"

GUARD_ANCHOR = "GUARDED_TOOLS: set[str] = {\n"
GUARD_LINE = '    "read_engagement_state",  # live-check test guard (not part of the branch)\n'
READER_ANCHOR = (
    "        try:\n"
    "            from . import engagement_ledger\n"
    "        except ImportError:\n"
    "            import engagement_ledger\n"
    "        selection, error = engagement_ledger.validate_reader_selection(numbers, after_number, limit)\n"
)
COUNTER_LINES = (
    "        # live-check reader counter (not part of the branch)\n"
    "        import time as _check_time\n"
    "        with open(Path(__file__).resolve().parents[2] / " + repr(COUNTER_FILE) + ", \"a\") as _check_counter:\n"
    "            _check_counter.write(f\"{_check_time.time()}\\n\")\n"
)

ROWS_QUERY = """
query SageApprovalCardCheckRows($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) { id author_type message metadata status
    deleted tool_output }
  chat_request_by_pk(id: $rid) { id status channel_id request_message_id error }
}
"""
TASKS_AFTER_QUERY = "query SageApprovalCardCheckTasks($after: Int!) { task(where: {display_id: {_gt: $after}}) { display_id } }"
MYTHIC_REFUSAL = "no longer pending"


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
    return _module("sage_approval_card_check_base", _SCRIPTS / "check_openai_responses.py")


# --------------------------------------------------------------------------------------------------
# The test guard (applied to a scratch worktree only)
# --------------------------------------------------------------------------------------------------

def apply_test_guard(source: str) -> str:
    """The declared test patch on `ai/langgraph/mythic_tools.py`; refuses unless each anchor occurs exactly once."""
    if source.count(GUARD_ANCHOR) != 1 or source.count(READER_ANCHOR) != 1:
        raise ValueError("test guard anchors not found exactly once in mythic_tools.py")
    if GUARD_LINE in source or COUNTER_LINES in source:
        raise ValueError("test guard already applied")
    if "\nfrom pathlib import Path\n" not in source:
        raise ValueError("mythic_tools.py no longer imports Path")
    patched = source.replace(GUARD_ANCHOR, GUARD_ANCHOR + GUARD_LINE)
    return patched.replace(READER_ANCHOR, COUNTER_LINES + READER_ANCHOR)


def declared_patch_holds(head_source: str, worktree_source: str, changed_files: list[str]) -> bool:
    return changed_files == ["Payload_Type/sage/ai/langgraph/mythic_tools.py"] and \
        apply_test_guard(head_source) == worktree_source


# --------------------------------------------------------------------------------------------------
# The stand-in model (OpenAI Chat Completions, deterministic, no message text logged)
# --------------------------------------------------------------------------------------------------

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


def stub_decision(body: dict[str, Any]) -> dict[str, Any]:
    """One reply from the request alone: a single tool call, or a closing."""
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    tools = {str((t.get("function") or {}).get("name") or "") for t in body.get("tools") or [] if isinstance(t, dict)}
    asked = " ".join(_text(m.get("content")) for m in messages if m.get("role") == "user")
    called: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            called[str(call.get("id") or "")] = str((call.get("function") or {}).get("name") or "")
    answered = {called.get(str(m.get("tool_call_id") or ""), "") for m in messages if m.get("role") == "tool"}

    def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "tool", "name": name, "arguments": arguments}

    if LEDGER_PROMPT in asked and READER in tools:
        return tool(READER, {}) if READER not in answered else {"kind": "text", "text": LEDGER_CLOSING}
    if SANDBOX_MARK in asked:
        if "sandbox_exec" in tools:
            if "sandbox_exec" not in answered:
                return tool("sandbox_exec", {"code_or_command": SANDBOX_CODE, "language": "python"})
            if "handback_to_supervisor" in tools and "handback_to_supervisor" not in answered:
                return tool("handback_to_supervisor", {"reason": "The calculation ran.", "summary": "stdout: 4",
                                                       "outcome": "complete", "next_owner": ""})
            return {"kind": "text", "text": SANDBOX_CLOSING}
        if "[step outcome]" in asked:  # the Sandbox step returned to the Supervisor
            if "respond_to_user" in tools and "respond_to_user" not in answered:
                return tool("respond_to_user", {"final_response": SANDBOX_CLOSING, "outcome": "completed"})
            return {"kind": "text", "text": SANDBOX_CLOSING}
        if "transfer_to_Sandbox" in tools and "transfer_to_Sandbox" not in answered:
            return tool("transfer_to_Sandbox", {
                "handoff_title": "Compute 2 + 2",
                "handoff_instruction": f"Run the Python code {SANDBOX_CODE} in the sandbox and report its output (2 + 2).",
            })
    return {"kind": "text", "text": DEFAULT_CLOSING}


def stub_completion(body: dict[str, Any]) -> dict[str, Any]:
    decision = stub_decision(body)
    message: dict[str, Any] = {"role": "assistant", "content": None}
    if decision["kind"] == "tool":
        message["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                                  "function": {"name": decision["name"],
                                               "arguments": json.dumps(decision["arguments"])}}]
        finish = "tool_calls"
    else:
        message["content"] = decision["text"]
        finish = "stop"
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
            "model": MODEL, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def stub_stream(completion: dict[str, Any], include_usage: bool) -> list[dict[str, Any]]:
    """The same reply as Chat Completions stream chunks."""
    choice = completion["choices"][0]
    message = choice["message"]
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": 0, **call} for call in message["tool_calls"]]
    head = {key: completion[key] for key in ("id", "created", "model")}
    chunks = [{**head, "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**head, "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]}]
    if include_usage:
        chunks.append({**head, "object": "chat.completion.chunk", "choices": [], "usage": completion["usage"]})
    return chunks


def make_stub_server(port: int, log_path: Path | None) -> ThreadingHTTPServer:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "sage-check"}]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, {"error": "only Chat Completions is served"})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            completion = stub_completion(body)
            choice = completion["choices"][0]
            calls = choice["message"].get("tool_calls") or []
            record = {"at": time.time(), "model": body.get("model"), "stream": bool(body.get("stream")),
                      "placeholder_key": self.headers.get("Authorization") == f"Bearer {PLACEHOLDER_KEY}",
                      "tools_offered": len(body.get("tools") or []),
                      "reply": calls[0]["function"]["name"] if calls else "text"}
            if log_path is not None:
                with lock, log_path.open("a") as handle:
                    handle.write(json.dumps(record) + "\n")
            if not body.get("stream"):
                self._send(200, completion)
                return
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in stub_stream(completion, include_usage):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def channel_config(port: int) -> dict[str, Any]:
    return {"provider": "openai_compatible", "model": MODEL, "API_ENDPOINT": f"http://127.0.0.1:{port}/v1",
            "API_KEY": PLACEHOLDER_KEY, "SAGE_OPENAI_API": "chat_completions", "mode": "supervised",
            "autonomous_solve": False, "max_steps": MAX_STEPS}


# --------------------------------------------------------------------------------------------------
# Pass rules (pure)
# --------------------------------------------------------------------------------------------------

def _json(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else (value or {})
    except ValueError:
        return {}


def cards(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in messages:
        metadata = row.get("metadata") or {}
        snapshot = metadata.get("input_requested")
        if metadata.get("special_type") == "input_requested" and isinstance(snapshot, dict) and not row.get("deleted"):
            data = snapshot.get("data") or {}
            out.append({"row": row["id"], "status": snapshot.get("status"), "input_type": snapshot.get("input_type"),
                        "tool_name": data.get("tool_name"), "delegation_id": metadata.get("delegation_id"),
                        "delegation_name": metadata.get("delegation_name")})
    return out


def tool_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in messages:
        metadata = row.get("metadata") or {}
        tool_use = metadata.get("tool_use") if metadata.get("special_type") == "tool_use" else None
        if isinstance(tool_use, dict):
            out.append({"row": row["id"], "tool_name": tool_use.get("tool_name"), "status": tool_use.get("status"),
                        "delegation_name": metadata.get("delegation_name"),
                        "result": _json(row.get("tool_output") or tool_use.get("result_preview") or "")})
    return out


def pre_decision(rows: dict[str, Any], reader_delta: int) -> dict[str, Any]:
    """The Supervisor-card precondition: a pending request whose only AI row is one unresolved reader card."""
    ai_rows = [row for row in rows.get("messages") or [] if row.get("author_type") == "ai" and not row.get("deleted")]
    found = cards(ai_rows)
    exercised = bool(rows.get("status") == "pending" and len(ai_rows) == 1 and len(found) == 1
                     and found[0]["status"] == "pending" and found[0]["input_type"] == "approval"
                     and found[0]["tool_name"] == READER and not found[0]["delegation_id"] and reader_delta == 0)
    return {"exercised": exercised, "request_status": rows.get("status"), "ai_rows": len(ai_rows), "cards": found,
            "reader_delta": reader_delta}


def decision_outcome(case: dict[str, Any]) -> dict[str, Any]:
    accept = case["name"].endswith("accept")
    pre = case.get("pre") or {}
    after = case.get("after") or {}
    second = case.get("second") or {}
    card_row = ((pre.get("cards") or [{}])[0]).get("row")
    after_card = next((c for c in cards((after.get("rows") or {}).get("messages") or []) if c["row"] == card_row), {})
    settled_card = next((c for c in cards((second.get("rows") or {}).get("messages") or []) if c["row"] == card_row), {})
    want_status, want_delta = ("accepted", 1) if accept else ("rejected", 0)
    p1 = bool(after_card.get("status") == want_status and after.get("reader_delta") == want_delta
              and (after.get("rows") or {}).get("status") == "complete")
    helper = second.get("helper") or {}
    raw = second.get("mythic") or {}
    p2 = bool(helper.get("refused") and helper.get("mutations_sent") == 0
              and raw.get("status") == "error" and MYTHIC_REFUSAL in str(raw.get("error") or "")
              and settled_card.get("status") == want_status and second.get("reader_delta") == want_delta)
    return {"exercised": bool(pre.get("exercised")), "P1": p1, "P2": p2,
            "detail": {"card": card_row, "after_status": after_card.get("status"),
                       "settled_status": settled_card.get("status"), "reader_after": after.get("reader_delta"),
                       "reader_settled": second.get("reader_delta"),
                       "request_status": (after.get("rows") or {}).get("status"),
                       "helper_error": helper.get("error"), "mythic_error": raw.get("error")}}


def stop_outcome(case: dict[str, Any]) -> dict[str, Any]:
    after = case.get("after") or {}
    idle = case.get("idle") or {}
    passed = bool((after.get("rows") or {}).get("status") == "cancelled" and after.get("acknowledged")
                  and after.get("reader_delta") == 0 and idle.get("status") == "complete")
    return {"exercised": bool((case.get("pre") or {}).get("exercised")), "R1": passed,
            "detail": {"request_status": (after.get("rows") or {}).get("status"),
                       "acknowledged": after.get("acknowledged"), "reader_delta": after.get("reader_delta"),
                       "idle_status": idle.get("status")}}


def worker_outcome(case: dict[str, Any]) -> dict[str, Any]:
    rows = (case.get("after") or {}).get("rows") or {}
    messages = rows.get("messages") or []
    sandbox_cards = [c for c in cards(messages) if c["tool_name"] == "sandbox_exec"
                     and c["delegation_name"] == "Sandbox"]
    accepted = [c for c in sandbox_cards if c["status"] == "accepted"]
    results = [t for t in tool_rows(messages) if t["tool_name"] == "sandbox_exec" and t["status"] == "completed"]
    ok = [t for t in results if isinstance(t["result"], dict) and t["result"].get("status") == "ok"
          and str(t["result"].get("stdout") or "").strip() == "4"]
    decided = [d for d in case.get("decisions") or [] if d.get("decision") == "accept"]
    passed = bool(len(accepted) == 1 and len(results) == 1 and len(ok) == 1 and rows.get("status") == "complete")
    return {"exercised": bool(decided and all(d.get("request_status") == "streaming" for d in decided)), "R2": passed,
            "detail": {"cards": sandbox_cards, "completed_results": len(results), "ok_four": len(ok),
                       "request_status": rows.get("status"), "decisions": case.get("decisions")}}


def global_outcome(spans: list[dict[str, Any]], stub_log: list[dict[str, Any]], new_tasks: int, requests: int,
                   wall_seconds: float) -> dict[str, Any]:
    models = sorted({str(span.get("model") or "") for span in spans})
    passed = bool(stub_log and all(entry.get("placeholder_key") for entry in stub_log)
                  and all(model == MODEL for model in models) and new_tasks == LIMITS["target_tasks"]
                  and requests <= LIMITS["requests"] and wall_seconds <= LIMITS["minutes"] * 60)
    return {"G": passed, "detail": {"span_models": models, "spans": len(spans), "stub_requests": len(stub_log),
                                    "stub_placeholder_key_all": all(e.get("placeholder_key") for e in stub_log),
                                    "new_tasks": new_tasks, "requests": requests, "wall_seconds": wall_seconds}}


def overall(results: dict[str, dict[str, Any]], global_result: dict[str, Any]) -> str:
    exercised = all(result.get("exercised") for result in results.values())
    keys = {"P1", "P2", "R1", "R2"}
    passed = all(result.get(key) for result in results.values() for key in keys if key in result)
    if not exercised:
        return "unexercised"
    return "pass" if passed and global_result.get("G") else "fail"


# --------------------------------------------------------------------------------------------------
# Mythic, deployment and readiness
# --------------------------------------------------------------------------------------------------

def _installed_dir() -> Path:
    value = os.environ.get("MYTHIC_ENV_PATH", "").strip()
    if not value:
        raise SystemExit("MYTHIC_ENV_PATH is not set")
    return Path(value).expanduser().resolve().parent / "InstalledServices" / "sage"


def reader_count() -> int:
    path = _installed_dir() / COUNTER_FILE
    return len(path.read_text().splitlines()) if path.exists() else 0


async def _rows(client: Any, request_id: int) -> dict[str, Any]:
    data = await _base()._query(client, ROWS_QUERY, rid=request_id)
    request = data.get("chat_request_by_pk") or {}
    return {"status": request.get("status"), "request": request, "messages": list(data.get("chat_message") or [])}


def _git(*args: str, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _run_json(command: list[str], cwd: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=1800)
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        payload = {"stdout_tail": result.stdout.strip()[-400:]}
    return {"returncode": result.returncode, "payload": payload, "stderr_tail": result.stderr.strip()[-400:]}


def _worktree(commit: str, label: str, root: Path) -> Path:
    path = root / f"approval-cards-{label}-{commit[:8]}-{uuid.uuid4().hex[:6]}"
    _git("worktree", "add", "--detach", str(path), commit)
    (path / ".venv").symlink_to(REPO_ROOT / ".venv")
    for name in ("certs", "tools"):  # git-ignored; the deploy sync mirrors them with --delete
        source = REPO_ROOT / "Payload_Type" / "sage" / name
        if source.is_dir():
            shutil.copytree(source, path / "Payload_Type" / "sage" / name, dirs_exist_ok=True)
    return path


def _deploy(worktree: Path) -> dict[str, Any]:
    python = str(REPO_ROOT / ".venv" / "bin" / "python")
    deployed = _run_json([python, str(worktree / _DEPLOYMENT), "--mode", "container", "deploy"], worktree)
    parity = _run_json([python, str(worktree / _DEPLOYMENT), "--mode", "container", "parity"], worktree)
    return {"deploy": {"returncode": deployed["returncode"], "stderr_tail": deployed["stderr_tail"]},
            "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                       "digest": parity["payload"].get("container_digest"),
                       "blockers": parity["payload"].get("blockers")}}


def cmd_deploy(run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("deployment"):
        raise SystemExit("this run file already holds a deployment")
    python = str(REPO_ROOT / ".venv" / "bin" / "python")
    head = _git("rev-parse", "HEAD")
    dev = _git("rev-parse", "dev")
    root = Path(args.worktree_root).expanduser().resolve()
    prior_probe = _worktree(dev, "prior-probe", root)
    prior = _run_json([python, str(prior_probe / _DEPLOYMENT), "--mode", "container", "parity"], prior_probe)
    _git("worktree", "remove", "--force", str(prior_probe))
    worktree = _worktree(head, "candidate", root)
    target = worktree / "Payload_Type" / "sage" / "ai" / "langgraph" / "mythic_tools.py"
    target.write_text(apply_test_guard(target.read_text()))
    run["deployment"] = {"head": head, "worktree": str(worktree),
                         "prior": {"commit": dev, "container_matches_dev": bool(prior["payload"].get("ready")),
                                   "container_digest": prior["payload"].get("container_digest")},
                         **_deploy(worktree), "at": time.time()}
    return run["deployment"]


def cmd_restore(run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    prior = (run.get("deployment") or {}).get("prior") or {}
    if not prior.get("commit"):
        raise SystemExit("no recorded prior deployment")
    worktree = _worktree(prior["commit"], "restore", Path(args.worktree_root).expanduser().resolve())
    run["restore"] = {"commit": prior["commit"], "worktree": str(worktree), **_deploy(worktree), "at": time.time()}
    run["restore"]["digest_matches_prior"] = run["restore"]["parity"]["digest"] == prior.get("container_digest")
    return run["restore"]


def _container_stub_probe(port: int) -> dict[str, Any]:
    program = ("import json,sys,urllib.request;r=urllib.request.urlopen(sys.argv[1],timeout=10);"
               "print(json.dumps({'status':r.status,'models':[m['id'] for m in json.load(r)['data']]}))")
    result = subprocess.run(["docker", "exec", "sage", "python3", "-I", "-c", program,
                             f"http://127.0.0.1:{port}/v1/models"], capture_output=True, text=True, timeout=60)
    return _json((result.stdout.strip().splitlines() or ["{}"])[-1]) or {"error": result.stderr.strip()[-200:]}


def _ui_identity() -> dict[str, Any]:
    mythic_root = _installed_dir().parent.parent
    image = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", "mythic_react"], capture_output=True, text=True,
                           timeout=30).stdout.strip()
    dirty = _git("status", "--porcelain", "--", MYTHIC_UI_SOURCE, cwd=mythic_root)
    return {"mythic_commit": _git("rev-parse", "HEAD", cwd=mythic_root), "ui_image": image,
            "ui_source_modified": bool(dirty)}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    deployment = run.get("deployment") or {}
    worktree = Path(deployment.get("worktree") or "")
    python = str(REPO_ROOT / ".venv" / "bin" / "python")
    parity = _run_json([python, str(worktree / _DEPLOYMENT), "--mode", "container", "parity"], worktree)
    changed = _git("diff", "--name-only", "HEAD", cwd=worktree).splitlines()
    rel = "Payload_Type/sage/ai/langgraph/mythic_tools.py"
    head_source = subprocess.run(["git", "show", f"{deployment.get('head')}:{rel}"], cwd=REPO_ROOT,
                                 capture_output=True, text=True, check=True).stdout
    patch_holds = declared_patch_holds(head_source, (worktree / rel).read_text(), changed)
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    network = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", "sage"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    tests = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *OFFLINE_TESTS],
                           capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type" / "sage")
    phoenix = Path(args.phoenix_db)
    report = {
        "head": deployment.get("head"),
        "parity": {"ready": bool(parity["payload"].get("ready")), "blockers": parity["payload"].get("blockers")},
        "declared_patch_only": patch_holds, "changed_files": changed,
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "network_mode": network,
        "stub_from_container": _container_stub_probe(args.stub_port),
        "ui": _ui_identity(),
        "phoenix_readable": phoenix.is_file(),
        "task_baseline": await base._max_task(client),
        "reader_count": reader_count(),
        "offline_tests": {"returncode": tests.returncode, "summary": tests.stdout.strip().splitlines()[-1:]},
        "limits": LIMITS,
    }
    blockers = [reason for ok, reason in (
        (report["parity"]["ready"], "the container does not run the candidate worktree"),
        (patch_holds, "the worktree differs from HEAD by more than the declared test patch"),
        (report["sage_container_running"], "Sage container not consuming"),
        (not report["open_requests"], "open chat requests"),
        (network == "host", "the Sage container is not on host networking"),
        (report["stub_from_container"].get("status") == 200
         and report["stub_from_container"].get("models") == [MODEL], "the stand-in is not reachable from the container"),
        (not report["ui"]["ui_source_modified"] and report["ui"]["ui_image"], "the Mythic UI is not the stock checkout"),
        (report["phoenix_readable"], "Phoenix database unreadable"),
        (tests.returncode == 0, "this line's offline tests fail"),
    ) if not ok]
    report["blockers"], report["verdict"] = blockers, ("not_ready" if blockers else "ready_for_live")
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": report["verdict"],
                                                    "blockers": blockers})
    run["readiness"] = report
    run["stub_port"] = args.stub_port
    run["stub_log"] = str(Path(args.stub_log).resolve())
    return report


# --------------------------------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------------------------------

def _guard(run: dict[str, Any]) -> None:
    if len(run.get("cases") or {}) >= LIMITS["requests"]:
        raise SystemExit("request limit reached")
    started = run.get("started_at")
    if started and time.time() - float(started) > LIMITS["minutes"] * 60:
        raise SystemExit("time limit reached")


async def _open(client: Any, run: dict[str, Any], name: str, prompt: str) -> dict[str, Any]:
    nc = _base().nc
    created = await nc.create_locked_channel(
        client, name=f"Approval card check: {name}", description="Supervisor approval card live check",
        metadata={"config": channel_config(int(run["stub_port"])),
                  "channel_metadata_display": {"display": nc.DEFAULT_CHANNEL_METADATA_DISPLAY}})
    channel = int(created["chat_channel_id"])
    baseline = reader_count()
    message = await nc.create_message(client, channel, prompt)
    case = {"name": name, "channel_id": channel, "channel_name": created["chat_channel_name"],
            "request_id": int(message["chat_request_id"]), "reader_baseline": baseline, "sent_at": time.time()}
    run.setdefault("cases", {})[name] = case
    run["requests_opened"] = int(run.get("requests_opened", 0)) + 1
    return case


async def _await_card(client: Any, case: dict[str, Any]) -> dict[str, Any] | None:
    nc = _base().nc
    try:
        snapshot = await nc.wait_for_request(client, case["request_id"], timeout_seconds=CARD_TIMEOUT,
                                             poll_interval_seconds=1.0, stop_on_input_requested=True)
    except (TimeoutError, asyncio.TimeoutError):
        return None
    return snapshot if snapshot.get("halt_reason") == "operator_input_requested" else None


async def _record_pre(client: Any, case: dict[str, Any]) -> None:
    rows = await _rows(client, case["request_id"])
    case["pre"] = {**pre_decision(rows, reader_count() - case["reader_baseline"]), "at": time.time()}


async def _await_end(client: Any, case: dict[str, Any]) -> dict[str, Any]:
    nc = _base().nc
    try:
        await nc.wait_for_request(client, case["request_id"], timeout_seconds=FINISH_TIMEOUT, poll_interval_seconds=1.0)
    except (TimeoutError, asyncio.TimeoutError):
        case["finish_timeout"] = True
    rows = await _rows(client, case["request_id"])
    return {"rows": rows, "reader_delta": reader_count() - case["reader_baseline"], "at": time.time()}


async def _second_decisions(client: Any, case: dict[str, Any]) -> None:
    nc = _base().nc
    card_row = ((case.get("pre") or {}).get("cards") or [{}])[0].get("row")
    opposite = "reject" if case["name"].endswith("accept") else "accept"
    mutations: list[Any] = []
    original = nc.mythic.execute_custom_query

    async def counting(client_: Any, query: str, variables: Any = None) -> Any:
        if query == nc.CHAT_INPUT_RESPONSE_MUTATION:
            mutations.append(variables)
        return await original(client_, query, variables=variables)

    helper: dict[str, Any] = {}
    nc.mythic.execute_custom_query = counting
    try:
        decide = nc.reject_pending_input_card if opposite == "reject" else nc.approve_pending_input_card
        await decide(client, case["request_id"])
        helper["refused"] = False
    except RuntimeError as error:
        helper.update(refused=True, error=str(error)[:200])
    finally:
        nc.mythic.execute_custom_query = original
    helper["mutations_sent"] = len(mutations)
    raw = {}
    if type(card_row) is int:
        result = await original(client, nc.CHAT_INPUT_RESPONSE_MUTATION,
                                variables={"messageId": card_row, "action": opposite, "response": None,
                                           "choiceId": None})
        response = result.get("chatInputResponse") or {}
        raw = {"status": response.get("status"), "error": str(response.get("error") or "")[:200]}
    await asyncio.sleep(SETTLE_SECONDS)
    case["second"] = {"helper": helper, "mythic": raw, "action": opposite,
                      "rows": await _rows(client, case["request_id"]),
                      "reader_delta": reader_count() - case["reader_baseline"], "at": time.time()}


async def _worker(client: Any, case: dict[str, Any]) -> None:
    nc = _base().nc
    case["decisions"] = []
    deadline = time.monotonic() + FINISH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            snapshot = await nc.wait_for_request(client, case["request_id"], poll_interval_seconds=1.0,
                                                 timeout_seconds=max(1.0, deadline - time.monotonic()),
                                                 stop_on_input_requested=True)
        except (TimeoutError, asyncio.TimeoutError):
            case["stopped"] = "time limit"
            break
        status = (snapshot.get("request") or {}).get("status")
        if status in nc.TERMINAL_STATUSES:
            break
        pending = nc._pending_input_requested_messages(snapshot["messages"])
        if pending and pending[0].get("id") in {d["row"] for d in case["decisions"]}:
            await asyncio.sleep(1)
            continue
        metadata = (pending[0].get("metadata") or {}) if pending else {}
        data = (metadata.get("input_requested") or {}).get("data") or {}
        accept = data.get("tool_name") == "sandbox_exec" and metadata.get("delegation_name") == "Sandbox"
        if accept:
            await nc.approve_pending_input_card(client, case["request_id"])
        else:
            await nc.reject_pending_input_card(client, case["request_id"])
        case["decisions"].append({"row": pending[0].get("id") if pending else None, "tool": data.get("tool_name"),
                                  "decision": "accept" if accept else "reject", "request_status": status})
    case["after"] = {"rows": await _rows(client, case["request_id"]), "at": time.time()}


async def cmd_case(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    nc = _base().nc
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness is not ready_for_live in this run file")
    if args.name in (run.get("cases") or {}):
        raise SystemExit(f"case {args.name} already ran in this run file; no retry")
    if not run.get("check_attempt"):
        if not (args.plan and args.authorized_by):
            raise SystemExit("the first case needs --plan and --authorized-by")
        gate = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
        run["check_attempt"] = {"returncode": gate.returncode, "output": gate.stdout.strip()[-400:]}
        if gate.returncode != 0:
            raise SystemExit(f"sage-line check-attempt refused: {gate.stdout.strip()} {gate.stderr.strip()}")
        run.update(authorized_by=args.authorized_by, started_at=time.time(),
                   task_baseline=await _base()._max_task(client),
                   phoenix_start=_base().phoenix_mark(Path(args.phoenix_db)) if args.phoenix_db else None)
    _guard(run)
    case = await _open(client, run, args.name, SANDBOX_PROMPT if args.name == "worker" else LEDGER_PROMPT)
    if args.name == "worker":
        await _worker(client, case)
        return case
    if await _await_card(client, case) is None:
        case["pre"] = {"exercised": False, "reason": "no pending card arrived"}
        return case
    await _record_pre(client, case)
    if args.name.startswith("helper-"):
        if args.name == "helper-accept":
            case["decision_result"] = await nc.approve_pending_input_card(client, case["request_id"])
        else:
            case["decision_result"] = await nc.reject_pending_input_card(client, case["request_id"])
        case["after"] = await _await_end(client, case)
        await _second_decisions(client, case)
    else:
        case["awaiting"] = "the operator's click in the stock UI"
    return case


async def cmd_observe(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    nc = _base().nc
    case = (run.get("cases") or {}).get(args.name)
    if not case or case.get("awaiting") is None:
        raise SystemExit(f"case {args.name} is not waiting for a UI click")
    case.pop("awaiting")
    case["browser_evidence"] = args.browser_evidence
    case["after"] = await _await_end(client, case)
    if args.name == "ui-stop":
        snapshot = await nc.fetch_request_snapshot(client, case["request_id"])
        case["after"]["acknowledged"] = nc.request_cancellation_acknowledged(snapshot)
        _guard(run)
        message = await nc.create_message(client, case["channel_id"], IDLE_COMMAND)
        run["requests_opened"] = int(run.get("requests_opened", 0)) + 1
        idle_id = int(message["chat_request_id"])
        try:
            await nc.wait_for_request(client, idle_id, timeout_seconds=60, poll_interval_seconds=1.0)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        case["idle"] = {"request_id": idle_id, "status": (await _rows(client, idle_id)).get("status")}
    else:
        await _second_decisions(client, case)
    return case


async def cmd_cleanup(client: Any, run: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
    nc = _base().nc
    stopped = []
    for case in (run.get("cases") or {}).values():
        status = (await _rows(client, case["request_id"])).get("status")
        if status in nc.ACTIVE_REQUEST_STATUSES:
            stopped.append(await nc.stop_native_chat_request(client, channel_id=case["channel_id"],
                                                             request_id=case["request_id"]))
    run["cleanup"] = {"stopped": stopped, "at": time.time()}
    run.setdefault("finished_at", time.time())
    return run["cleanup"]


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verify"):
        raise SystemExit("this run was already read; the result is read once")
    cases = run.get("cases") or {}
    results: dict[str, dict[str, Any]] = {}
    for name in DECISION_CASES:
        results[name] = decision_outcome(cases[name]) if name in cases else {"exercised": False}
    results["ui-stop"] = stop_outcome(cases["ui-stop"]) if "ui-stop" in cases else {"exercised": False}
    results["worker"] = worker_outcome(cases["worker"]) if "worker" in cases else {"exercised": False}
    base = _base()
    spans = base.model_spans(Path(args.phoenix_db), int(run.get("phoenix_start") or 0))
    log_path = Path(run.get("stub_log") or "")
    started = float(run.get("started_at") or 0)
    stub_log = [entry for entry in (_json(line) for line in (log_path.read_text().splitlines()
                                                             if log_path.is_file() else []))
                if isinstance(entry, dict) and float(entry.get("at") or 0) >= started]
    tasks = (await base._query(client, TASKS_AFTER_QUERY, after=int(run.get("task_baseline") or 0))).get("task") or []
    finished = float(run.get("finished_at") or time.time())
    global_result = global_outcome(spans, stub_log, len(tasks), int(run.get("requests_opened") or 0),
                                   round(finished - started, 1))
    verdict = overall(results, global_result)
    result = {"verdict": verdict, "cases": results, "global": global_result,
              "observations": {"stub_replies": [entry.get("reply") for entry in stub_log],
                               "tool_rows": {name: tool_rows((cases[name].get("after") or {}).get("rows", {})
                                                             .get("messages") or [])
                                             for name in cases}},
              "verified_at": time.time()}
    run["verify"] = result
    return result


# --------------------------------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------------------------------

async def _main(args: argparse.Namespace) -> int:
    if args.command == "stub":
        server = make_stub_server(args.port, Path(args.log))
        print(json.dumps({"serving": f"http://127.0.0.1:{args.port}/v1", "model": MODEL}), flush=True)
        try:
            await asyncio.to_thread(server.serve_forever)
        finally:
            server.server_close()
        return 0
    base = _base()
    run_path = Path(args.run_file)
    run = base._load(run_path)
    try:
        if args.command in {"deploy", "restore"}:
            result = (cmd_deploy if args.command == "deploy" else cmd_restore)(run, args)
        else:
            client = await base.nc.login()
            handlers = {"readiness": cmd_readiness, "case": cmd_case, "observe": cmd_observe,
                        "cleanup": cmd_cleanup, "verify": cmd_verify}
            result = await handlers[args.command](client, run, args)
    finally:
        base._save(run_path, run)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    stub = sub.add_parser("stub")
    stub.add_argument("--port", type=int, required=True)
    stub.add_argument("--log", required=True, help="JSONL request log (no message text)")
    for name in ("deploy", "readiness", "case", "observe", "cleanup", "verify", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--run-file", required=True)
        if name in {"deploy", "restore"}:
            command.add_argument("--worktree-root", required=True,
                                 help="directory for the scratch worktrees (outside the repo; not tmpfs)")
        if name == "readiness":
            command.add_argument("--stub-port", type=int, required=True)
            command.add_argument("--stub-log", required=True)
            command.add_argument("--phoenix-db", required=True, help="the deployed Sage's phoenix.db, read in place")
        if name in {"case", "observe"}:
            command.add_argument("--name", required=True, choices=CASES)
        if name == "case":
            command.add_argument("--plan", help="the line's plan file, for sage-line check-attempt (first case)")
            command.add_argument("--authorized-by", help="the operator's authorization for this attempt (first case)")
            command.add_argument("--phoenix-db", help="the deployed Sage's phoenix.db (first case)")
        if name == "observe":
            command.add_argument("--browser-evidence", default="",
                                 help="what recorded the UI click (for example a GIF name)")
        if name == "verify":
            command.add_argument("--phoenix-db", required=True)
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
