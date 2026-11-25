#!/usr/bin/env python3
"""Live check: each OpenAI-family route sends the API it should, read from the wire against the real providers.

Providers OpenAI, LiteLLM Gateway (``litellm``) and Custom (``openai_compatible``) all build Sage's OpenAI adapter.
With OpenAI API blank, OpenAI on OpenAI's own endpoint uses Responses (stateless, encrypted reasoning, Daybreak
named) and every other route uses Chat Completions. This check runs six requests in four fresh Conversation
channels and reads each provider request and response from Sage's own metered capture.

One run file (JSON, owner-only) carries every stage. ``readiness`` and ``verify`` only read Mythic, the deployment
and the provider model lists. ``arm``, ``run`` and ``cleanup`` change the deployment or spend provider tokens and
need the operator's authorization.

  readiness --run-file F --route-file R --phoenix-db P
      Read-only. Container deployment and source parity, the container's adapter versions, one consuming Sage, no
      open requests, Phoenix, the task baseline, the Sage Watcher paused or absent, the deployed model definitions,
      the operator's user-secret names, and the phase 1 credential: the installed ``.env.local`` holds only the
      LiteLLM block's ``API_KEY`` and its ``API_ENDPOINT`` is the route file's gateway; inside the container Sage's
      own dotenv and TLS bootstrap load that key and a keyed ``GET /models`` lists the gateway model. The phase 2
      credential (the ``# OpenAI`` block's key in the repo ``.env.local``) is proved by a keyed, verified-TLS
      ``GET /models`` from this host listing both OpenAI models. The runner's default channel path: neither
      ``SAGE_BACKING_APITOKEN_ID`` nor ``SAGE_CHAT_CONTAINER_ID`` set, exactly one live operation, the operation's
      own bot among the active bots, and the token it would bind (id and scope names). Names, booleans, ids and
      status codes only. Verdict: ``deployment_required``, ``capture_required``, ``ready_for_live`` or ``not_ready``.
  arm --run-file F --authorized-by TEXT
      Starts the private wire receiver, writes the resource budget, saves the installed ``.env`` and ``.env.local``
      (owner-only), adds a marked capture block to the installed ``.env`` and recreates the Sage container.
  run --run-file F --route-file R --phoenix-db P --authorized-by TEXT
      Phase 1 (LiteLLM block): channel L (LiteLLM Gateway) L1, channel M (OpenAI, gateway endpoint from the
      environment) M1. Phase 2: writes the OpenAI block's key into the installed ``.env.local`` as its only active
      provider block (no API_ENDPOINT anywhere), recreates the container and proves the key from inside it; channel
      O (gpt-5.6-sol, effort low) O1, O2; channel G (gpt-4.1) G1. Phase 3: restores the installed ``.env.local``
      byte for byte, recreates the container, proves the gateway key; channel L L2. Channels come from the runner's
      default path. Before each step (its channel and its message) the check waits until Mythic shows the Sage
      container running and the capture shows Sage's answer to Mythic's on-start after its latest process start.
      Each step is terminal before the next. Stops on a target task, a wrong route, a limit, a failed phase proof
      or a container that never becomes ready; always restores and cleans up.
  cleanup --run-file F
      Cancels this set's open requests, restores the installed ``.env.local`` and ``.env`` byte for byte, recreates
      the container, stops the receiver and confirms no open requests. Safe to repeat.
  verify --run-file F --phoenix-db P
      Applies the frozen pass rules P1-P9 once (``evaluate``). Each captured provider request belongs to the step
      in flight when Sage sent it: from the step's send until Sage's last captured complete answer to its Mythic
      request (``attribute``). P9 reads each run channel's backing token from Mythic. The check did not run
      (``not_run``, not an attempt) when O1 or L1 made no tool call, when a phase's credential proof failed before
      its first step, or when the container never became ready for a step.

The route file is KEY=VALUE, owner-only, with no credential: GATEWAY_ENDPOINT, GATEWAY_MODEL,
OPENAI_REASONING_MODEL, OPENAI_PLAIN_MODEL. No channel ever carries a key or an API Endpoint.

Recreations: ``arm`` and ``cleanup`` each recreate the container to load and remove the capture block; the
attempt's own sequence recreates it twice (phase 2 and phase 3), the two the limits name.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import importlib.util
import json
import math
import os
import signal
import sqlite3
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
_MODEL_SWITCH_PATH = REPO_ROOT / "skills/sage-focused-capability-tests/scripts/check_model_switch.py"

LIST = "List the callbacks Mythic has right now, with host and user."
INTEGRITY = "Which of those runs with the highest integrity?"
# (step, phase, channel, prompt)
STEPS = (
    ("L1", 1, "L", LIST),
    ("M1", 1, "M", LIST),
    ("O1", 2, "O", LIST),
    ("O2", 2, "O", INTEGRITY),
    ("G1", 2, "G", LIST),
    ("L2", 3, "L", INTEGRITY),
)
# channel -> (provider, model route-file key, route, effort)
CHANNELS = {
    "L": ("litellm", "GATEWAY_MODEL", "gateway", ""),
    "M": ("openai", "GATEWAY_MODEL", "gateway", ""),
    "O": ("openai", "OPENAI_REASONING_MODEL", "openai", "low"),
    "G": ("openai", "OPENAI_PLAIN_MODEL", "openai", ""),
}
ROUTE_API = {"gateway": "chat_completions", "openai": "responses"}
PHASE_ROUTE = {1: "gateway", 2: "openai", 3: "gateway"}
EXPECTED_ROUTE = {"GATEWAY_MODEL": "bedrock-claude-4-6-sonnet", "OPENAI_REASONING_MODEL": "gpt-5.6-sol",
                  "OPENAI_PLAIN_MODEL": "gpt-4.1"}
OPENAI_ENDPOINT = "https://api.openai.com/v1"
RESPONSES_FIELDS = ("store", "include", "access_programs")
REASONING_INCLUDE = "reasoning.encrypted_content"
LIMITS = {"channels": 4, "requests": 6, "provider_calls": 30, "provider_tokens": 400_000,
          "active_seconds": 30 * 60, "recreations": 2, "target_tasks": 0}
TOKEN_RESERVATION = 65_536  # per call, settled to actual usage; the largest recent Sage call was ~35k tokens
OUTPUT_TOKENS_PER_CALL = 4096
CLEANUP_SECONDS = 5 * 60
MAX_STEPS = 4
STEP_TIMEOUT = 300
EXPECTED_ADAPTER = {"langchain-openai": "1.7.2", "openai": "3.58.0", "langchain": "1.4.6"}
EXPECTED_PROVIDERS = [["OpenAI", "openai"], ["LiteLLM Gateway", "litellm"],
                      ["Custom (OpenAI-compatible)", "openai_compatible"], ["AWS Bedrock", "bedrock"],
                      ["Anthropic", "anthropic"], ["Ollama", "ollama"]]
EXPECTED_API_CHOICES = [["Use configured default", ""], ["Chat Completions", "chat_completions"],
                        ["Responses", "responses"]]
NOTICE_PREFIXES = ("⚠️ **BloodHound is not connected**", "Applied this chat's updated OpenAI settings",
                   "This chat now uses model")
CREDENTIAL_NAMES = ("API_KEY", "OPENAI_API_KEY")
ENDPOINT_NAMES = ("API_ENDPOINT", "OPENAI_API_BASE", "OPENAI_BASE_URL", "LANGSMITH_GATEWAY")
PROVIDER_KEYS = ("provider", "model", "API_ENDPOINT", "API_KEY")
# A user secret with any of these names would outrank the installed files for every channel in the run.
SHADOWING_SECRETS = ("API_KEY", "API_ENDPOINT", "provider", "model", "SAGE_OPENAI_API",
                     "SAGE_OPENAI_ACCESS_PROGRAM", "SAGE_REASONING_EFFORT")
OPERATOR_SECRETS_QUERY = ("query SageProviderSplitSecrets($operatorId: Int) { getOperatorSecrets(operator_id: "
                          "$operatorId) { status error secrets } }")
# Before each step the check waits until Mythic shows the Sage container running and the capture shows Sage's
# answer to Mythic's on-start after its latest process start; Sage closes still-open requests during on-start.
GATE_SECONDS = 120
ON_START_RESPONSE = "container_on_start_response"
CHAT_RESPONSE = "chat_response"
EXPLICIT_BINDING = ("SAGE_BACKING_APITOKEN_ID", "SAGE_CHAT_CONTAINER_ID")
LIVE_OPERATIONS_QUERY = "query SageProviderSplitOperations { operation(where: {deleted: {_eq: false}, complete: {_eq: false}}) { id } }"
CHANNEL_BINDING_QUERY = """
query SageProviderSplitBinding($id: Int!) {
  chat_channel_by_pk(id: $id) { id apitokens_id }
}
"""
TOKEN_ROW_QUERY = """
query SageProviderSplitToken($id: Int!) {
  apitokens_by_pk(id: $id) {
    id operator_id operation_id active deleted token_type scopes operator { username account_type }
  }
}
"""
OPERATION_QUERY = "query SageProviderSplitOperation($id: Int!) { operation_by_pk(id: $id) { id name } }"
ENV_BEGIN = "# >>> provider-split check (temporary; removed by cleanup)"
ENV_END = "# <<< provider-split check"
PHASE2_HEADER = "# OpenAI (provider-split check, phase 2; restored by the check)"
CHANGED_FILES = (
    "Payload_Type/sage/ai/openai_api.py",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/operator_error.py",
    "Payload_Type/sage/sage_chat/config.py",
    "Payload_Type/sage/sage_chat/models.py",
    "skills/sage-focused-capability-tests/scripts/check_provider_split.py",
    "skills/sage-live-runner/scripts/native_chat.py",
    "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
    "skills/sage-goad-reset/scripts/readiness_contract.py",
)


# --------------------------------------------------------------------------------------------------
# Route file, channels, identity, evidence
# --------------------------------------------------------------------------------------------------

def read_route(path: Path) -> dict[str, str]:
    """KEY=VALUE route file: owner-only, no credential, the plan's gateway and models exactly."""
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
    missing = [key for key in ("GATEWAY_ENDPOINT", *EXPECTED_ROUTE) if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    for key, expected in EXPECTED_ROUTE.items():
        if values[key] != expected:
            raise SystemExit(f"{key} must be {expected}; no substitute")
    gateway = urlsplit(values["GATEWAY_ENDPOINT"])
    if gateway.scheme != "https" or not gateway.hostname or (gateway.hostname or "").endswith("api.openai.com"):
        raise SystemExit("GATEWAY_ENDPOINT must be the gateway's https base URL")
    return values


def channel_config(route: dict[str, str], channel: str) -> dict[str, Any]:
    """One channel's native configuration: Conversation, max_steps 4; no key, endpoint or OpenAI API, ever."""
    provider, model_key, _route, effort = CHANNELS[channel]
    config: dict[str, Any] = {"provider": provider, "model": route[model_key], "mode": "conversation",
                              "autonomous_solve": False, "max_steps": MAX_STEPS}
    if effort:
        config["SAGE_REASONING_EFFORT"] = effort
    return {"config": config, "channel_metadata_display": {"display": "expanded; max=15"}}


def expected_url(route: dict[str, str], channel: str) -> str:
    kind = CHANNELS[channel][2]
    base = route["GATEWAY_ENDPOINT"] if kind == "gateway" else OPENAI_ENDPOINT
    return base.rstrip("/") + ("/chat/completions" if kind == "gateway" else "/responses")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "head": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "Payload_Type/sage", "skills")),
            "files": {path: _sha256_bytes((REPO_ROOT / path).read_bytes()) for path in CHANGED_FILES}}


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def evidence_dir(run: dict[str, Any]) -> Path:
    if not run.get("evidence_dir"):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run["evidence_dir"] = str(REPO_ROOT / ".sage_history/2026/11/diagnostics/openai-provider-split"
                                  / f"{stamp}-live-check")
    return _private_dir(Path(run["evidence_dir"]))


