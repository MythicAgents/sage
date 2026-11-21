#!/usr/bin/env python3
"""Live check for switching a native Sage chat's model in place and recovering from a wrong model name.

Steps share one run file (JSON). ``readiness`` and ``verify`` only read; ``run`` is authorized live activity: it may
create one API key owned by the logged-in operator (the channel's creator), creates one conversation channel bound to
that key, sends the prompts and slash commands below and edits that channel's settings three times.

  readiness --run-file F --route-file R --phoenix-db P
      Mythic login; Sage chat container running; no open chat requests; deployed source identity; adapter versions;
      the offline model-switch tests pass; Phoenix readable; Mythic's stored Sage definition declares /model; the
      logged-in operator owns (or will create) an API key the channel can use, so the key's owner is the channel's
      creator. The route file names the direct OpenAI endpoint, model A, model B and a safety identifier; the API key
      is never in it (the operator's API_KEY user secret or the container environment supplies it).
  run --run-file F --route-file R --phoenix-db P --authorized-by TEXT
      S1-S8 below, in one channel. Stops at the first step that breaks a ceiling.
  verify --run-file F --phoenix-db P
      Applies the pass rules once, reading Mythic's own rows and the live Phoenix trace.

Pass rules, written before the run and read once:
  S1 Model nonexistent-model-a, OpenAI API Chat Completions, Daybreak Red; label prompt: a configuration error naming
     the setting; zero provider calls; no replaced or restart refusal.
  S2 OpenAI API set to Responses in settings; same prompt: one error terminal naming the Model value, the provider's
     message and the fix; no refusal.
  S3 `/model`, then `/model <A>`: shows the current model; Mythic's stored config has model A; Sage's channel
     metadata Model chip has click "/model".
  S4 label prompt: completes; the notice names A; every S4 model span names A.
  S5 read-only list_callbacks prompt: a function call is produced and its id consumed in the same step (if no call is
     produced, the check did not run).
  S6 settings set Model B with Daybreak Off; "Repeat my label without tools": completes; the answer has the label;
     spans name only B.
  S7 `/model nonexistent-model-b`; "Repeat my label": error names nonexistent-model-b; no span names A or B; no refusal.
  S8 settings set Daybreak Red (Model unchanged); `/model <A>`; "Repeat my label without tools": completes with the
     label; spans name only A.
  Every request ends terminal; zero target tasks; 16 provider calls, 1,048,576 tokens, 4,096 output tokens per call,
  20 minutes. S6 and S8 exercise the model seam.
Observations, not pass criteria: wording, callback count, latency, the calling agent.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sage_openai_check_for_model_switch", _HERE / "check_openai_responses.py")
cor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cor
_spec.loader.exec_module(cor)  # type: ignore[union-attr]
nc = cor.nc
REPO_ROOT = cor.REPO_ROOT

LABEL = "amber-seven"
LABEL_PROMPT = f"Remember the label {LABEL}. Explain what an API endpoint is in one sentence."
TOOL_PROMPT = ("For a read-only status check, use list_callbacks once, then tell me the label from the previous turn "
               "and summarize the returned callback list. Do not run tasks.")
REPEAT_NO_TOOLS = "Repeat my label without tools."
REPEAT = "Repeat my label."
WRONG_A, WRONG_B = "nonexistent-model-a", "nonexistent-model-b"
REFUSALS = cor.REFUSALS
CEILINGS = {"provider_calls": 16, "total_tokens": 1_048_576, "output_tokens_per_call": 4096,
            "minutes": 20, "target_tasks": 0}
MAX_STEPS = 4
TERMINAL = cor.TERMINAL
KEY_NAME = "Sage model-switch live check"
# The channel key needs what Mythic requires of any AI channel key plus what this check reads; the rest of Sage's
# declared scopes keep its normal tool gating (conversation mode issues no task either way).
REQUIRED_KEY_SCOPES = {"apitoken.write", "chat-ai.write", "callback.read"}
CHANGED_FILES = (
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/operator_error.py",
    "Payload_Type/sage/sage_chat/metadata.py",
    "Payload_Type/sage/sage_chat/model_command.py",
    "Payload_Type/sage/sage_chat/models.py",
    "Payload_Type/sage/sage_chat/service.py",
    "Payload_Type/sage/sage_chat/session.py",
    "Payload_Type/sage/sage_chat/slash.py",
)
OFFLINE_TESTS = (
    "tests/test_openai_responses_native_chat.py",
    "tests/test_openai_responses_battery.py",
    "tests/test_openai_responses_route.py",
)
OPERATOR_QUERY = "query SageModelSwitchMe($name: String!) { operator(where: {username: {_eq: $name}}) { id username } }"
KEYS_QUERY = """
query SageModelSwitchKeys($operatorId: Int!) {
  apitokens(where: {operator_id: {_eq: $operatorId}, active: {_eq: true}, deleted: {_eq: false},
                    token_type: {_eq: "api"}}, order_by: {id: asc}) { id name scopes operator_id }
}
"""
CHANNEL_QUERY = """
query SageModelSwitchChannel($id: Int!) { chat_channel_by_pk(id: $id) { id created_by apitokens_id ai_metadata } }
"""
UPDATE_CHANNEL = cor.UPDATE_CHANNEL


def read_route(path: Path) -> dict[str, str]:
    """The owner-only route file: API_ENDPOINT, model (A), model_b and SAGE_OPENAI_SAFETY_IDENTIFIER; never a key."""
    values = cor.read_route(path)
    if not values.get("model_b"):
        raise SystemExit(f"{path} is missing model_b")
    chosen = [values["model"], values["model_b"], WRONG_A, WRONG_B]
    if len(set(chosen)) != 4 or any(a != b and a.startswith(b + "-") for a in chosen for b in chosen):
        raise SystemExit("model A, model B and the two wrong names must all differ, none a dated form of another")
    return values


def channel_config(route: dict[str, str]) -> dict[str, Any]:
    """S1's settings: a wrong model, Chat Completions and Daybreak Red (an invalid route Sage refuses to build)."""
    return {"config": {"provider": "openai", "model": WRONG_A, "API_ENDPOINT": route["API_ENDPOINT"],
                       "mode": "conversation", "autonomous_solve": False, "max_steps": MAX_STEPS,
                       "SAGE_OPENAI_API": "chat_completions", "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red",
                       "SAGE_OPENAI_SAFETY_IDENTIFIER": route["SAGE_OPENAI_SAFETY_IDENTIFIER"]},
            "channel_metadata_display": {"display": nc.DEFAULT_CHANNEL_METADATA_DISPLAY}}


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "head": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "Payload_Type/sage")),
            "files": {path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() for path in CHANGED_FILES}}


