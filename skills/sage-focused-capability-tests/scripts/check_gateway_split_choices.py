#!/usr/bin/env python3
"""Live check: a reply the gateway splits across Chat Completions choices reaches Sage as one message.

Some OpenAI-compatible gateways send one reply's text and its tool call as separate choices. Sage's OpenAI adapter
folds such a reply into one message before LangChain keeps the first choice. This check runs two steps against the
lab gateway with the feature deployed in container mode. It reads each provider response, and the message Sage sends
back for it in the next request, from Sage's own metered capture, and the cards, effects and tasks from Mythic. Phoenix
holds no content while the capture is armed, so no rule reads it; it gives only span counts.

  S  a new supervised channel, LiteLLM Gateway, the gateway model, max 16 steps: "Run whoami on callback N."
     The first card is accepted and any later card rejected.
  K  a new Conversation channel, Custom (OpenAI-compatible) at the same gateway, the gateway model:
     "List the callbacks Mythic has right now, with host and user."

One run file (JSON, owner-only) carries every stage. ``readiness`` and ``verify`` only read Mythic, the deployment,
Phoenix and the gateway's model list. ``arm``, ``run`` and ``cleanup`` change the deployment or spend provider tokens
and need the operator's authorization.

  readiness --run-file F --route-file R --phoenix-db P --callback N
      Read-only. Container deployment and source parity, the container's adapter versions, one consuming Sage, no
      open requests, a clean tree on the line's branch, Phoenix, the task baseline, callback N active, the Sage
      Watcher paused or absent, the deployed model definitions, the operator's user-secret names, and the
      credential: the installed ``.env.local`` holds only the LiteLLM block's ``API_KEY`` and its ``API_ENDPOINT`` is
      the route file's gateway; inside the container Sage's own dotenv and TLS bootstrap load that key and a keyed
      ``GET /models`` lists the gateway model. Names, booleans, ids and status codes only. Verdict:
      ``deployment_required``, ``capture_required``, ``ready_for_live`` or ``not_ready``.
  arm --run-file F --authorized-by TEXT
      Starts the private wire receiver, writes the resource budget (provider requests and tokens, control requests,
      and payload task attempts limited to the one target task; readiness checks it once armed), saves the installed
      ``.env`` and ``.env.local`` (owner-only), adds a marked capture block to the installed ``.env`` and recreates
      the Sage container.
  run --run-file F --route-file R --phoenix-db P --authorized-by TEXT
      Step S, then step K, each terminal before the next and each after Mythic shows the container running and Sage
      has answered Mythic's on-start. Stops on more than one target task, a wrong route or model, or a limit; always
      cleans up. No step is ever re-sent.
  cleanup --run-file F
      Cancels this run's open requests, restores the installed ``.env.local`` and ``.env`` byte for byte, recreates
      the container, stops the receiver and confirms no open requests. Safe to repeat.
  verify --run-file F --phoenix-db P
      Applies the frozen pass rules once (``evaluate``). A reply with a tool call is delivered when
      (a) a later captured request in the same step carries one assistant message with exactly its calls in index
          order: ids and names equal, arguments equal once parsed (Sage's prepared arguments for a guarded call are
          read by R3), and the choices' text joined in index order, or "." when there was none; or
      (b) none of its call ids appears later, and the step ended at that reply on a Sage effect naming one of its
          calls: a control call's effect (below), or, for a guarded call, Sage's step boundary: a Mythic
          ``[step-boundary]`` message and the Supervisor's next request carrying a ``[step outcome]``.
      The step's last reply, when Sage then halted at its step limit, is not judged and is reported.
      G   at least one split gateway reply with a tool call is captured and judged. Otherwise the check did not run
          (``not_run``, not an attempt).
      R1  every split reply with a tool call is delivered.
      R2  every split call's id returns as a tool message in a later captured request in the same step, or, for a
          Sage control call whose result Sage never sends back within the turn, its effect carries on: a handoff's
          instruction (its title when it has none) reaches a later request in the step that no longer offers that
          handoff, a handback is followed by a request that offers ``respond_to_user``, and a finish's
          ``final_response`` (a continuation's summary) appears in the step's Mythic messages; a reused worker
          answer counts when the step's last answer is not the reply's own text. A guarded call refused by the
          step boundary counts as returned.
      R3  if an issue call came split: exactly one card for it or the step boundary. An accepted card leads to
          exactly one new Mythic task on callback N with the command and prepared parameters its replay shows; a
          rejected card leads to none. If no issue call came split, R3 is an observation.
      R4  every single-choice gateway reply with a tool call is delivered, and no captured request sets ``n``.
      L   at most 30 provider calls, 400,000 tokens, 30 minutes and 1 target task; no step re-sent; the installed
          files restored and the capture removed.

The route file is KEY=VALUE, owner-only, with no credential: GATEWAY_ENDPOINT and GATEWAY_MODEL (gpt-5.6-cyber). No
channel ever carries a key, an API Endpoint or an OpenAI API setting.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
_SPLIT_PATH = REPO_ROOT / "skills/sage-focused-capability-tests/scripts/check_provider_split.py"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# The provider-split check owns the capture, the receiver, the credential proof and cleanup; this check loads its own
# copy and names its capture block.
split = _module("sage_gateway_split_choices_capture", _SPLIT_PATH)
split.ENV_BEGIN = "# >>> gateway split-choices check (temporary; removed by cleanup)"
split.ENV_END = "# <<< gateway split-choices check"

GATEWAY_MODEL = "gpt-5.6-cyber"
ISSUE_TOOL = "issue_task_and_waitfor_task_output"
# Sage control calls whose acknowledgement never reaches the provider within the turn (recorded: every delivered
# handoff and finish call in a supervised and a Conversation run); R2 reads their effect instead.
HANDBACK_TOOLS = ("summarize_and_handback", "handback_to_supervisor")
FINISH_TOOL = "respond_to_user"
CONTINUATION_TOOL = "request_continuation"
WHOAMI = "Run whoami on callback {callback}."
LIST = "List the callbacks Mythic has right now, with host and user."
# (step, channel, prompt, answers cards)
STEPS = (("S", "S", WHOAMI, True), ("K", "K", LIST, False))
# channel -> (provider, mode, max steps)
# S ran out of steps before a later request could carry its last reply (raised so the oracle exists; L still binds).
CHANNELS = {"S": ("litellm", "supervised", 16), "K": ("openai_compatible", "conversation", 4)}
LIMITS = {"provider_calls": 30, "provider_tokens": 400_000, "active_seconds": 30 * 60, "target_tasks": 1}
split.LIMITS = {**split.LIMITS, "provider_calls": LIMITS["provider_calls"],
                "provider_tokens": LIMITS["provider_tokens"]}
BUDGET_RESOURCES = ("provider_requests", "provider_tokens", "control_requests", "payload_task_attempts")
_PROVIDER_SPLIT_BUDGET = split.budget_config


def budget_config() -> dict[str, Any]:
    """The provider-split budget plus the task resource Sage's admission reserves, limited to L's one target task, so
    Sage itself refuses a second task. Installed in this check's copy only, where its arm writes ``budget.json``."""
    config = _PROVIDER_SPLIT_BUDGET()
    config["resources"]["payload_task_attempts"] = {"limit": LIMITS["target_tasks"], "cleanup_reserve": 0}
    return config