# --------------------------------------------------------------------------------------------------
# Dotenv files (pure; values never leave these functions except the one key written into the installed copy)
# --------------------------------------------------------------------------------------------------

def _is_header(stripped: str) -> bool:
    text = stripped.lstrip("#").strip()
    return stripped.startswith("#") and bool(text) and "=" not in text and len(text) <= 80


def dotenv_state(text: str, gateway: str = "") -> dict[str, Any]:
    """Active provider-relevant assignments by name, each with the header of the block it sits in."""
    header = ""
    credentials: list[dict[str, str]] = []
    endpoints: list[str] = []
    provider_keys: list[str] = []
    endpoint_matches = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if _is_header(stripped):
                header = stripped.lstrip("#").strip()
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not value:
            continue
        if key in CREDENTIAL_NAMES:
            credentials.append({"name": key, "block": header})
        if key in ENDPOINT_NAMES:
            endpoints.append(key)
            if key == "API_ENDPOINT" and gateway:
                endpoint_matches = value.rstrip("/") == gateway.rstrip("/")
        if key in PROVIDER_KEYS:
            provider_keys.append(key)
    return {"credentials": credentials, "endpoints": endpoints, "provider_keys": provider_keys,
            "api_endpoint_is_gateway": endpoint_matches}


def openai_block_key(repo_local: str) -> str:
    """The ``API_KEY`` value in the repo ``.env.local``'s ``# OpenAI`` block (commented or not)."""
    inside = False
    for line in repo_local.splitlines():
        stripped = line.strip()
        if _is_header(stripped):
            inside = stripped.lstrip("#").strip() == "OpenAI"
            continue
        if not inside:
            continue
        candidate = stripped.lstrip("#").strip()
        key, separator, value = candidate.partition("=")
        if separator and key.strip() == "API_KEY" and value.strip():
            return value.strip()
    raise SystemExit("the repo .env.local has no API_KEY in its # OpenAI block")


def phase2_local(original: bytes, key: str) -> bytes:
    """The installed ``.env.local`` for phase 2: every active provider line commented out, then the OpenAI block."""
    lines = []
    for line in original.decode().splitlines():
        name, separator, value = line.partition("=")
        if separator and value.strip() and name.strip() in PROVIDER_KEYS and not line.lstrip().startswith("#"):
            lines.append("#" + line)
        else:
            lines.append(line)
    lines += [PHASE2_HEADER, f"API_KEY={key}"]
    return ("\n".join(lines) + "\n").encode()


def phase_blockers(phase: int, local: dict[str, Any], probe: dict[str, Any], route: dict[str, str]) -> list[str]:
    """Readiness of one phase's credential (pure): the plan's block is the only active one and Sage sends it."""
    blockers = []
    credentials = local.get("credentials") or []
    block = "OpenAI" if phase == 2 else "LiteLLM"
    if [c["name"] for c in credentials] != ["API_KEY"] or not str(credentials[0]["block"]).startswith(block):
        blockers.append(f"installed .env.local must hold exactly one active API_KEY, in the {block} block")
    if phase == 2:
        if local.get("endpoints"):
            blockers.append("an endpoint is set in the installed .env.local")
        if probe.get("endpoints_after_dotenv"):
            blockers.append("an endpoint is set where Sage reads it")
        models = {route["OPENAI_REASONING_MODEL"]: True, route["OPENAI_PLAIN_MODEL"]: True}
    else:
        if local.get("endpoints") != ["API_ENDPOINT"] or not local.get("api_endpoint_is_gateway"):
            blockers.append("the installed .env.local's API_ENDPOINT is not the route's gateway")
        if [name for name in probe.get("endpoints_after_dotenv") or [] if name != "API_ENDPOINT"]:
            blockers.append("an adapter endpoint variable is set where Sage reads it")
        models = {route["GATEWAY_MODEL"]: True}
    if probe.get("container_env"):
        blockers.append("the container environment sets a key or endpoint")
    if probe.get("source") != ".env.local:API_KEY":
        blockers.append("Sage would not load the key from the installed .env.local")
    if probe.get("status") != 200 or probe.get("models") != models or not probe.get("tls_verified"):
        blockers.append("keyed verified-TLS model listing did not return 200 with the phase's models")
    return blockers