def _offline_tests() -> dict[str, Any]:
    completed = subprocess.run([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:logging",
                                *OFFLINE_TESTS], capture_output=True, text=True, cwd=REPO_ROOT / "Payload_Type/sage")
    return {"returncode": completed.returncode, "summary": completed.stdout.strip().splitlines()[-1:]}


# --------------------------------------------------------------------------------------------------
# Pure rules (tested offline)
# --------------------------------------------------------------------------------------------------

def over_ceiling(spans: list[dict[str, Any]], elapsed_seconds: float) -> str:
    if len(spans) > CEILINGS["provider_calls"]:
        return "provider call ceiling"
    if sum(s["prompt_tokens"] + s["completion_tokens"] for s in spans) > CEILINGS["total_tokens"]:
        return "token ceiling"
    if any(s["completion_tokens"] > CEILINGS["output_tokens_per_call"] for s in spans):
        return "output token ceiling"
    if elapsed_seconds > CEILINGS["minutes"] * 60:
        return "wall-time ceiling"
    return ""


def names(span_model: str, model: str) -> bool:
    """A span names ``model`` when its model is that name or a dated snapshot of it."""
    return span_model == model or span_model.startswith(model + "-")


def declares_model_command(subscriptions: Any) -> bool:
    for item in subscriptions or []:
        try:
            model = json.loads(item) if isinstance(item, str) else item
        except ValueError:
            continue
        if isinstance(model, dict) and model.get("name") == "Sage":
            commands = (model.get("metadata") or {}).get("slash_commands") or []
            return any(str(c.get("name")) == "model" for c in commands if isinstance(c, dict))
    return False