split.budget_config = budget_config
# Sage's own words for the effects the rules read (model.py: the step boundary, its step outcome, the step-limit halt)
# and the marker Sage puts on a guarded call's prepared arguments (mythic_tools.py, the supervised issue action).
STEP_BOUNDARY = "[step-boundary]"
STEP_OUTCOME = "[step outcome]"
STEP_LIMIT_HALT = "Halted: step budget exhausted"
PREPARED_MARKER = "_sage_prepared_action"
STEP_TIMEOUT = 600
SETTLE_SECONDS = 3
CHANGED_FILES = (
    "Payload_Type/sage/ai/openai_api.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/sage_chat/config.py",
    "skills/sage-focused-capability-tests/scripts/check_gateway_split_choices.py",
)
BRANCH = "feature/gateway-split-choices"
TASKS_AFTER_QUERY = """
query SageSplitChoiceTasks($after: Int!) {
  task(where: {display_id: {_gt: $after}}, order_by: {display_id: asc}) {
    display_id command_name params completed status callback { display_id } responses(limit: 1) { id }
  }
}
"""
CALLBACK_QUERY = """
query SageSplitChoiceCallback($id: Int!) {
  callback(where: {display_id: {_eq: $id}}) { display_id active payload { payloadtype { name } } }
}
"""


# --------------------------------------------------------------------------------------------------
# Route file, channels, identity, evidence
# --------------------------------------------------------------------------------------------------