# --------------------------------------------------------------------------------------------------
# Capture block and budget (pure; the original bytes are restored exactly)
# --------------------------------------------------------------------------------------------------

def env_block(container_dir: str, fifo_dir: str) -> str:
    return "\n".join([
        ENV_BEGIN,
        f"SAGE_OBSERVATION_DIRECTORY={fifo_dir}",
        f"SAGE_RESOURCE_BUDGET_CONFIG={container_dir}/budget.json",
        f"SAGE_RESOURCE_BUDGET_LEDGER={container_dir}/ledger.jsonl",
        f"SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION={TOKEN_RESERVATION}",
        f"SAGE_EVAL_MAX_OUTPUT_TOKENS={OUTPUT_TOKENS_PER_CALL}",
        ENV_END,
        "",
    ])


def insert_block(original: bytes, block: str) -> bytes:
    if ENV_BEGIN.encode() in original:
        raise SystemExit("the installed .env already carries a check block; run cleanup first")
    prefix = original if not original or original.endswith(b"\n") else original + b"\n"
    return prefix + block.encode()


def preempted_keys(local_env: bytes, block: str) -> list[str]:
    keys = {line.split("=", 1)[0] for line in block.splitlines() if "=" in line and not line.startswith("#")}
    found = []
    for line in local_env.decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in keys and value.strip():
            found.append(key.strip())
    return sorted(found)


# Sage meters its Mythic control plane on the same ledger from startup; that limit only has to be out of reach.
LEDGER_CALENDAR_SECONDS = 4 * 3600
CONTROL_REQUEST_LIMIT = 1_000_000


def budget_config() -> dict[str, Any]:
    return {"schema": "sage-shared-resource-budget-v1",
            "resources": {"provider_requests": {"limit": LIMITS["provider_calls"] + 1, "cleanup_reserve": 1},
                          "provider_tokens": {"limit": LIMITS["provider_tokens"] + 1, "cleanup_reserve": 1},
                          "control_requests": {"limit": CONTROL_REQUEST_LIMIT, "cleanup_reserve": 1000}},
            "calendar_seconds": LEDGER_CALENDAR_SECONDS,
            "cleanup_seconds": CLEANUP_SECONDS}


# --------------------------------------------------------------------------------------------------
# Wire evidence (pure, tested offline)
# --------------------------------------------------------------------------------------------------

def _body(value: Any) -> Any:
    """A captured body: JSON, or a server-sent event stream reassembled into the JSON it streams."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return _from_events(raw)


def _from_events(raw: str) -> dict[str, Any] | None:
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            try:
                events.append(json.loads(line[5:].strip()))
            except ValueError:
                return None
    completed = [e["response"] for e in events if isinstance(e.get("response"), dict)
                 and e.get("type") == "response.completed"]
    if completed:
        return completed[-1]
    chunks = [e for e in events if e.get("object") == "chat.completion.chunk"]
    if not chunks:
        return None
    text, calls, usage, model = [], {}, None, None
    for chunk in chunks:
        model = chunk.get("model") or model
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text.append(str(delta.get("content") or ""))
            for call in delta.get("tool_calls") or []:
                entry = calls.setdefault(call.get("index", 0), {"id": "", "type": "function",
                                                                "function": {"name": "", "arguments": ""}})
                entry["id"] = call.get("id") or entry["id"]
                function = call.get("function") or {}
                entry["function"]["name"] += str(function.get("name") or "")
                entry["function"]["arguments"] += str(function.get("arguments") or "")
    message = {"role": "assistant", "content": "".join(text) or None,
               "tool_calls": [calls[i] for i in sorted(calls)] or None}
    return {"object": "chat.completion", "model": model, "choices": [{"index": 0, "message": message}],
            "usage": usage, "streamed": True}


def exchanges(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each captured model request with its response, in request order, with the fields the rules read."""
    responses: dict[Any, dict[str, Any]] = {}
    requests = []
    for record in records:
        payload = (record.get("record") or {}).get("payload") or {}
        if record.get("kind") == "model_request":
            requests.append((payload, int((record.get("record") or {}).get("time_ns") or 0)))
        elif record.get("kind") == "model_response":
            responses[payload.get("request_id")] = payload
    out = []
    for payload, sent_ns in requests:
        body = _body(payload.get("body_base64")) or {}
        url = str(payload.get("url") or "")
        path = urlsplit(url).path.rstrip("/")
        api = ("responses" if path.endswith("/responses")
               else "chat_completions" if path.endswith("/chat/completions") else "other")
        response = responses.get(payload.get("request_id")) or {}
        reply = _body(response.get("body_base64"))
        out.append({
            "request_id": payload.get("request_id"), "time_ns": sent_ns,
            "url": url, "api": api, "model": body.get("model"), "fields": sorted(k for k in RESPONSES_FIELDS
                                                                              if k in body),
            "store": body.get("store"), "include": body.get("include"), "access_programs": body.get("access_programs"),
            "input": body.get("input") if api == "responses" else body.get("messages"),
            "status": response.get("status_code"), "outcome": response.get("outcome"),
            "reply": reply if isinstance(reply, dict) else {},
        })
    return out


def _control(record: dict[str, Any], routing_key: str) -> dict[str, Any] | None:
    payload = (record.get("record") or {}).get("payload") or {}
    if record.get("kind") == "control_request" and payload.get("routing_key") == routing_key:
        return payload
    return None


def sage_answered_on_start(records: list[dict[str, Any]]) -> bool:
    """Whether Sage answered Mythic's on-start after its latest process start (Sage's own clock and capture)."""
    starts = [int(r["record"]["time_ns"]) for r in records if r.get("kind") == "process_start"]
    if not starts:
        return False
    latest = max(starts)
    return any(_control(r, ON_START_RESPONSE) is not None and int(r["record"]["time_ns"]) > latest for r in records)


def step_completions(records: list[dict[str, Any]]) -> dict[int, int]:
    """Mythic chat request id -> time of Sage's last captured complete chat_response for it."""
    ends: dict[int, int] = {}
    for record in records:
        payload = _control(record, CHAT_RESPONSE)
        body = _body(payload.get("body_base64")) if payload else None
        if isinstance(body, dict) and body.get("status") == "complete" and type(body.get("request_id")) is int:
            ends[body["request_id"]] = max(ends.get(body["request_id"], 0), int(record["record"]["time_ns"]))
    return ends