def usable_key(keys: list[dict[str, Any]]) -> dict[str, Any] | None:
    for key in keys:
        scopes = set(key.get("scopes") or [])
        if "*" in scopes or REQUIRED_KEY_SCOPES <= scopes:
            return key
    return None


def model_chip(ai_metadata: Any) -> dict[str, Any] | None:
    items = (((ai_metadata or {}).get("channel_metadata") or {}).get("items") or []) if isinstance(ai_metadata, dict) else []
    return next((item for item in items if isinstance(item, dict) and item.get("key") == "cfg_model"), None)


def criteria_for(run: dict[str, Any], rows: dict[str, list[dict[str, Any]]],
                 spans: dict[str, list[dict[str, Any]]], tool: dict[str, Any]) -> dict[str, Any]:
    """Each step's pass rule, from Mythic's rows (per request, in order) and the step's Phoenix model spans."""
    route = run["readiness"]["route_models"]
    model_a, model_b = route["a"], route["b"]

    def texts(step: str, index: int | None = None) -> str:
        requests = rows.get(step, [])
        selected = requests if index is None else ([requests[index]] if -len(requests) <= index < len(requests) else [])
        return "\n".join(str(m.get("message") or "") for request in selected for m in request.get("messages", []))

    def status(step: str, index: int = -1) -> str:
        requests = rows.get(step) or [{}]
        return str(requests[index].get("status") or "")

    every_text = "\n".join(texts(step) for step in rows)
    no_refusal = not any(refusal in every_text for refusal in REFUSALS)
    step_models = {step: [str(s.get("model") or "") for s in spans.get(step, [])] for step in spans}
    stored = run.get("stored_after_s3") or {}
    return {
        "S1": status("S1") == "error" and "OpenAI Daybreak" in texts("S1") and not spans.get("S1") and no_refusal,
        "S2": status("S2") == "error" and WRONG_A in texts("S2") and ("/model" in texts("S2") or "**Model**" in texts("S2"))
        and "rejected" in texts("S2") and no_refusal,
        "S3": status("S3", 0) == "complete" and WRONG_A in texts("S3", 0)
        and status("S3", 1) == "complete" and f"Model set to `{model_a}`" in texts("S3", 1)
        and stored.get("model") == model_a and (stored.get("chip") or {}).get("click") == "/model",
        "S4": status("S4") == "complete" and f"now uses model `{model_a}`" in texts("S4")
        and bool(step_models.get("S4")) and all(names(m, model_a) for m in step_models["S4"]),
        "S5": tool["passed"],
        "S6": status("S6") == "complete" and LABEL in texts("S6")
        and bool(step_models.get("S6")) and all(names(m, model_b) for m in step_models["S6"]),
        "S7": status("S7", 0) == "complete" and status("S7", -1) == "error" and WRONG_B in texts("S7")
        and not any(names(m, model_a) or names(m, model_b) for m in step_models.get("S7", [])) and no_refusal,
        "S8": status("S8", -1) == "complete" and LABEL in texts("S8", -1)
        and bool(step_models.get("S8")) and all(names(m, model_a) for m in step_models["S8"]),
        "terminal": all(str(request.get("status") or "") in TERMINAL for step in rows for request in rows[step]),
        "tasks_and_ceilings": not run.get("stopped")
        and all(info.get("tasks_after") == run.get("task_baseline") for info in (run.get("steps") or {}).values())
        and over_ceiling([s for step in spans for s in spans[step]],
                         float(run.get("finished_at", 0)) - float(run.get("started_at", 0))) == "",
    }


# --------------------------------------------------------------------------------------------------
# Mythic
# --------------------------------------------------------------------------------------------------

async def _operator_id(client: Any) -> int:
    username = str(getattr(client, "username", "") or nc.DEFAULT_USER)
    rows = (await cor._query(client, OPERATOR_QUERY, name=username)).get("operator") or []
    if len(rows) != 1:
        raise SystemExit(f"could not resolve the logged-in operator {username!r}")
    return int(rows[0]["id"])


async def _keys(client: Any, operator_id: int) -> list[dict[str, Any]]:
    return list((await cor._query(client, KEYS_QUERY, operatorId=operator_id)).get("apitokens") or [])