def read_route(path: Path) -> dict[str, str]:
    """KEY=VALUE route file: owner-only, no credential, the plan's gateway model exactly."""
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"{path} must be owner-only (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if any(word in key.upper() for key in values for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        raise SystemExit(f"{path} must not contain a credential; the installed .env.local supplies it")
    missing = [key for key in ("GATEWAY_ENDPOINT", "GATEWAY_MODEL") if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    if values["GATEWAY_MODEL"] != GATEWAY_MODEL:
        raise SystemExit(f"GATEWAY_MODEL must be {GATEWAY_MODEL}; no substitute")
    gateway = urlsplit(values["GATEWAY_ENDPOINT"])
    if gateway.scheme != "https" or not gateway.hostname or (gateway.hostname or "").endswith("api.openai.com"):
        raise SystemExit("GATEWAY_ENDPOINT must be the gateway's https base URL")
    return values


def channel_config(route: dict[str, str], channel: str) -> dict[str, Any]:
    """One channel's native configuration: no key, endpoint, OpenAI API or retry policy, ever."""
    provider, mode, max_steps = CHANNELS[channel]
    config = {"provider": provider, "model": route["GATEWAY_MODEL"], "mode": mode, "autonomous_solve": False,
              "max_steps": max_steps}
    return {"config": config, "channel_metadata_display": {"display": "expanded; max=15"}}


def expected_url(route: dict[str, str]) -> str:
    return route["GATEWAY_ENDPOINT"].rstrip("/") + "/chat/completions"


def _identity() -> dict[str, Any]:
    identity = split._identity()
    identity["files"] = {path: split._sha256_bytes((REPO_ROOT / path).read_bytes()) for path in CHANGED_FILES}
    return identity


def evidence_dir(run: dict[str, Any]) -> Path:
    if not run.get("evidence_dir"):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run["evidence_dir"] = str(REPO_ROOT / ".sage_history/2026/11/diagnostics/gateway-split-choices"
                                  / f"{stamp}-live-check")
    return split._private_dir(Path(run["evidence_dir"]))


# --------------------------------------------------------------------------------------------------
# Evidence readers (pure, tested offline)
# --------------------------------------------------------------------------------------------------

def _ordered_choices(reply: dict[str, Any]) -> list[dict[str, Any]]:
    choices = [c for c in reply.get("choices") or [] if isinstance(c, dict)]
    return [c for _p, c in sorted(enumerate(choices), key=lambda item: (
        item[1].get("index") if type(item[1].get("index")) is int else item[0], item[0]))]


def _arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def reply_calls(reply: dict[str, Any]) -> list[dict[str, Any]]:
    """Every tool call in a Chat Completions reply, in choice order: ``{"id", "name", "args"}``."""
    calls = []
    for choice in _ordered_choices(reply):
        for call in (choice.get("message") or {}).get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append({"id": str(call.get("id") or ""), "name": str(function.get("name") or ""),
                          "args": _arguments(function.get("arguments"))})
    return calls


def reply_text(reply: dict[str, Any]) -> str:
    """The reply's text as one message carries it: non-blank choice texts in choice order, blank-line joined."""
    texts = [(c.get("message") or {}).get("content") for c in _ordered_choices(reply)]
    return "\n\n".join(t for t in texts if isinstance(t, str) and t.strip())


def is_split(item: dict[str, Any]) -> bool:
    reply = item.get("reply") or {}
    return len(reply.get("choices") or []) > 1 and bool(reply_calls(reply))


def _message_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool calls a replayed assistant message carries, in order: ``{"id", "name", "args"}``."""
    calls = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") or {}
            calls.append({"id": str(call.get("id") or ""), "name": str(function.get("name") or ""),
                          "args": _arguments(function.get("arguments"))})
    return calls


def _prepared(args: Any) -> bool:
    """Whether Sage prepared these arguments for a guarded call before replaying them."""
    visibility = args.get("visibility_context") if isinstance(args, dict) else None
    return isinstance(visibility, dict) and visibility.get(PREPARED_MARKER) is True


def replay_of(reply: dict[str, Any], later: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The assistant message a later request in the step replays for this reply: the first that carries any of its
    call ids (Chat Completions sends the kept message back before its tool results)."""
    ids = {call["id"] for call in reply_calls(reply)}
    for item in later:
        for entry in item["input"] if isinstance(item["input"], list) else []:
            if (isinstance(entry, dict) and entry.get("role") == "assistant"
                    and ids & {call["id"] for call in _message_calls(entry)}):
                return entry
    return None


def replay_problem(reply: dict[str, Any], message: dict[str, Any]) -> str:
    """"" when the replayed message carries exactly the reply, else what differs. Ids and names must match in
    order; arguments must be equal once parsed, except Sage's prepared arguments for a guarded call (R3 reads those);
    the text must be the choices' text joined in index order, or "." when there was none."""
    want, got = reply_calls(reply), _message_calls(message)
    if [(c["id"], c["name"]) for c in want] != [(c["id"], c["name"]) for c in got]:
        return "the replayed calls differ (id, name or order)"
    changed = [w["id"] for w, g in zip(want, got) if not _prepared(g["args"]) and w["args"] != g["args"]]
    if changed:
        return "the replayed arguments differ for " + ", ".join(changed)
    text, content = reply_text(reply), message.get("content")
    if not (content == text or (not text and content == ".")):
        return "the replayed text differs from the choices' text"
    return ""


def ids_reappear(reply: dict[str, Any], later: list[dict[str, Any]]) -> bool:
    """Whether any of the reply's call ids appears in a later request of the step, as a call or a result."""
    ids = {call["id"] for call in reply_calls(reply)}
    return any(ids & (set(found["calls"]) | set(found["results"]))
               for found in (split.replayed(item) for item in later))


def _texts(rows: dict[str, Any]) -> list[str]:
    return [str(m.get("message") or "") for m in rows.get("messages") or []]


def boundary_refused(call: dict[str, Any], position: int, items: list[dict[str, Any]], rows: dict[str, Any],
                     offered: dict[str, list[str]]) -> str:
    """How Sage's step boundary shows for a guarded call it removed, or "" (pure). Both effects must name the call:
    a Mythic ``[step-boundary]`` message, and the next captured request is the Supervisor's (it offers
    ``respond_to_user``) carrying a ``[step outcome]``."""
    named = f"`{call['name']}`"
    if not any(STEP_BOUNDARY in text and named in text for text in _texts(rows)):
        return ""
    if position + 1 >= len(items):
        return ""
    after = items[position + 1]
    if FINISH_TOOL not in offered.get(str(after["request_id"]), []):
        return ""
    entries = after["input"] if isinstance(after["input"], list) else []
    outcomes = [str(entry.get("content") or "") for entry in entries if isinstance(entry, dict)]
    if not any(STEP_OUTCOME in text and named in text for text in outcomes):
        return ""
    return "Sage's step boundary refused it and the Supervisor ran next"


def halted_after(position: int, items: list[dict[str, Any]], rows: dict[str, Any]) -> bool:
    """Whether this is the step's last reply and Sage then halted at its step limit (the reply is not judged)."""
    return position == len(items) - 1 and any(STEP_LIMIT_HALT in text for text in _texts(rows))


def delivery(position: int, items: list[dict[str, Any]], rows: dict[str, Any],
             offered: dict[str, list[str]]) -> dict[str, Any]:
    """Whether a reply with a tool call reached Sage (pure): (a) a later request replays it exactly, or (b) none of
    its ids appears later and the step ended on a Sage effect naming one of its calls."""
    reply, later = items[position]["reply"], items[position + 1:]
    message = replay_of(reply, later)
    if message is not None:
        problem = replay_problem(reply, message)
        return {"delivered": not problem, "how": "" if problem else "replayed", "problem": problem}
    if ids_reappear(reply, later):
        return {"delivered": False, "how": "", "problem": "an id reappears without its replayed message"}
    for call in reply_calls(reply):
        effect = (carried_on(call, reply, later, offered, rows)
                  or boundary_refused(call, position, items, rows, offered))
        if effect:
            return {"delivered": True, "how": effect, "problem": ""}
    return {"delivered": False, "how": "", "problem": "no replay, and no Sage effect names its calls"}


def requests_setting_n(records: list[dict[str, Any]]) -> int:
    """Captured provider requests whose body sets ``n``."""
    count = 0
    for record in records:
        if record.get("kind") != "model_request":
            continue
        body = split._body(((record.get("record") or {}).get("payload") or {}).get("body_base64"))
        if isinstance(body, dict) and "n" in body:
            count += 1
    return count


def request_tools(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Captured request id -> the tool names that request offered."""
    offered: dict[str, list[str]] = {}
    for record in records:
        if record.get("kind") != "model_request":
            continue
        payload = (record.get("record") or {}).get("payload") or {}
        body = split._body(payload.get("body_base64"))
        tools = body.get("tools") if isinstance(body, dict) else None
        offered[str(payload.get("request_id"))] = [
            str(((tool.get("function") or {}).get("name")) or tool.get("name") or "")
            for tool in tools or [] if isinstance(tool, dict)]
    return offered


def _carries(item: dict[str, Any], text: str) -> bool:
    dumped = json.dumps(item.get("input") or [], ensure_ascii=False)
    return text in dumped or json.dumps(text, ensure_ascii=False)[1:-1] in dumped


def carried_on(call: dict[str, Any], reply: dict[str, Any], later: list[dict[str, Any]],
               offered: dict[str, list[str]], rows: dict[str, Any]) -> str:
    """How a delivered control call's effect shows, or "" when it does not (pure)."""
    name, args = call["name"], call["args"] if isinstance(call["args"], dict) else {}
    if name.startswith("transfer_to_"):
        needle = str(args.get("handoff_instruction") or "").strip() or str(args.get("handoff_title") or "").strip()
        if needle and any(_carries(item, needle) and name not in offered.get(str(item["request_id"]), [name])
                          for item in later):
            return "the handoff's worker received it"
        return ""
    if name in HANDBACK_TOOLS:
        return ("the Supervisor ran next" if any(FINISH_TOOL in offered.get(str(item["request_id"]), [])
                                                 for item in later) else "")
    messages = [str(m.get("message") or "") for m in rows.get("messages") or []]
    if name == FINISH_TOOL:
        final = str(args.get("final_response") or "").strip()
        answers = split.native_answers(rows)
        if final:
            return "Mythic shows its final_response" if any(final in text for text in messages) else ""
        if args.get("reuse_latest_worker_answer") and answers and answers[-1].strip() != reply_text(reply).strip():
            return "Mythic shows the reused worker answer"
        return ""
    if name == CONTINUATION_TOOL:
        summary = str(args.get("summary") or "").strip()
        return "Mythic shows its summary" if summary and any(summary in text for text in messages) else ""
    return ""


def cards(rows: dict[str, Any]) -> list[dict[str, Any]]:
    """Every approval card in a Mythic request's messages, in order, resolved or not."""
    nc = split._runner()
    found = []
    for message in rows.get("messages") or []:
        for metadata in nc._metadata_containers(message):
            card = metadata.get("input_requested")
            if isinstance(card, dict):
                found.append(card)
                break
    return found


def _empty(value: Any) -> bool:
    return value in (None, "", {}, [], "{}", "[]")


def same_parameters(called: Any, issued: Any) -> bool:
    """The call's parameters and the task's, read as JSON where they are JSON; any two empties are the same."""
    called, issued = _arguments(called), _arguments(issued)
    return (_empty(called) and _empty(issued)) or called == issued


# --------------------------------------------------------------------------------------------------
# Pass rules (pure, tested offline)
# --------------------------------------------------------------------------------------------------

def _issue_r3(step: str, items: list[dict[str, Any]], rows: dict[str, Any], offered: dict[str, list[str]],
              tasks: list[dict[str, Any]], callback: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """R3 for one step's split issue calls (pure). Sage raises one card per issue call that reaches the approval gate,
    in order, so after the calls the step boundary removed, the k-th remaining issue call pairs with the k-th card."""
    calls = []
    for position, item in enumerate(items):
        reply, later = item["reply"], items[position + 1:]
        message = replay_of(reply, later)
        prepared = {call["id"]: call["args"] for call in _message_calls(message)} if message else {}
        absent = message is None and not ids_reappear(reply, later)
        for call in reply_calls(reply):
            if call["name"] != ISSUE_TOOL:
                continue
            calls.append({"call": call, "split": is_split(item), "judged": not halted_after(position, items, rows),
                          "prepared": prepared.get(call["id"]),
                          "refused": boundary_refused(call, position, items, rows, offered) if absent else ""})
    gated = [entry for entry in calls if not entry["refused"]]
    issue_cards = [card for card in cards(rows) if (card.get("data") or {}).get("tool_name") == ISSUE_TOOL]
    accepted = sum(1 for card in issue_cards if card.get("status") == "accepted")
    counts = {"issue_calls": len(calls), "split_issue_calls": sum(1 for entry in calls if entry["split"]),
              "refused_by_step_boundary": len(calls) - len(gated), "issue_cards": len(issue_cards),
              "accepted_issue_cards": accepted}
    entries = []
    for entry in calls:
        if not (entry["split"] and entry["judged"]):
            continue
        call = entry["call"]
        result: dict[str, Any] = {"step": step, "call": call["id"]}
        if entry["refused"]:
            result.update(outcome="step boundary", pass_=True)
        else:
            index = gated.index(entry)
            card = issue_cards[index] if len(issue_cards) == len(gated) else None
            shown = ((card or {}).get("data") or {}).get("arguments") or {}
            args = entry["prepared"] if entry["prepared"] is not None else shown
            one_card = card is not None and (
                shown == entry["prepared"] if entry["prepared"] is not None
                else (isinstance(call["args"], dict) and shown.get("command") == call["args"].get("command")
                      and shown.get("callback_display_id") == call["args"].get("callback_display_id")))
            status = str((card or {}).get("status") or "")
            if status == "accepted":
                effect = (len(tasks) == 1 and int((tasks[0].get("callback") or {}).get("display_id") or 0) == callback
                          and tasks[0].get("command_name") == args.get("command")
                          and same_parameters(args.get("parameters"), tasks[0].get("params")))
            elif status == "rejected":
                effect = len(tasks) <= accepted
            else:
                effect = False
            result.update(outcome=f"card {status or 'unresolved'}", one_card_for_the_call=one_card,
                          task_effect_matches=effect, pass_=one_card and effect)
        result["pass"] = result.pop("pass_")
        entries.append(result)
    return entries, counts


def evaluate(run: dict[str, Any], wire: dict[str, list[dict[str, Any]]], rows: dict[str, dict[str, Any]],
             tasks: list[dict[str, Any]], totals: dict[str, Any], offered: dict[str, list[str]]) -> dict[str, Any]:
    """The frozen rules, read once. ``wire`` holds each step's captured exchanges in request order. Every rule reads
    the capture and Mythic; none reads Phoenix, which holds no content while the capture is armed."""
    callback = int(run["callback"])
    criteria: dict[str, dict[str, Any]] = {}
    judged: list[tuple[str, int, dict[str, Any], dict[str, Any]]] = []  # (step, position, item, delivery)
    not_judged = []
    for step, items in wire.items():
        step_rows = rows.get(step) or {}
        for position, item in enumerate(items):
            if not reply_calls(item["reply"]):
                continue
            if halted_after(position, items, step_rows):
                not_judged.append({"step": step, "response": item["reply"].get("id"), "split": is_split(item),
                                   "reason": "Sage halted at its step limit directly after this reply"})
                continue
            judged.append((step, position, item, delivery(position, items, step_rows, offered)))
    criteria["not_judged"] = {"replies": not_judged}

    split_judged = [entry for entry in judged if is_split(entry[2])]
    criteria["G"] = {"pass": bool(split_judged), "split_responses": len(split_judged)}

    r1 = [{"step": step, "response": item["reply"].get("id"), **found}
          for step, _position, item, found in split_judged]
    criteria["R1"] = {"pass": bool(r1) and all(entry["delivered"] for entry in r1), "responses": r1}

    r2 = []
    for step, position, item, _found in split_judged:
        items, step_rows = wire[step], rows.get(step) or {}
        later = items[position + 1:]
        for call in reply_calls(item["reply"]):
            returned = any(call["id"] in split.replayed(entry)["results"] for entry in later)
            carried = "" if returned else (carried_on(call, item["reply"], later, offered, step_rows)
                                           or boundary_refused(call, position, items, step_rows, offered))
            r2.append({"step": step, "call": call["id"], "name": call["name"], "returned": returned,
                       "carried_on": carried})
    criteria["R2"] = {"pass": bool(r2) and all(entry["returned"] or entry["carried_on"] for entry in r2),
                      "calls": r2}

    r3: dict[str, Any] = {"calls": [], "steps": {}}
    for step, items in wire.items():
        entries, counts = _issue_r3(step, items, rows.get(step) or {}, offered, tasks, callback)
        r3["calls"] += entries
        r3["steps"][step] = counts
    r3["applies"] = bool(r3["calls"])
    r3["new_tasks"] = len(tasks)
    if r3["applies"]:
        r3["pass"] = all(entry["pass"] for entry in r3["calls"])
    criteria["R3"] = r3

    r4 = [{"step": step, "response": item["reply"].get("id"), **found}
          for step, _position, item, found in judged
          if item["api"] == "chat_completions" and len(item["reply"].get("choices") or []) == 1]
    criteria["R4"] = {"pass": all(entry["delivered"] for entry in r4) and totals.get("requests_setting_n") == 0,
                      "responses": r4, "requests_setting_n": totals.get("requests_setting_n")}

    files = run.get("files_after_cleanup") or {}
    sends = {step: info.get("sends") for step, info in (run.get("steps") or {}).items()}
    limits = {"provider_calls": totals.get("provider_calls", 0) <= LIMITS["provider_calls"],
              "provider_tokens": totals.get("provider_tokens", 0) <= LIMITS["provider_tokens"],
              "active_seconds": float(run.get("active_seconds") or 0) <= LIMITS["active_seconds"],
              "target_tasks": len(tasks) <= LIMITS["target_tasks"],
              "no_step_resent": bool(sends) and all(value == 1 for value in sends.values()),
              "files_restored": bool(files.get("env_restored") and files.get("env_local_restored")),
              "capture_removed": bool(run.get("capture_released")) and not run.get("capture")}
    criteria["L"] = {"pass": all(limits.values()), **limits, "totals": totals}

    stopped = run.get("stopped") or {}
    not_run_reason = ""
    if stopped and not run.get("steps"):
        not_run_reason = f"stopped before the first step: {stopped.get('reason')}"
    elif not criteria["G"]["pass"]:
        not_run_reason = "no split gateway reply with a tool call was captured and judged"
    if not_run_reason:
        verdict = "not_run"
    else:
        rules = ["R1", "R2", "R4", "L"] + (["R3"] if r3["applies"] else [])
        verdict = "pass" if all(criteria[name]["pass"] for name in rules) else "fail"
    return {"verdict": verdict, "not_run_reason": not_run_reason, "criteria": criteria}


# --------------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------------

async def _callback_state(client: Any, callback: int) -> dict[str, Any]:
    base = split._base()
    rows = (await base._query(client, CALLBACK_QUERY, id=int(callback))).get("callback") or []
    row = rows[0] if rows else {}
    return {"found": bool(rows), "active": bool(row.get("active")),
            "payload_type": (((row.get("payload") or {}).get("payloadtype")) or {}).get("name")}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = split._base()
    route = read_route(Path(args.route_file))
    if run.get("callback") not in (None, int(args.callback)):
        raise SystemExit("this run file names another callback; a new callback needs a new run file")
    run["callback"] = int(args.callback)
    installed = Path(split._deployment()._installed_service_dir())
    readiness = await base._query(client, base.nc.READINESS_QUERY)
    containers = readiness.get("consuming_container") or []
    python = str(REPO_ROOT / ".venv/bin/python")
    parity = split._run_json([python, str(split._DEPLOYMENT_PATH), "--mode", "container", "parity"])
    check = split._run_json([python, str(split._DEPLOYMENT_PATH), "--mode", "container", "check"])
    local_path = installed / ".env.local"
    local_text = local_path.read_text() if local_path.exists() else ""
    if run.get("capture"):  # once armed, the installed .env.local is the one saved at arm
        local_text = Path(run["capture"]["local_backup"]).read_text()
    local = split.dotenv_state(local_text, route["GATEWAY_ENDPOINT"])
    report = {
        "route": {"gateway_model": route["GATEWAY_MODEL"]},
        "callback": {"display_id": run["callback"], **await _callback_state(client, run["callback"])},
        "deployment_check": {"returncode": check["returncode"], "tail": check["tail"]},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "identity": _identity(),
        "container_adapter": split._container_adapters(),
        "phoenix_readable": bool(args.phoenix_db) and Path(args.phoenix_db).is_file(),
        "task_baseline": await base._max_task(client),
        "watcher": split.watcher_state(installed / "sage.db"),
        "settings_error": split.settings_error(await split._subscriptions(client)),
        "credentials": {
            "installed_local": local,
            "installed_env": split.dotenv_state((installed / ".env").read_text()
                                                if (installed / ".env").exists() else ""),
            "container": split._container_probe(1, route),
            "operator_secrets": await split._operator_secret_names(client),
            "channel_config_sets_key_or_endpoint": any(
                name in channel_config(route, channel)["config"] for channel in CHANNELS
                for name in (*split.CREDENTIAL_NAMES, *split.ENDPOINT_NAMES, "SAGE_OPENAI_API",
                             "provider_retry_policy")),
        },
        "capture": split._capture_state(run),
        "default_channel_path": await split.default_channel_path(client),
        "limits": LIMITS,
        "check_sha256": split._sha256_bytes(Path(__file__).read_bytes()),
    }
    blockers = []
    if report["deployment_check"]["returncode"] != 0:
        blockers.append("deployment check")
    if not report["sage_container_running"]:
        blockers.append("Sage container not consuming")
    if report["open_requests"]:
        blockers.append("open chat requests")
    if report["identity"]["dirty"]:
        blockers.append("working tree dirty")
    if report["identity"]["branch"] != BRANCH:
        blockers.append(f"the checkout is not {BRANCH}")
    if report["container_adapter"] != split.EXPECTED_ADAPTER:
        blockers.append("container adapter versions")
    if not report["phoenix_readable"]:
        blockers.append("Phoenix unreadable")
    if not (report["callback"]["found"] and report["callback"]["active"]):
        blockers.append(f"callback {run['callback']} is not active")
    watcher = report["watcher"]
    if not watcher.get("readable") or watcher.get("active"):
        blockers.append("the Sage Watcher is not paused or absent (pause it from its owner channel)")
    credentials = report["credentials"]
    blockers += split.phase_blockers(1, credentials["installed_local"], credentials["container"], route)
    if credentials["installed_env"]["credentials"] or credentials["installed_env"]["endpoints"]:
        blockers.append("the installed .env sets a key or endpoint")
    secrets = credentials["operator_secrets"]
    if not secrets.get("resolved"):
        blockers.append("the run operator's user secrets could not be resolved")
    elif set(secrets.get("names") or []) & set(split.SHADOWING_SECRETS):
        blockers.append("a user secret would shadow the installed provider settings")
    if credentials["channel_config_sets_key_or_endpoint"]:
        blockers.append("a channel configuration sets a key, endpoint, OpenAI API or retry policy")
    blockers += split.default_path_blockers(report["default_channel_path"])
    capture = report["capture"]
    report["budget"] = budget_state(run)
    if capture.get("armed") and not report["budget"]["valid"]:
        blockers.append("the installed budget.json does not declare " + ", ".join(BUDGET_RESOURCES)
                        + " with one target task")
    if not report["parity"]["ready"] or report["settings_error"]:
        verdict = "deployment_required"
        if report["settings_error"]:
            blockers.append("deployed settings: " + report["settings_error"])
    elif blockers:
        verdict = "not_ready"
    elif not capture.get("armed"):
        verdict = "capture_required"
    elif not (capture["receiver_alive"] and not capture["receiver_failed"] and capture["process_start_after_arm"]
              and capture["budget_valid"] and capture["env_block_present"] and capture["provider_reservations"] == 0):
        verdict = "not_ready"
        blockers.append("capture or budget not live")
    else:
        verdict = "ready_for_live"
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


def budget_state(run: dict[str, Any]) -> dict[str, Any]:
    """Once armed: whether the installed ``budget.json`` declares exactly this check's four resources, with the task
    resource limited to L's one target task (Sage's admission reserves it; an undeclared resource is refused)."""
    capture = run.get("capture") or {}
    if not capture:
        return {"armed": False, "valid": False}
    try:
        resources = json.loads((Path(capture["host_dir"]) / "budget.json").read_text())["resources"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"armed": True, "valid": False, "resources": []}
    task = resources.get("payload_task_attempts") if isinstance(resources, dict) else None
    valid = (isinstance(resources, dict) and set(resources) == set(BUDGET_RESOURCES)
             and task == {"limit": LIMITS["target_tasks"], "cleanup_reserve": 0})
    return {"armed": True, "valid": valid, "resources": sorted(resources) if isinstance(resources, dict) else []}


async def cmd_arm(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    evidence_dir(run)  # this check's evidence folder; the capture block carries this check's marker
    return await split.cmd_arm(client, run, args)


async def _send(client: Any, run: dict[str, Any], step: str, channel_id: int, prompt: str, *,
                answers_cards: bool) -> dict[str, Any]:
    """Send one message once and wait until its request ends, accepting the first card and rejecting later ones."""
    nc = split._base().nc
    entry: dict[str, Any] = {"channel_id": channel_id, "sent_ns": time.time_ns(), "sends": 1,
                             "cards_seen": 0, "cards_accepted": 0, "cards_rejected": 0}
    message = await nc.create_message(client, channel_id, prompt)
    entry["request_id"] = int(message["chat_request_id"])
    run["steps"][step] = entry
    deadline = time.monotonic() + STEP_TIMEOUT
    try:
        while True:
            result = await nc.wait_for_request(client, entry["request_id"],
                                               timeout_seconds=max(1.0, deadline - time.monotonic()),
                                               stop_on_input_requested=answers_cards)
            if result.get("halt_reason") != "operator_input_requested":
                break
            entry["cards_seen"] += 1
            if entry["cards_seen"] == 1:
                await nc.approve_pending_input_card(client, entry["request_id"])
                entry["cards_accepted"] += 1
            else:
                await nc.reject_pending_input_card(client, entry["request_id"])
                entry["cards_rejected"] += 1
    except (TimeoutError, asyncio.TimeoutError) as error:
        entry["timeout"] = str(error) or "request timeout"
        await nc.stop_native_chat_request(client, channel_id=channel_id, request_id=entry["request_id"])
    await asyncio.sleep(SETTLE_SECONDS)
    return entry


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness has not reported ready_for_live in this run file")
    if run.get("steps"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    base = split._base()
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    record = Path(run["capture"]["record"])
    run.update(authorized_by=args.authorized_by, started_at=time.time(), identity=_identity(),
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix),
               steps={}, channels={}, route_summary={k: route[k] for k in ("GATEWAY_ENDPOINT", "GATEWAY_MODEL")},
               settings_error=split.settings_error(await split._subscriptions(client)))
    started = time.monotonic()
    try:
        for step, channel, prompt, answers_cards in STEPS:
            failure = await split.await_sage_ready(client, base, record, phase=1)
            if failure:
                run["stopped"] = {"step": step, "reason": failure}
                break
            created = await base.nc.create_locked_channel(
                client, name=f"Gateway split choices live check {channel}",
                description="Sage gateway split-choices live check", metadata=channel_config(route, channel))
            run["channels"][channel] = int(created["chat_channel_id"])
            mark = base.phoenix_mark(phoenix)
            entry = await _send(client, run, step, run["channels"][channel],
                                prompt.format(callback=run["callback"]), answers_cards=answers_cards)
            entry.update(phoenix_after=mark, phoenix_until=base.phoenix_mark(phoenix),
                         tasks_after=await base._max_task(client))
            records = split.read_wire(record)
            items = split.attribute(records, run["steps"])[0][step]
            everything = split.exchanges(records)
            stop = ""
            if entry["tasks_after"] - run["task_baseline"] > LIMITS["target_tasks"]:
                stop = "more than one target task was created"
            elif any(item["url"].rstrip("/") != expected_url(route) or item["model"] != route["GATEWAY_MODEL"]
                     for item in items):
                stop = "wrong model or route"
            elif len(everything) > LIMITS["provider_calls"]:
                stop = "provider call limit"
            elif sum(split.usage_tokens(item) for item in everything) > LIMITS["provider_tokens"]:
                stop = "provider token limit"
            elif time.monotonic() - started > LIMITS["active_seconds"]:
                stop = "active wall-time limit"
            if stop:
                run["stopped"] = {"step": step, "reason": stop}
                break
    finally:
        run["finished_at"] = time.time()
        run["active_seconds"] = round(time.monotonic() - started, 1)
        run["phoenix_end"] = base.phoenix_mark(phoenix)
        run["cleanup_result"] = await split.cmd_cleanup(client, run, args)
    return {"channels": run["channels"], "steps": run["steps"], "stopped": run.get("stopped"),
            "cleanup": run["cleanup_result"]}


async def cmd_cleanup(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return await split.cmd_cleanup(client, run, args)


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verify"):
        raise SystemExit("this run was already read; a result is read once")
    if not run.get("steps") and not run.get("stopped"):
        raise SystemExit("this run file holds no attempt")
    base = split._base()
    capture = run.get("capture_released") or run.get("capture") or {}
    records = split.read_wire(Path(capture["record"])) if capture.get("record") else []
    wire, unattributed = split.attribute(records, run.get("steps") or {})
    rows = {step: await base.request_rows(client, int(info["request_id"]))
            for step, info in (run.get("steps") or {}).items()}
    tasks = list((await base._query(client, TASKS_AFTER_QUERY, after=int(run.get("task_baseline") or 0))
                  ).get("task") or [])
    everything = split.exchanges(records)
    totals = {"provider_calls": len(everything), "provider_tokens": sum(split.usage_tokens(i) for i in everything),
              "requests_setting_n": requests_setting_n(records), "unattributed": len(unattributed)}
    phoenix = Path(args.phoenix_db)  # span counts only (an observation); no rule reads Phoenix
    result = evaluate(run, wire, rows, tasks, totals, request_tools(records))
    result["observations"] = {
        "split_responses_by_step": {step: sum(1 for i in items if is_split(i)) for step, items in wire.items()},
        "split_tools": {step: [c["name"] for i in items if is_split(i) for c in reply_calls(i["reply"])]
                        for step, items in wire.items()},
        "provider_calls_by_step": {step: len(items) for step, items in wire.items()},
        "llm_spans_by_step": {step: len(base.model_spans(phoenix, info["phoenix_after"], info["phoenix_until"]))
                              for step, info in (run.get("steps") or {}).items() if "phoenix_until" in info},
        "tasks": [{"display_id": t.get("display_id"), "command": t.get("command_name"),
                   "callback": (t.get("callback") or {}).get("display_id"), "completed": t.get("completed"),
                   "status": t.get("status"), "has_output": bool(t.get("responses"))} for t in tasks],
        "k_answer": (split.native_answers(rows.get("K") or {}) or [""])[-1][:300],
        "gateway_version": "not exposed in the captured bodies (headers are not captured)",
        "seconds": run.get("active_seconds"),
    }
    run["verify"] = result
    return result


async def _main(args: argparse.Namespace) -> int:
    base = split._base()
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
        if name == "readiness":
            command.add_argument("--callback", required=True, type=int, help="the active Apollo callback's display id")
        if name in {"arm", "run"}:
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this deployment change or attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