def attribute(records: list[dict[str, Any]], steps: dict[str, dict[str, Any]],
              ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Each captured model request goes to the step in flight when Sage sent it.

    A step is in flight from its send until Sage's last captured complete answer to its Mythic request (or, with
    none captured, until the next step's send). Sage answers a channel's requests in order, so a request sent while
    two steps are in flight belongs to the earlier one.
    """
    ends = step_completions(records)
    order = sorted(steps.items(), key=lambda kv: int(kv[1]["sent_ns"]))
    windows = []
    for index, (step, info) in enumerate(order):
        end = ends.get(int(info["request_id"]))
        if end is None:
            end = int(order[index + 1][1]["sent_ns"]) - 1 if index + 1 < len(order) else math.inf
        windows.append((step, int(info["sent_ns"]), end))
    by_step: dict[str, list[dict[str, Any]]] = {step: [] for step in steps}
    unattributed = []
    for item in exchanges(records):
        owner = next((step for step, start, end in windows if start <= item["time_ns"] <= end), None)
        (by_step[owner] if owner is not None else unattributed).append(item)
    return by_step, unattributed


def binding_problems(binding: dict[str, Any] | None, operation: dict[str, Any]) -> list[str]:
    """P9 for one channel: its backing token is the operation bot's, live, in this operation, with the scopes."""
    token = (binding or {}).get("token") or {}
    if not token:
        return ["no binding row"]
    owner = (token.get("operator") or {})
    scopes = {str(scope) for scope in token.get("scopes") or []}
    problems = []
    if owner.get("username") != _runner().operation_bot_name(str(operation.get("name") or "")):
        problems.append("owner is not the operation's bot")
    if token.get("active") is not True or token.get("deleted") is not False:
        problems.append("token not active")
    if token.get("operation_id") != operation.get("id"):
        problems.append("token in another operation")
    if not _runner().CHANNEL_TOKEN_SCOPES <= scopes:
        problems.append("missing channel scopes")
    if "*" in scopes:
        problems.append("wildcard scope")
    return problems


async def default_channel_path(client: Any) -> dict[str, Any]:
    """What the runner's default channel path would bind, from Mythic rows (ids and scope names only)."""
    nc = _runner()
    explicit = [name for name in EXPLICIT_BINDING if os.environ.get(name)]
    operations = (await nc.mythic.execute_custom_query(client, LIVE_OPERATIONS_QUERY)).get("operation") or []
    observed = await nc.mythic.execute_custom_query(
        client, nc.OPERATION_BOT_QUERY, variables={"operationId": int(client.current_operation_id)})
    active = sorted(str(row.get("username")) for row in observed.get("operator") or []
                    if row.get("account_type") == "bot" and row.get("active") is True and row.get("deleted") is False)
    state = {"explicit_binding": explicit, "live_operations": len(operations), "active_bots": active,
             "operation_bot": None, "token": None, "error": ""}
    try:
        bot = nc.select_operation_bot(observed)
    except RuntimeError as error:
        state["error"] = str(error)
        return state
    state["operation_bot"] = bot["username"]
    tokens = (await nc.mythic.execute_custom_query(client, nc.READINESS_QUERY)).get("apitokens") or []
    usable = nc.usable_bot_tokens(tokens, operator_id=int(bot["id"]), operation_id=int(client.current_operation_id))
    state["token"] = ({"id": usable[0]["id"], "scopes": sorted(nc._scopes(usable[0]))} if usable
                      else "created at the first channel")
    return state


def default_path_blockers(state: dict[str, Any]) -> list[str]:
    blockers = []
    if state.get("explicit_binding"):
        blockers.append(f"an explicit channel binding is set ({', '.join(state['explicit_binding'])}); "
                        "the check uses the default path")
    if state.get("live_operations") != 1:
        blockers.append(f"Mythic has {state.get('live_operations')} live operations; the send gate needs exactly one")
    if not state.get("operation_bot"):
        blockers.append("the default channel path cannot pick the operation bot")
    return blockers


def produced_calls(item: dict[str, Any]) -> list[dict[str, str]]:
    """Function calls a provider response produced: ``{"call_id", "name", "arguments"}``."""
    reply = item["reply"]
    if item["api"] == "responses":
        return [{"call_id": str(o.get("call_id") or ""), "name": str(o.get("name") or ""),
                 "arguments": str(o.get("arguments") or "")}
                for o in reply.get("output") or [] if isinstance(o, dict) and o.get("type") == "function_call"]
    calls = []
    for choice in reply.get("choices") or []:
        for call in (choice.get("message") or {}).get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append({"call_id": str(call.get("id") or ""), "name": str(function.get("name") or ""),
                          "arguments": str(function.get("arguments") or "")})
    return calls


def replayed(item: dict[str, Any]) -> dict[str, Any]:
    """What a request's history carries: call id -> (name, arguments), call id -> result, reasoning items."""
    calls: dict[str, dict[str, str]] = {}
    results: dict[str, str] = {}
    reasoning: list[tuple[str, str]] = []
    for entry in item["input"] if isinstance(item["input"], list) else []:
        if not isinstance(entry, dict):
            continue
        if item["api"] == "responses":
            kind = entry.get("type")
            if kind == "function_call":
                calls[str(entry.get("call_id") or "")] = {"name": str(entry.get("name") or ""),
                                                          "arguments": str(entry.get("arguments") or "")}
            elif kind == "function_call_output":
                results[str(entry.get("call_id") or "")] = str(entry.get("output") or "")
            elif kind == "reasoning":
                reasoning.append((str(entry.get("id") or ""), str(entry.get("encrypted_content") or "")))
        else:
            for call in entry.get("tool_calls") or []:
                function = call.get("function") or {}
                calls[str(call.get("id") or "")] = {"name": str(function.get("name") or ""),
                                                    "arguments": str(function.get("arguments") or "")}
            if entry.get("role") == "tool":
                results[str(entry.get("tool_call_id") or "")] = str(entry.get("content") or "")
    return {"calls": calls, "results": results, "reasoning": reasoning}


def reasoning_items(item: dict[str, Any]) -> list[tuple[str, str]]:
    return [(str(o.get("id") or ""), str(o.get("encrypted_content") or ""))
            for o in item["reply"].get("output") or []
            if isinstance(o, dict) and o.get("type") == "reasoning" and o.get("encrypted_content")]


def usage_tokens(item: dict[str, Any]) -> int:
    total = (item["reply"].get("usage") or {}).get("total_tokens")
    return int(total) if isinstance(total, int) else 0


def native_answers(rows: dict[str, Any]) -> list[str]:
    texts = [str(m.get("message") or "").strip() for m in rows.get("messages", [])
             if "tool_use" not in (m.get("metadata") or {})]
    return [text for text in texts if text and not text.startswith(NOTICE_PREFIXES)]


def round_trip(found: list[dict[str, Any]]) -> dict[str, Any]:
    """A produced call whose id comes back as Sage's tool result in a later request of the same step."""
    ok = [item for item in found if item["status"] and 200 <= int(item["status"]) < 300]
    produced = [(index, call) for index, item in enumerate(ok) for call in produced_calls(item) if call["call_id"]]
    consumed = [(index, call) for index, call in produced
                if any(call["call_id"] in replayed(later)["results"] for later in ok[index + 1:])]
    return {"calls": [call["call_id"] for _index, call in produced],
            "consumed": [call["call_id"] for _index, call in consumed]}


def carried(earlier: list[dict[str, Any]], calls: list[dict[str, str]], first: dict[str, Any]) -> dict[str, int]:
    """How many of an earlier step's calls a later step's first request replays, by id: each call with its name and
    a result (a finish call's acknowledgement first appears here), equal to the result the earlier step sent."""
    sent: dict[str, str] = {}
    for item in earlier:
        sent.update(replayed(item)["results"])
    return {
        "calls_replayed": sum(1 for call in calls if first["calls"].get(call["call_id"], {}).get("name") == call["name"]),
        "results_replayed": sum(1 for call in calls if first["results"].get(call["call_id"])
                                and first["results"][call["call_id"]] == sent.get(call["call_id"],
                                                                                  first["results"][call["call_id"]])),
    }


def settings_error(subscriptions: Any) -> str:
    """P7: ``""`` when Sage and Sage Watcher list the six providers and OpenAI API keeps its three choices."""
    models = []
    for item in subscriptions or []:
        try:
            models.append(json.loads(item) if isinstance(item, str) else item)
        except ValueError:
            continue
    by_name = {m.get("name"): m for m in models if isinstance(m, dict)}
    for name, option_name in (("Sage", "provider"), ("Sage Watcher", "SAGE_WATCHER_PROVIDER")):
        options = {o.get("name"): o for o in ((by_name.get(name) or {}).get("metadata") or {})
                   .get("configuration_options") or []}
        if option_name not in options:
            return f"{name} has no {option_name} setting"
        found = [[c.get("label"), c.get("value")] for c in options[option_name].get("choices") or []]
        if found != EXPECTED_PROVIDERS:
            return f"{name} provider choices are {found}"
        if name == "Sage":
            api = [[c.get("label"), c.get("value")] for c in (options.get("SAGE_OPENAI_API") or {}).get("choices")
                   or []]
            if api != EXPECTED_API_CHOICES:
                return f"OpenAI API choices are {api}"
    return ""


def evaluate(run: dict[str, Any], wire: dict[str, list[dict[str, Any]]], rows: dict[str, dict[str, Any]],
             totals: dict[str, Any], route: dict[str, str], stored_provider: dict[str, str],
             bindings: dict[str, dict[str, Any]], operation: dict[str, Any]) -> dict[str, Any]:
    """P1-P9 of the plan, each with its numbers, from the captured wire, Mythic rows and run records."""
    steps = run.get("steps") or {}
    all_steps = [step for step, *_ in STEPS]
    detail: dict[str, Any] = {}
    stopped = str((run.get("stopped") or {}).get("reason") or "")
    if stopped.startswith("phase ") and stopped.split(" ")[1] in ("1", "2", "3"):
        return {"verdict": "not_run", "reason": f"{stopped} (container readiness)", "criteria": {},
                "detail": {"recreations": [{k: r.get(k) for k in ("phase", "returncode", "process_start",
                                                                 "blockers")} for r in run.get("recreations") or []]}}
    complete = [step for step in all_steps if step in steps] == all_steps
    channel_of = {step: channel for step, _phase, channel, _prompt in STEPS}

    def answered(step: str) -> bool:
        info = rows.get(step) or {}
        return info.get("status") == "complete" and bool(native_answers(info))

    # P1 route
    p1 = {}
    for step in all_steps:
        found, channel = wire.get(step, []), channel_of[step]
        kind = CHANNELS[channel][2]
        problems = []
        if not found:
            problems.append("no captured request")
        for item in found:
            if item["url"].rstrip("/") != expected_url(route, channel):
                problems.append(f"url {item['api']}")
            if item["model"] != route[CHANNELS[channel][1]]:
                problems.append("model")
            if not (item["status"] and 200 <= int(item["status"]) < 300):
                problems.append(f"status {item['status']}")
            if kind == "openai":
                if (item["store"] is not False or REASONING_INCLUDE not in (item["include"] or [])
                        or item["access_programs"] != {"cyber": "standard"}):
                    problems.append("responses projection")
            elif item["fields"]:
                problems.append(f"responses fields on chat completions: {item['fields']}")
        p1[step] = problems or "ok"
    detail["P1_route"] = p1
    # A gate step on its correct route that made no tool call never exercised the round trip: not an attempt.
    # A gate step on the wrong route is a failure of the change, whatever it called.
    for gate in ("L1", "O1"):
        if gate in steps and p1.get(gate) == "ok" and not round_trip(wire.get(gate, []))["calls"]:
            return {"verdict": "not_run", "reason": f"{gate} made no tool call, so the check did not run",
                    "criteria": {}, "detail": {gate: round_trip(wire.get(gate, [])), "P1_route": p1}}

    # P2 round trip in O1 and L1
    p2 = {step: {**round_trip(wire.get(step, [])), "answered": answered(step)} for step in ("O1", "L1")}
    detail["P2_round_trip"] = p2

    # P3 stateless replay: O1's reasoning and calls reach O2's first request with matching ids
    o1, o2 = wire.get("O1", []), wire.get("O2", [])
    o1_reasoning = [pair for item in o1 for pair in reasoning_items(item)]
    o1_calls = [call for item in o1 for call in produced_calls(item)]
    first_o2 = replayed(o2[0]) if o2 else {"calls": {}, "results": {}, "reasoning": []}
    p3 = {
        "o1_reasoning_items": len(o1_reasoning),
        "o1_calls": len(o1_calls),
        **carried(o1, o1_calls, first_o2),
        "reasoning_replayed": sum(1 for pair in o1_reasoning if pair in first_o2["reasoning"]),
    }
    detail["P3_stateless_replay"] = p3

    # P6 restart: channel L's stored provider, L1's call and result in L2's first request
    l1_calls = [call for item in wire.get("L1", []) for call in produced_calls(item)]
    l2 = wire.get("L2", [])
    first_l2 = replayed(l2[0]) if l2 else {"calls": {}, "results": {}, "reasoning": []}
    recreations = [r for r in run.get("recreations") or [] if r.get("returncode") == 0 and r.get("process_start")]
    p6 = {"recreations": len(recreations), "stored_provider": stored_provider.get("L"),
          "l1_calls": len(l1_calls), **carried(wire.get("L1", []), l1_calls, first_l2),
          "answered": answered("L2")}
    detail["P6_restart"] = p6

    # P8 containment
    tasks = {step: (info or {}).get("tasks_after") for step, info in steps.items()}
    files = run.get("files_after_cleanup") or {}
    p8 = {"target_tasks": sum(1 for value in tasks.values() if value != run.get("task_baseline")),
          "channels": len(run.get("channels") or {}), "requests": len(steps),
          "provider_calls": totals.get("provider_calls"), "provider_tokens": totals.get("provider_tokens"),
          "unpaired": totals.get("unpaired"), "active_seconds": totals.get("active_seconds"),
          "recreations": len(run.get("recreations") or []),
          "env_local_restored": files.get("env_local_restored"), "env_restored": files.get("env_restored"),
          "stopped": run.get("stopped")}
    detail["P8_containment"] = p8
    detail["P7_settings"] = run.get("settings_error", "not recorded")

    # P9 channel binding: each run channel's backing token, from Mythic's rows
    p9 = {channel: binding_problems(bindings.get(channel), operation) or "ok" for channel in run.get("channels") or {}}
    detail["P9_channel_binding"] = p9

    criteria = {
        "P1_route": complete and all(value == "ok" for value in p1.values()),
        "P2_round_trip": all(v["consumed"] and v["answered"] for v in p2.values()),
        "P3_stateless_replay": (p3["o1_reasoning_items"] >= 1 and p3["o1_calls"] >= 1
                                and p3["calls_replayed"] == p3["results_replayed"] == p3["o1_calls"]
                                and p3["reasoning_replayed"] == p3["o1_reasoning_items"]),
        "P4_non_reasoning_model": answered("G1") and p1.get("G1") == "ok",
        "P5_migration": answered("M1") and p1.get("M1") == "ok",
        "P6_restart": (p6["recreations"] == 2 and p6["stored_provider"] == "litellm" and p6["l1_calls"] >= 1
                       and p6["calls_replayed"] == p6["results_replayed"] == p6["l1_calls"] and p6["answered"]),
        "P7_settings": run.get("settings_error") == "",
        "P8_containment": (complete and not run.get("stopped") and p8["target_tasks"] == 0
                           and p8["channels"] <= LIMITS["channels"] and p8["requests"] <= LIMITS["requests"]
                           and isinstance(p8["provider_calls"], int)
                           and p8["provider_calls"] <= LIMITS["provider_calls"]
                           and isinstance(p8["provider_tokens"], int)
                           and p8["provider_tokens"] <= LIMITS["provider_tokens"] and p8["unpaired"] == 0
                           and isinstance(p8["active_seconds"], (int, float))
                           and p8["active_seconds"] <= LIMITS["active_seconds"]
                           and p8["recreations"] == LIMITS["recreations"]
                           and p8["env_local_restored"] is True and p8["env_restored"] is True),
        "P9_channel_binding": bool(p9) and all(value == "ok" for value in p9.values()),
    }
    return {"criteria": criteria, "detail": detail, "verdict": "pass" if all(criteria.values()) else "fail"}


# --------------------------------------------------------------------------------------------------
# Receiver process (host side; Sage in the container runs as the same uid over the bind mount)
# --------------------------------------------------------------------------------------------------

def _serve(record: Path, parent: Path, ready: Path) -> int:
    receiver_module = _module("sage_provider_split_receiver", _RECEIVER_PATH)
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


def _process_started_after(record: Path, after_ns: int) -> bool:
    return any(r.get("kind") == "process_start" and int((r.get("record") or {}).get("time_ns") or 0) >= after_ns
               for r in read_wire(record))


# --------------------------------------------------------------------------------------------------
# Deployment, container and provider probes
# --------------------------------------------------------------------------------------------------

def _base():
    return _module("sage_provider_split_base", _BASE_PATH)


@functools.lru_cache(maxsize=1)
def _runner():
    """The live runner module (one owner of the bot-name rule and the channel scope set)."""
    return _base().nc


def _deployment():
    return _module("sage_provider_split_deployment", _DEPLOYMENT_PATH)


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


# Runs inside the Sage container with Sage's own dotenv and TLS bootstrap; prints names, booleans and status only.
_CONTAINER_PROBE = r"""
import json, os, ssl, sys
sys.path.insert(0, "/Mythic")
from dotenv import dotenv_values
import dotenv_bootstrap as boot
CREDENTIALS = ("API_KEY", "OPENAI_API_KEY")
ENDPOINTS = ("API_ENDPOINT", "OPENAI_API_BASE", "OPENAI_BASE_URL", "LANGSMITH_GATEWAY")
out = {"container_env": sorted(n for n in CREDENTIALS + ENDPOINTS if os.environ.get(n)), "source": "",
       "status": None, "models": {}, "tls_verified": False}
environ = dict(os.environ)
if environ.get("API_KEY"):
    out["source"] = "container-env:API_KEY"
for path in boot.dotenv_paths("/Mythic"):
    if "API_KEY" in boot.apply_dotenv(dotenv_values(path), environ) and not out["source"]:
        out["source"] = os.path.basename(path) + ":API_KEY"
out["endpoints_after_dotenv"] = sorted(n for n in ENDPOINTS if environ.get(n))
key = environ.get("API_KEY", "")
if key:
    import httpx
    from ssl_bootstrap import configure_sage_ssl_cert_file
    bundle = configure_sage_ssl_cert_file()
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    try:
        response = httpx.get(sys.argv[1].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key},
                             timeout=20, verify=context)
        out["status"] = response.status_code
        listed = {row.get("id") for row in (response.json().get("data") or [])} if response.status_code == 200 else set()
        out["models"] = {model: model in listed for model in sys.argv[2:]}
        out["tls_verified"], out["custom_bundle"] = True, bool(bundle)
    except Exception as error:
        out["error"] = type(error).__name__
print(json.dumps(out))
"""


def _phase_models(phase: int, route: dict[str, str]) -> tuple[str, list[str]]:
    if PHASE_ROUTE[phase] == "openai":
        return OPENAI_ENDPOINT, [route["OPENAI_REASONING_MODEL"], route["OPENAI_PLAIN_MODEL"]]
    return route["GATEWAY_ENDPOINT"], [route["GATEWAY_MODEL"]]


def _container_probe(phase: int, route: dict[str, str]) -> dict[str, Any]:
    """Keyed, non-inference ``GET /models`` from the deployed container with the key Sage itself would load."""
    endpoint, models = _phase_models(phase, route)
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", _CONTAINER_PROBE,
                                endpoint, *models], capture_output=True, text=True, timeout=90)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