async def _settings(client: Any, channel_id: int, **config: Any) -> dict[str, Any]:
    """Change only the named settings (read-modify-write, as the settings dialog does)."""
    stored = (await cor._query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {}
    metadata = copy.deepcopy(stored.get("ai_metadata") or {})
    metadata["config"] = {**(metadata.get("config") or {}), **config}
    result = await cor._query(client, UPDATE_CHANNEL, channelId=channel_id, metadata=metadata)
    return {"changed": sorted(config), "result": result.get("chatUpdateChannel")}


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    route = read_route(Path(args.route_file))
    readiness = await cor._query(client, nc.READINESS_QUERY)
    containers = readiness.get("consuming_container") or []
    operator_id = await _operator_id(client)
    key = usable_key(await _keys(client, operator_id))
    subscriptions = ((await cor._query(client, cor.CONTAINER_QUERY)).get("consuming_container") or [{}])[0]
    report = {
        "route": cor.route_summary(route),
        "route_models": {"a": route["model"], "b": route["model_b"]},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "chat_container_id": int(containers[0]["id"]) if containers else None,
        "open_requests": (await cor._query(client, cor.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "deployment": cor._deployment_check(),
        "identity": _identity(),
        "adapter": cor._adapter_versions(),
        "offline_tests": _offline_tests(),
        "phoenix_readable": Path(args.phoenix_db).is_file(),
        "task_baseline": await cor._max_task(client),
        "declares_model_command": declares_model_command(subscriptions.get("subscriptions")),
        "channel_creator_operator_id": operator_id,
        "creator_key": {"id": key["id"], "scopes": key.get("scopes")} if key else {"create": sorted(REQUIRED_KEY_SCOPES)},
        "ceilings": CEILINGS,
    }
    if report["phoenix_readable"]:
        report["phoenix_mark"] = cor.phoenix_mark(Path(args.phoenix_db))
    report["ready"] = bool(
        report["route"]["direct_openai"] and report["sage_container_running"] and not report["open_requests"]
        and report["deployment"]["returncode"] == 0 and not report["identity"]["dirty"]
        and report["identity"]["branch"] == "feature/chat-model-switch"
        and report["adapter"] == cor.EXPECTED_ADAPTER and report["offline_tests"]["returncode"] == 0
        and report["phoenix_readable"] and report["declares_model_command"]
    )
    run["readiness"] = report
    return report


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not (run.get("readiness") or {}).get("ready"):
        raise SystemExit("readiness has not passed in this run file")
    if run.get("steps"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    model_a, model_b = route["model"], route["model_b"]
    run.update(authorized_by=args.authorized_by, started_at=time.time(), identity=_identity(),
               task_baseline=await cor._max_task(client), phoenix_start=cor.phoenix_mark(phoenix), steps={})
    started = time.monotonic()

    operator_id = await _operator_id(client)
    key = usable_key(await _keys(client, operator_id))
    if key is None:
        created = await cor._query(client, nc.CREATE_TOKEN_MUTATION, operatorId=operator_id, name=KEY_NAME,
                                   scopes=sorted(REQUIRED_KEY_SCOPES))
        made = created.get("createAPIToken") or {}
        if made.get("status") != "success":
            raise SystemExit(f"could not create the channel's API key: {made.get('error')}")
        key = {"id": made["id"], "scopes": made.get("scopes"), "created": True}
    run["channel_key"] = {"id": int(key["id"]), "owner": operator_id, "created": bool(key.get("created"))}
    created = await cor._query(client, nc.CREATE_CHANNEL_MUTATION, name=f"Model switch live check {int(time.time())}",
                               description="Sage in-place model switch live check",
                               containerId=int(run["readiness"]["chat_container_id"]), model="Sage",
                               tokenId=int(key["id"]), metadata=nc.default_ai_metadata(channel_config(route)))
    made = created.get("chatCreateChannel") or {}
    if made.get("status") != "success":
        raise SystemExit(f"channel creation failed: {made.get('error')}")
    channel_id = int(made.get("channel_id") or made.get("id"))
    stored = (await cor._query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {}
    run["channel"] = {"id": channel_id, "created_by": stored.get("created_by"), "apitokens_id": stored.get("apitokens_id")}

    async def send(step: str, message: str) -> None:
        mark = cor.phoenix_mark(phoenix)
        sent = await nc.create_message(client, channel_id, message)
        request_id = int(sent["chat_request_id"])
        await nc.wait_for_request(client, request_id, timeout_seconds=300)
        info = run["steps"].setdefault(step, {"requests": [], "phoenix_after": mark})
        info["requests"].append({"request_id": request_id, "message": message})
        info["phoenix_until"] = cor.phoenix_mark(phoenix)
        info["tasks_after"] = await cor._max_task(client)

    plan = [
        ("S1", [("send", LABEL_PROMPT)]),
        ("S2", [("settings", {"SAGE_OPENAI_API": "responses"}), ("send", LABEL_PROMPT)]),
        ("S3", [("send", "/model"), ("send", f"/model {model_a}"), ("store", None)]),
        ("S4", [("send", LABEL_PROMPT)]),
        ("S5", [("send", TOOL_PROMPT)]),
        ("S6", [("settings", {"model": model_b, "SAGE_OPENAI_ACCESS_PROGRAM": "standard"}), ("send", REPEAT_NO_TOOLS)]),
        ("S7", [("send", f"/model {WRONG_B}"), ("send", REPEAT)]),
        ("S8", [("settings", {"SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red"}), ("send", f"/model {model_a}"),
                ("send", REPEAT_NO_TOOLS)]),
    ]
    for step, actions in plan:
        for kind, value in actions:
            if kind == "send":
                await send(step, value)
            elif kind == "settings":
                run.setdefault("settings_changes", []).append({"step": step, **await _settings(client, channel_id, **value)})
            elif kind == "store":
                stored = (await cor._query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {}
                metadata = stored.get("ai_metadata") or {}
                run["stored_after_s3"] = {"model": (metadata.get("config") or {}).get("model"),
                                          "chip": model_chip(metadata)}
        stop = over_ceiling(cor.model_spans(phoenix, run["phoenix_start"]), time.monotonic() - started)
        if run["steps"].get(step, {}).get("tasks_after", run["task_baseline"]) != run["task_baseline"]:
            stop = "a target task was created"
        if stop:
            run["stopped"] = {"step": step, "reason": stop}
            break
    run["finished_at"] = time.time()
    return {"channel": run["channel"], "channel_key": run["channel_key"], "steps": run["steps"],
            "stopped": run.get("stopped")}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    phoenix = Path(args.phoenix_db)
    steps = run.get("steps") or {}
    rows = {step: [await cor.request_rows(client, int(r["request_id"])) for r in info["requests"]]
            for step, info in steps.items()}
    spans = {step: cor.model_spans(phoenix, info["phoenix_after"], info["phoenix_until"]) for step, info in steps.items()}
    s5_rows = (rows.get("S5") or [{"status": "", "messages": []}])[-1]
    tool = cor.tool_step_result(spans.get("S5", []), s5_rows)
    criteria = criteria_for(run, rows, spans, tool)
    every = [s for step in spans for s in spans[step]]
    observations = {
        "provider_calls": len(every),
        "total_tokens": sum(s["prompt_tokens"] + s["completion_tokens"] for s in every),
        "max_completion_tokens": max((s["completion_tokens"] for s in every), default=0),
        "wall_seconds": round(float(run.get("finished_at", 0)) - float(run.get("started_at", 0)), 1),
        "span_models": {step: [s["model"] for s in spans[step]] for step in spans},
        "tool_step": tool,
        "channel": run.get("channel"), "channel_key": run.get("channel_key"),
        "stored_after_s3": run.get("stored_after_s3"),
    }
    if not tool["round_trip_calls"]:
        verdict = "not-run (S5 produced no function call)"
    else:
        verdict = "pass" if all(criteria.values()) else "fail"
    run["verify"] = {"criteria": criteria, "observations": observations, "verdict": verdict}
    return run["verify"]


async def _main(args: argparse.Namespace) -> int:
    run_path = Path(args.run_file)
    run = cor._load(run_path)
    client = await nc.login()
    handlers = {"readiness": cmd_readiness, "run": cmd_run, "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        cor._save(run_path, run)
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
        if name == "run":
            command.add_argument("--authorized-by", required=True,
                                 help="the operator's authorization for this bounded attempt")
    return parser


def main() -> None:
    sys.exit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