def _host_openai_probe(route: dict[str, str]) -> dict[str, Any]:
    """Phase 2's key from the repo ``.env.local``: keyed, verified-TLS ``GET /models`` from this host."""
    sys.path.insert(0, str(RUNTIME))
    import httpx

    from ssl_bootstrap import configure_sage_ssl_cert_file

    try:
        key = openai_block_key((RUNTIME / ".env.local").read_text())
    except (OSError, SystemExit) as error:
        return {"error": str(error) if isinstance(error, SystemExit) else type(error).__name__}
    bundle = configure_sage_ssl_cert_file()
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    models = [route["OPENAI_REASONING_MODEL"], route["OPENAI_PLAIN_MODEL"]]
    try:
        response = httpx.get(OPENAI_ENDPOINT + "/models", headers={"Authorization": "Bearer " + key}, timeout=20,
                             verify=context)
    except Exception as error:
        return {"error": type(error).__name__}
    listed = {row.get("id") for row in (response.json().get("data") or [])} if response.status_code == 200 else set()
    return {"status": response.status_code, "models": {m: m in listed for m in models}, "tls_verified": True}


async def _operator_secret_names(client: Any) -> dict[str, Any]:
    """The run's Mythic operator (the channel creator and requester) and its user-secret names; never values."""
    try:
        switch = _module("sage_provider_split_model_switch", _MODEL_SWITCH_PATH)
        operator_id = await switch._operator_id(client)
        result = (await _base()._query(client, OPERATOR_SECRETS_QUERY, operatorId=operator_id)).get(
            "getOperatorSecrets") or {}
    except (Exception, SystemExit) as error:
        return {"resolved": False, "error": type(error).__name__}
    if result.get("status") != "success" or not isinstance(result.get("secrets") or {}, dict):
        return {"resolved": False, "operator": operator_id, "error": "secrets not readable"}
    return {"resolved": True, "operator": operator_id, "names": sorted((result.get("secrets") or {}).keys())}


def watcher_state(database: Path) -> dict[str, Any]:
    """Each operation's Watcher profile state from the deployed Sage database, read in place (WAL-aware)."""
    if not database.is_file():
        return {"readable": False}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = connection.execute("select operation_id, lifecycle_state, paused from watcher_profiles").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        return {"readable": False, "error": type(error).__name__}
    profiles = [{"operation": str(op), "state": str(state), "paused": bool(paused)} for op, state, paused in rows]
    return {"readable": True, "profiles": profiles,
            "active": [p["operation"] for p in profiles if not p["paused"] and p["state"] != "paused"]}


async def _subscriptions(client: Any) -> Any:
    base = _base()
    rows = (await base._query(client, base.CONTAINER_QUERY)).get("consuming_container") or []
    return rows[0].get("subscriptions") if rows else None


def _capture_state(run: dict[str, Any]) -> dict[str, Any]:
    capture = run.get("capture") or {}
    if not capture:
        return {"armed": False}
    parent = Path(capture["host_dir"])
    ledger = parent / "ledger.jsonl"
    lines = ledger.read_text().splitlines() if ledger.exists() else []
    reservations = sum(1 for line in lines if '"provider_requests"' in line and '"reservation"' in line)
    return {"armed": True, "receiver_alive": _pid_alive(int(capture["receiver_pid"])),
            "receiver_failed": (parent / "receiver-failed").exists(),
            "process_start_after_arm": _process_started_after(Path(capture["record"]), int(capture["armed_ns"])),
            "budget_valid": (parent / "budget.json").exists(), "provider_reservations": reservations,
            "env_block_present": ENV_BEGIN.encode() in (Path(capture["installed"]) / ".env").read_bytes()}


# --------------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------------

async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    route = read_route(Path(args.route_file))
    installed = Path(_deployment()._installed_service_dir())
    readiness = await base._query(client, base.nc.READINESS_QUERY)
    containers = readiness.get("consuming_container") or []
    parity = _run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container", "parity"])
    check = _run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container", "check"])
    local_path = installed / ".env.local"
    local_text = local_path.read_text() if local_path.exists() else ""
    phase1_local = dotenv_state(local_text, route["GATEWAY_ENDPOINT"])
    if run.get("capture"):  # once armed, the installed .env.local is the one saved at arm (phase 1)
        phase1_local = dotenv_state(Path(run["capture"]["local_backup"]).read_text(), route["GATEWAY_ENDPOINT"])
    report = {
        "route": {"gateway_model": route["GATEWAY_MODEL"],
                  "openai_models": [route["OPENAI_REASONING_MODEL"], route["OPENAI_PLAIN_MODEL"]]},
        "deployment_check": {"returncode": check["returncode"], "tail": check["tail"]},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "identity": _identity(),
        "container_adapter": _container_adapters(),
        "phoenix_readable": bool(args.phoenix_db) and Path(args.phoenix_db).is_file(),
        "task_baseline": await base._max_task(client),
        "watcher": watcher_state(installed / "sage.db"),
        "settings_error": settings_error(await _subscriptions(client)),
        "credentials": {
            "phase1_installed_local": phase1_local,
            "installed_env": dotenv_state((installed / ".env").read_text() if (installed / ".env").exists() else ""),
            "phase1_container": _container_probe(1, route),
            "phase2_host": _host_openai_probe(route),
            "operator_secrets": await _operator_secret_names(client),
            "channel_config_sets_key_or_endpoint": any(
                name in channel_config(route, channel)["config"] for channel in CHANNELS
                for name in (*CREDENTIAL_NAMES, *ENDPOINT_NAMES, "SAGE_OPENAI_API")),
        },
        "capture": _capture_state(run),
        "default_channel_path": await default_channel_path(client),
        "limits": LIMITS, "token_reservation": TOKEN_RESERVATION,
        "check_sha256": _sha256_bytes(Path(__file__).read_bytes()),
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
    if report["container_adapter"] != EXPECTED_ADAPTER:
        blockers.append("container adapter versions")
    if not report["phoenix_readable"]:
        blockers.append("Phoenix unreadable")
    watcher = report["watcher"]
    if not watcher.get("readable") or watcher.get("active"):
        blockers.append("the Sage Watcher is not paused or absent (pause it from its owner channel)")
    credentials = report["credentials"]
    blockers += phase_blockers(1, credentials["phase1_installed_local"], credentials["phase1_container"], route)
    if credentials["installed_env"]["credentials"] or credentials["installed_env"]["endpoints"]:
        blockers.append("the installed .env sets a key or endpoint")
    phase2 = credentials["phase2_host"]
    if phase2.get("status") != 200 or phase2.get("models") != {m: True for m in report["route"]["openai_models"]}:
        blockers.append("the OpenAI block's key did not list both OpenAI models")
    secrets = credentials["operator_secrets"]
    if not secrets.get("resolved"):
        blockers.append("the run operator's user secrets could not be resolved")
    elif set(secrets.get("names") or []) & set(SHADOWING_SECRETS):
        blockers.append("a user secret would shadow the installed provider settings")
    if credentials["channel_config_sets_key_or_endpoint"]:
        blockers.append("a channel configuration sets a key, endpoint or OpenAI API")
    blockers += default_path_blockers(report["default_channel_path"])
    capture = report["capture"]
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


async def cmd_arm(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("capture"):
        raise SystemExit("already armed; run cleanup first")
    if (run.get("readiness") or {}).get("verdict") != "capture_required":
        raise SystemExit("readiness must report capture_required (feature deployed, parity passing) before arming")
    installed = Path(_deployment()._installed_service_dir())
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    host_dir = _private_dir(installed / f".provider-split-check-{stamp}")
    container_dir = f"/Mythic/{host_dir.name}"
    evidence = evidence_dir(run)
    record, ready = evidence / "wire.jsonl", host_dir / "receiver-ready.json"
    (host_dir / "budget.json").write_text(json.dumps(budget_config(), sort_keys=True))
    os.chmod(host_dir / "budget.json", 0o600)
    env_path, local_path = installed / ".env", installed / ".env.local"
    original_env, original_local = env_path.read_bytes(), local_path.read_bytes()
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
    fifo_container = f"{container_dir}/{Path(json.loads(ready.read_text())['directory']).name}"
    block = env_block(container_dir, fifo_container)
    preempted = preempted_keys(original_local, block)
    if preempted:
        (host_dir / "stop").write_text("")
        raise SystemExit(f"the installed .env.local already sets {', '.join(preempted)}; it would override the check")
    backups = {}
    for name, data in (("env", original_env), ("local", original_local)):
        backup = evidence / f"installed-{name}.original"
        backup.write_bytes(data)
        os.chmod(backup, 0o600)
        backups[name] = str(backup)
    armed_ns = time.time_ns()
    run["capture"] = {"host_dir": str(host_dir), "container_dir": container_dir, "record": str(record),
                      "receiver_pid": receiver.pid, "installed": str(installed),
                      "env_backup": backups["env"], "env_original_sha256": _sha256_bytes(original_env),
                      "local_backup": backups["local"], "local_original_sha256": _sha256_bytes(original_local),
                      "local_mode": stat.S_IMODE(local_path.stat().st_mode), "armed_ns": armed_ns,
                      "authorized_by": args.authorized_by}
    env_path.write_bytes(insert_block(original_env, block))
    started = _deployment()._start_container()
    run["capture"]["start"] = {"returncode": started.get("returncode")}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not _process_started_after(record, armed_ns):
        time.sleep(2)
    return {"armed": True, "start_returncode": started.get("returncode"), "capture": _capture_state(run)}


def _restore_local(capture: dict[str, Any]) -> bool:
    """Put the installed ``.env.local`` back to the bytes saved at arm; True when it matches them."""
    path = Path(capture["installed"]) / ".env.local"
    original = Path(capture["local_backup"]).read_bytes()
    if _sha256_bytes(original) != capture["local_original_sha256"]:
        raise SystemExit("the saved .env.local backup does not match its recorded hash; restore it by hand")
    if path.read_bytes() != original:
        path.write_bytes(original)
        os.chmod(path, int(capture.get("local_mode", 0o600)))
    return _sha256_bytes(path.read_bytes()) == capture["local_original_sha256"]


async def _switch_phase(run: dict[str, Any], phase: int, route: dict[str, str]) -> str:
    """Write the phase's installed ``.env.local``, recreate the container and prove the phase's key from inside."""
    capture = run["capture"]
    path = Path(capture["installed"]) / ".env.local"
    if phase == 2:
        key = openai_block_key((RUNTIME / ".env.local").read_text())
        path.write_bytes(phase2_local(Path(capture["local_backup"]).read_bytes(), key))
        os.chmod(path, int(capture.get("local_mode", 0o600)))
    else:
        _restore_local(capture)
    switched_ns = time.time_ns()
    started = _deployment()._start_container()
    record = Path(capture["record"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not _process_started_after(record, switched_ns):
        time.sleep(2)
    probe = _container_probe(phase, route)
    local = dotenv_state(path.read_text(), route["GATEWAY_ENDPOINT"])
    blockers = phase_blockers(phase, local, probe, route)
    run.setdefault("recreations", []).append({
        "phase": phase, "returncode": started.get("returncode"),
        "process_start": _process_started_after(record, switched_ns), "probe": probe, "local": local,
        "blockers": blockers})
    if started.get("returncode") != 0 or not run["recreations"][-1]["process_start"]:
        return f"phase {phase} container did not restart"
    return f"phase {phase} credential: {'; '.join(blockers)}" if blockers else ""


async def await_sage_ready(client: Any, base: Any, record: Path, *, phase: int, timeout: float = GATE_SECONDS,
                           poll: float = 1.0) -> str:
    """Hold a step until Mythic shows the Sage container running and Sage has answered Mythic's on-start."""
    deadline = time.monotonic() + timeout
    while True:
        rows = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
        running = bool(rows and rows[0].get("container_running"))
        answered = sage_answered_on_start(read_wire(record))
        if running and answered:
            return ""
        if time.monotonic() >= deadline:
            reason = ("Mythic does not show the Sage container running" if not running
                      else "Sage has not answered Mythic's on-start")
            return f"phase {phase} container not ready: {reason}"
        await asyncio.sleep(poll)


async def cmd_cleanup(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    actions: dict[str, Any] = {}
    channel_ids = {int(channel_id) for channel_id in (run.get("channels") or {}).values()}
    for row in (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or []:
        if int(row.get("channel_id") or 0) in channel_ids:
            await base.nc.mythic.execute_custom_query(client, base.nc.CANCEL_REQUEST_MUTATION,
                                                      variables={"request_id": int(row["id"])})
            actions.setdefault("cancelled", []).append(int(row["id"]))
    capture = run.get("capture") or {}
    if capture:
        env_path = Path(capture["installed"]) / ".env"
        original = Path(capture["env_backup"]).read_bytes()
        if _sha256_bytes(original) != capture["env_original_sha256"]:
            raise SystemExit("the saved .env backup does not match its recorded hash; restore it by hand")
        if env_path.read_bytes() != original:
            env_path.write_bytes(original)
        actions["env_local_restored"] = _restore_local(capture)
        actions["env_restored"] = _sha256_bytes(env_path.read_bytes()) == capture["env_original_sha256"]
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
        run["files_after_cleanup"] = {"env_local_restored": actions["env_local_restored"],
                                      "env_restored": actions["env_restored"]}
    actions["open_requests_after"] = (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or []
    run["cleanup"] = {**actions, "at": time.time()}
    if capture and actions.get("env_restored") and actions.get("env_local_restored") and actions.get(
            "receiver_stopped"):
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
               steps={}, channels={}, recreations=[],
               route_summary={k: route[k] for k in ("GATEWAY_ENDPOINT", *EXPECTED_ROUTE)},
               settings_error=settings_error(await _subscriptions(client)))
    started = time.monotonic()
    phase = 1
    try:
        for step, step_phase, channel, prompt in STEPS:
            if step_phase != phase:
                failure = await _switch_phase(run, step_phase, route)
                phase = step_phase
                if failure:
                    run["stopped"] = {"step": step, "reason": failure}
                    break
            failure = await await_sage_ready(client, base, record, phase=step_phase)
            if failure:
                run["stopped"] = {"step": step, "reason": failure}
                break
            if channel not in run["channels"]:
                created = await base.nc.create_locked_channel(
                    client, name=f"Provider split live check {channel}",
                    description="Sage OpenAI provider split live check", metadata=channel_config(route, channel))
                run["channels"][channel] = int(created["chat_channel_id"])
            mark = base.phoenix_mark(phoenix)
            sent_ns = time.time_ns()
            message = await base.nc.create_message(client, run["channels"][channel], prompt)
            request_id = int(message["chat_request_id"])
            await base.nc.wait_for_request(client, request_id, timeout_seconds=STEP_TIMEOUT)
            run["steps"][step] = {"request_id": request_id, "phase": step_phase, "sent_ns": sent_ns,
                                  "phoenix_after": mark, "phoenix_until": base.phoenix_mark(phoenix),
                                  "tasks_after": await base._max_task(client)}
            records = read_wire(record)
            items = attribute(records, run["steps"])[0][step]
            everything = exchanges(records)
            stop = ""
            if run["steps"][step]["tasks_after"] != run["task_baseline"]:
                stop = "a target task was created"
            elif any(item["url"].rstrip("/") != expected_url(route, channel)
                     or item["model"] != route[CHANNELS[channel][1]] for item in items):
                stop = "wrong model or route"
            elif len(everything) > LIMITS["provider_calls"]:
                stop = "provider call limit"
            elif sum(usage_tokens(item) for item in everything) > LIMITS["provider_tokens"]:
                stop = "provider token limit"
            elif time.monotonic() - started > LIMITS["active_seconds"]:
                stop = "active wall-time limit"
            if stop:
                run["stopped"] = {"step": step, "reason": stop}
                break
    finally:
        run["finished_at"] = time.time()
        run["active_seconds"] = round(time.monotonic() - started, 1)
        run["cleanup_result"] = await cmd_cleanup(client, run, args)
    return {"channels": run["channels"], "steps": run["steps"], "stopped": run.get("stopped"),
            "recreations": [{k: r.get(k) for k in ("phase", "returncode", "process_start", "blockers")}
                            for r in run["recreations"]],
            "cleanup": run["cleanup_result"]}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verify"):
        raise SystemExit("this run was already read; a result is read once")
    if not run.get("steps"):
        raise SystemExit("this run file holds no attempt")
    base = _base()
    capture = run.get("capture_released") or run.get("capture") or {}
    records = read_wire(Path(capture["record"]))
    wire, unattributed = attribute(records, run["steps"])
    rows = {step: await base.request_rows(client, int(info["request_id"])) for step, info in run["steps"].items()}
    all_items = exchanges(records)
    totals = {"provider_calls": len(all_items), "provider_tokens": sum(usage_tokens(i) for i in all_items),
              "unpaired": sum(1 for i in all_items if i["status"] is None and i["outcome"] is None),
              "active_seconds": run.get("active_seconds")}
    stored = {}
    for channel, channel_id in (run.get("channels") or {}).items():
        data = (await base._query(client, base.CHANNEL_QUERY, id=int(channel_id))).get("chat_channel_by_pk") or {}
        stored[channel] = str(((data.get("ai_metadata") or {}).get("config") or {}).get("provider") or "")
    operation = (await base._query(client, OPERATION_QUERY, id=int(client.current_operation_id))
                 ).get("operation_by_pk") or {}
    bindings = {}
    for channel, channel_id in (run.get("channels") or {}).items():
        row = (await base._query(client, CHANNEL_BINDING_QUERY, id=int(channel_id))).get("chat_channel_by_pk") or {}
        token = ((await base._query(client, TOKEN_ROW_QUERY, id=int(row["apitokens_id"]))).get("apitokens_by_pk")
                 if row.get("apitokens_id") else None)
        bindings[channel] = {"apitokens_id": row.get("apitokens_id"), "token": token or {}}
    result = evaluate(run, wire, rows, totals, run.get("route_summary") or {}, stored, bindings, operation)
    phoenix = Path(args.phoenix_db)
    result["observations"] = {
        "provider_calls_by_step": {step: len(items) for step, items in wire.items()},
        "tokens_by_step": {step: sum(usage_tokens(i) for i in items) for step, items in wire.items()},
        "tools_called": {step: [c["name"] for i in items for c in produced_calls(i)] for step, items in wire.items()},
        "streamed_replies": sum(1 for i in all_items if i["reply"].get("streamed")),
        "unattributed_calls": len(unattributed),
        "bindings": {c: {"token": b["apitokens_id"], "owner": (b["token"].get("operator") or {}).get("username")}
                     for c, b in bindings.items()},
        "phoenix_llm_spans": {step: len(base.model_spans(phoenix, info["phoenix_after"], info["phoenix_until"]))
                              for step, info in run["steps"].items()},
        "answers": {step: (native_answers(rows[step]) or [""])[-1][:300] for step in rows},
        "seconds": {"active": run.get("active_seconds")},
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
