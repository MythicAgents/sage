#!/usr/bin/env python3
"""Live check: Sage on the Mythic SDK 0.3.0rc10, gql 4.4, websockets 17.4, Phoenix 22.1.0 and current LangChain.

The change's claim is that the move keeps Mythic's GraphQL transport, Phoenix in the container and every chat provider's wire behaving as before, with Phoenix now on the network
behind its sign-in. One run file (JSON, owner-only) carries every stage. Credential values never enter it.

  snapshot --run-file F --phoenix-db P
      Read-only, BEFORE the branch is deployed. Copies the live Phoenix store with SQLite's backup API into an
      owner-only file under the run's evidence directory (rollback: the `dev` image plus that copy), records the
      store's trace ids (count and digest) and the highest Mythic event-log id, and refuses if an unresolved
      `sage_on_start` warning exists (it would absorb P3's warning as a count).
  readiness --run-file F --route-file R --phoenix-db P --callback N --bloodhound-channel C
      Read-only, after the deploy. Deployment parity; Sage consuming; no open requests; the container's installed set
      equals this branch's constraints; the Watcher's owner channel is bound to an active API token (Mythic refuses
      messages in a channel without one); the deployed Sage starts Phoenix on 0.0.0.0 with sign-in on and the shipped
      first password (its dotenv plus Sage's own defaults, read in the container), and the running Phoenix listens on
      0.0.0.0 for HTTP and gRPC and refuses GraphQL without a token; callback N active; the Watcher's owner channel and its stored profile paused;
      channel C's BloodHound settings present (names only; MCP1 copies them into its own channel) and BloodHound CE
      answering; Phoenix readable and its newest tool-result span records the result's text (M2's source); the route
      file's models; the operator's user-secret names (none may shadow a provider or Watcher route setting).
      Credentials by name and status only: the installed .env.local's LiteLLM block is its only active provider block
      and a keyed GET /models from inside the container lists GATEWAY_MODEL and SUPERVISED_MODEL; the Watcher's five
      SAGE_WATCHER_* settings are active where Sage reads them, its endpoint is the gateway, a keyed GET /models there
      with the Watcher's own key lists SAGE_WATCHER_MODEL, and Sage's own Watcher connect and scan reads succeed in
      the container; the repo .env.local's `# OpenAI`, `# Anthropic` and `# Bedrock`
      blocks hold their keys, and each is proved from this host (OpenAI and Anthropic: keyed verified-TLS model list;
      Bedrock: ListInferenceProfiles names BEDROCK_MODEL). Verdict: ready_for_live, deployment_required or not_ready.
  run --run-file F --route-file R --phoenix-db P --callback N --bloodhound-channel C --plan PLAN
      --authorized-by TEXT --pip-audit BIN
      Runs `sage-line check-attempt PLAN` and refuses on failure. Records A1 and the P1 probes, then the phases below,
      each request terminal before the next. Before each phase's first request the check waits until Mythic shows
      the Sage container running and an on-start event from Sage newer than the phase's start. Stops on a second
      target task, a limit, a wrong model or a failed phase proof; always restores and cleans up. No retries.
        N (normal mode, installed files as found): M1 C "List the callbacks..."; M2 S (SUPERVISED_MODEL) "Run whoami
          on callback N." (8 steps; first card accepted, any later card rejected); MCP1 `/bloodhound` then
          `/mcp tools BloodHound` in channel H, a new channel carrying the BloodHound connection settings copied from
          channel C (the operator's BloodHound channel; the values are copied in memory and never recorded); M3
          `/watcher status`, `/watcher resume`, `/watcher scan`, `/watcher pause` in the Watcher's owner channel.
        A (normal; the installed .env.local's provider lines commented out and the `# Anthropic` block appended):
          A1n channel A "List..." then "Which of those runs with the highest integrity?"; A2n channel X (a model name
          that does not exist) "Say hello in one short sentence."
        B (normal; the `# Bedrock` block instead): B1 channel B, the same two requests; B2 channel Y, wrong model.
        W (capture mode: the wire receiver and a marked capture block in the installed .env; files as found):
          W3 channel L (LiteLLM Gateway) "List..."; W4 channel K (Custom at the gateway) "List...".
        O (capture; the `# OpenAI` block): W1 channel O (OPENAI_REASONING_MODEL, effort low) "List..." then the
          integrity question; W2 channel G (OPENAI_PLAIN_MODEL, OpenAI API Chat Completions) "List...".
      Cleanup blanks channel H's copied BloodHound settings, restores the installed .env and .env.local byte for
      byte, recreates the container (normal mode again), sends `/watcher pause` in the owner channel if the stored
      Watcher profile is not paused, stops the receiver, takes the last P1 probe, and deletes its two file backups (they hold live keys) once both
      restores match their recorded hashes. BloodHound CE is left as the check found it.
  cleanup --run-file F
      The same cleanup, for a run that stopped outside `run`. Safe to repeat.
  verify --run-file F --route-file R --phoenix-db P
      Applies the pass rules below once (`evaluate`), reading Mythic's rows, the captured wire and Phoenix in place.

The route file is KEY=VALUE, owner-only, with no credential: GATEWAY_ENDPOINT, GATEWAY_MODEL (gpt-5.6-cyber),
SUPERVISED_MODEL (bedrock-claude-4-6-sonnet, same gateway and key), OPENAI_REASONING_MODEL, OPENAI_PLAIN_MODEL,
ANTHROPIC_MODEL, BEDROCK_MODEL. No channel carries a key or an endpoint.

Pass rules (frozen with the plan's live check; read once):
  A1  The container's installed set equals this branch's constraints.txt, `pip check` is clean, and pip-audit of that
      set reports zero findings (dev 97ddbd8's set also audits to zero, so zero means no new advisory).
  M1  C's request ends `complete` with a native answer.
  M2  S raised exactly one approval card, accepted; exactly one new Mythic task exists, on callback N, completed, with
      output and no subtasks; S ends `complete`; no tool card in S reports `wait_failed`; and the task's result holds
      at each stage, by identity (task_result_readers.py): Mythic's response rows for the task, joined in response-id
      order and decoded as Sage decodes the SDK's return, equal the full stored output of the task's lifecycle card
      `mythic-task:N:<task>`; the formatted result the follow-up LLM span carried for the one task call (Phoenix input
      messages) equals that call's own card (inline up to 4,000 characters, lazy above); and it is the task's identity
      and state prefix, the raw value exactly, then only Sage's known notes. Gate: when S's window has LLM spans, all on SUPERVISED_MODEL and none errored, and
      no first generation (what LangChain hands the agent; Phoenix's recorded LLMResult) holds an
      `issue_task_and_waitfor_task_output` call, valid or invalid, M2 did not run. A call only in a later generation
      does not count. An empty window, another model or an errored span is judged by the rule above.
  M3  `/watcher scan` completes in the owner channel after `/watcher resume`: zero pending deliveries, state `running`
      with no error, or `degraded` only with one of rc11 L5's completed-degraded codes, a scan count above the
      `/watcher status` before the resume, and no denial. `/watcher pause` then completes and shows `paused`. The
      stored profile is paused after cleanup.
  P1  Phoenix listens on every IPv4 address for HTTP (6006) and gRPC (4317) at the start (`0.0.0.0`, or the
      dual-stack wildcard `*` gRPC binds for it); /healthz answers 200 at all three
      probes (start, end of phase N, after cleanup); GraphQL without a token answers 401.
  P2  Each model-backed step has at least one LLM span in its window, and in phases W and O each step has exactly as
      many LLM spans as captured provider requests. Every trace id in the snapshot is still in the live store.
  P3  Mythic's event log holds a `sage_on_start` warning newer than the snapshot whose text names the default-password
      problem, and that text contains neither the shipped password nor either Phoenix secret.
  W1  Every O request goes to OpenAI's /v1/responses with OPENAI_REASONING_MODEL, 2xx, `store: false`, encrypted
      reasoning included and the Daybreak program named (`{"cyber": "standard"}`); O's first request's call comes
      back as Sage's tool result in a later request of that step; O's second request replays every reasoning item,
      call and result the first produced, by id. Both requests answered.
  W2  Every G request goes to OpenAI's /v1/chat/completions with OPENAI_PLAIN_MODEL, 2xx, no Responses fields; G is
      answered.
  W3  Every L request goes to the gateway's /chat/completions with GATEWAY_MODEL, 2xx, no Responses fields; a call
      comes back as Sage's tool result; L is answered.
  W4  The same as W3 for K (Custom).
  A1n A's first request: an LLM span for ANTHROPIC_MODEL produced a tool call whose id a later span of that request
      consumed as a tool result, and the request is answered. A's second request is answered and its first LLM span
      carries the first request's call ids. Sage requests no extended thinking from Anthropic, so a thinking block
      appears only if the model sends one unasked: when the first request's output carried one, the second request
      must also have been answered without a provider rejection (already required); the count is recorded.
  A2n X's request ends with Sage's provider notice for this chat's model: it names the model value and tells the
      operator to change **Model**.
  B1  As A1n, for channel B and BEDROCK_MODEL.
  B2  As A2n, for channel Y.
  MCP1 `/bloodhound` completes, reports a connection and reports ingest Ready (the shipped MCP check's L1 text); then
      the `/mcp tools BloodHound` answer lists exactly one server, `BloodHound`, under which `file_upload`,
      `domain_info` and `cypher_query` each appear exactly once.
  L   Containment: at most 40 provider calls and 500,000 tokens across the set, one target task, 60 active minutes,
      every request sent once, no stop by the check, and both installed files restored byte for byte.
Did not run: a criterion is recorded as not run (null), never as passed, when M2's gate above did not open, or when
its gate step (W1 first request, W3, W4, A1n first, B1 first) had the right route, or a model that answered, but made no tool call; or when the check
itself stopped the set (a phase's credential proof or container readiness failed, or the check hit its own error):
every criterion with a step never sent, or sent but cut off before Sage finished it (Mythic's rows show it neither
complete nor error), P1 if only its later probes are missing, and L if the set stayed contained. A step Sage finished
is judged from Mythic's rows as usual; a cut-off step's Phoenix window is left out of P2. The verdict is `fail` if any criterion that ran failed, else
`not_run` (not an attempt) if any did not run, else `pass`. A wrong route or a provider failure is a failure.
Observations, not pass criteria: tokens and wall time per step, thinking blocks seen, how the notice renders, M2's
LLM span count and any later-generation calls in its window.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = REPO_ROOT / "Payload_Type" / "sage"
_SCRIPTS = REPO_ROOT / "skills" / "sage-focused-capability-tests" / "scripts"
_DEPLOYMENT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"


def _module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _split():
    return _module("sage_rc10_check_split", _SCRIPTS / "check_provider_split.py")


def _rc11():
    return _module("sage_rc10_check_rc11", _SCRIPTS / "check_mythic_container_rc11.py")


def _mcp():
    return _module("sage_rc10_check_mcp", _SCRIPTS / "check_mcp_client_phoenix.py")


def _dep():
    return _module("sage_rc10_check_dependency", _SCRIPTS / "check_dependency_advisories.py")


def _base():
    return _split()._base()


def _readers():
    """Transport, presentation and annotation readers for M2 (task_result_readers.py); offline tests use the same."""
    return _module("sage_rc10_check_readers", _SCRIPTS / "task_result_readers.py")


def _deployment():
    return _module("sage_rc10_check_deployment", _DEPLOYMENT_PATH)


# --------------------------------------------------------------------------------------------------
# Steps, channels, limits
# --------------------------------------------------------------------------------------------------

LIST = "List the callbacks Mythic has right now, with host and user."
INTEGRITY = "Which of those runs with the highest integrity?"
WHOAMI = "Run whoami on callback {callback}."
HELLO = "Say hello in one short sentence."
MISSING_MODEL_SUFFIX = "-sage-rc10-check-missing"
# The supervised whoami reads the command's arguments before proposing the task; 4 steps halted it before the card.
SUPERVISED_MAX_STEPS = 8
BLOODHOUND_KEYS = ("BLOODHOUND_URL", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY")
# (step, phase, channel key, prompt, accepts cards, model-backed)
STEPS = (
    ("M1", "N", "C", LIST, False, True),
    ("M2", "N", "S", WHOAMI, True, True),
    # BloodHound's MCP connection is process-wide and opens on `/bloodhound`; `/mcp tools` only reads it.
    ("MCP1_connect", "N", "H", "/bloodhound", False, False),
    ("MCP1", "N", "H", "/mcp tools BloodHound", False, False),
    # A paused Watcher answers `/watcher scan` with its snapshot and never polls, so the scan runs between a resume and
    # a pause (cleanup pauses again if the stored profile is not paused).
    ("M3_status", "N", "WATCHER", "/watcher status", False, False),
    ("M3_resume", "N", "WATCHER", "/watcher resume", False, False),
    ("M3_scan", "N", "WATCHER", "/watcher scan", False, False),
    ("M3_pause", "N", "WATCHER", "/watcher pause", False, False),
    ("A1n_1", "A", "A", LIST, False, True),
    ("A1n_2", "A", "A", INTEGRITY, False, True),
    ("A2n", "A", "X", HELLO, False, False),
    ("B1_1", "B", "B", LIST, False, True),
    ("B1_2", "B", "B", INTEGRITY, False, True),
    ("B2", "B", "Y", HELLO, False, False),
    ("W3", "W", "L", LIST, False, True),
    ("W4", "W", "K", LIST, False, True),
    ("W1_1", "O", "O", LIST, False, True),
    ("W1_2", "O", "O", INTEGRITY, False, True),
    ("W2", "O", "G", LIST, False, True),
)
PHASES = ("N", "A", "B", "W", "O")
CAPTURE_PHASES = {"W", "O"}
# channel key -> (provider, route-file model key, mode, extra config)
CHANNELS = {
    "C": ("litellm", "GATEWAY_MODEL", "conversation", {}),
    "S": ("litellm", "SUPERVISED_MODEL", "supervised", {"max_steps": SUPERVISED_MAX_STEPS}),
    "H": ("litellm", "GATEWAY_MODEL", "conversation", {}),
    "A": ("anthropic", "ANTHROPIC_MODEL", "conversation", {}),
    "X": ("anthropic", "ANTHROPIC_MODEL", "conversation", {"missing_model": True}),
    "B": ("bedrock", "BEDROCK_MODEL", "conversation", {}),
    "Y": ("bedrock", "BEDROCK_MODEL", "conversation", {"missing_model": True}),
    "L": ("litellm", "GATEWAY_MODEL", "conversation", {}),
    "K": ("openai_compatible", "GATEWAY_MODEL", "conversation", {}),
    "O": ("openai", "OPENAI_REASONING_MODEL", "conversation", {"SAGE_REASONING_EFFORT": "low"}),
    "G": ("openai", "OPENAI_PLAIN_MODEL", "conversation", {"SAGE_OPENAI_API": "chat_completions"}),
}
WIRE_STEPS = {"W3": ("gateway", "chat_completions"), "W4": ("gateway", "chat_completions"),
              "W1_1": ("openai", "responses"), "W1_2": ("openai", "responses"),
              "W2": ("openai", "chat_completions")}
GATE_STEPS = ("W1_1", "W3", "W4", "A1n_1", "B1_1")
GATE_CRITERION = {"W1_1": "W1", "W3": "W3", "W4": "W4", "A1n_1": "A1n", "B1_1": "B1"}
CRITERION_STEPS = {"M1": ("M1",), "M2": ("M2",), "MCP1": ("MCP1_connect", "MCP1"),
                   "M3": ("M3_status", "M3_resume", "M3_scan", "M3_pause"),
                   "A1n": ("A1n_1", "A1n_2"), "A2n": ("A2n",), "B1": ("B1_1", "B1_2"), "B2": ("B2",),
                   "W3": ("W3",), "W4": ("W4",), "W1": ("W1_1", "W1_2"), "W2": ("W2",)}
PHASE_CRITERIA = {"N": ("M1", "M2", "M3", "MCP1"), "A": ("A1n", "A2n"), "B": ("B1", "B2"), "W": ("W3", "W4"),
                  "O": ("W1", "W2")}
ROUTE_KEYS = ("GATEWAY_ENDPOINT", "GATEWAY_MODEL", "SUPERVISED_MODEL", "OPENAI_REASONING_MODEL", "OPENAI_PLAIN_MODEL",
              "ANTHROPIC_MODEL", "BEDROCK_MODEL")
EXPECTED_GATEWAY_MODEL = "gpt-5.6-cyber"
# Same gateway, endpoint and key as GATEWAY_MODEL; its replies keep text and a tool call in one choice.
EXPECTED_SUPERVISED_MODEL = "bedrock-claude-4-6-sonnet"
TASK_TOOL = "issue_task_and_waitfor_task_output"
WATCHER_ROUTE_KEYS = ("SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT", "SAGE_WATCHER_API_KEY",
                      "SAGE_WATCHER_APITOKEN")
OPENAI_ENDPOINT = "https://api.openai.com/v1"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"
LIMITS = {"provider_calls": 40, "provider_tokens": 500_000, "target_tasks": 1, "active_seconds": 60 * 60}
MAX_STEPS = 4
STEP_TIMEOUT = 300
WHOAMI_TIMEOUT = 600
SLASH_TIMEOUT = 180
SETTLE_SECONDS = 3
PHASE_READY_SECONDS = 240
EXPECTED_PACKAGES = {"mythic": "0.3.0rc10", "gql": "4.4.0", "graphql-core": "3.3.2", "websockets": "17.4",
                     "arize-phoenix": "22.1.0", "langchain": "1.5.2", "langchain-core": "1.8.5",
                     "langchain-openai": "1.8.5", "langchain-anthropic": "1.7.13", "langchain-aws": "1.10.0",
                     "langsmith": "0.16.2", "fastmcp-slim": "4.2.6", "anthropic": "1.17.0"}
SHIPPED_PASSWORD = "sage-phoenix-change-me"
NOTICE_PHRASE = "still accepts the default admin password"
MODEL_NOTICE_ACTION = "change **Model**"
BLOCK_KEYS = {"OpenAI": ("API_KEY",), "Anthropic": ("API_KEY",),
              "Bedrock": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION")}
PHASE_BLOCK = {"A": "Anthropic", "B": "Bedrock", "O": "OpenAI"}
# Every provider credential or endpoint Sage reads; a phase's installed .env.local keeps only its own block's.
PROVIDER_LINE_KEYS = ("provider", "model", "API_ENDPOINT", "API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE",
                      "OPENAI_BASE_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                      "AWS_DEFAULT_REGION")
PHASE_HEADER = "# {block} (Mythic SDK rc10 check, phase {phase}; restored by the check)"
SHADOWING_SECRETS = ("API_KEY", "API_ENDPOINT", "provider", "model", "SAGE_OPENAI_API", "SAGE_REASONING_EFFORT",
                     "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION",
                     "SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT", "SAGE_WATCHER_API_KEY")

CALLBACK_QUERY = """
query SageRc10Callback($cb: Int!) { callback(where: {display_id: {_eq: $cb}}) { display_id active } }
"""
TASKS_AFTER_QUERY = """
query SageRc10Tasks($after: Int!) {
  task(where: {display_id: {_gt: $after}}, order_by: {display_id: asc}) {
    id display_id command_name completed status callback { display_id }
    responses(order_by: {id: asc}) { id response_text }
  }
}
"""
# The SDK's own subtask lookup (mythic.get_all_subtask_ids): a task's output includes its subtasks' rows.
SUBTASKS_QUERY = """
query SageRc10Subtasks($task_id: Int!) { task(where: {parent_task_id: {_eq: $task_id}}) { id display_id } }
"""
REQUEST_ROWS_QUERY = """
query SageRc10Rows($rid: Int!) {
  chat_message(where: {chat_request_id: {_eq: $rid}}, order_by: {id: asc}) {
    id author_type message metadata tool_output
  }
  chat_request_by_pk(id: $rid) { id status }
}
"""
EVENT_LOG_QUERY = """
query SageRc10Events($after: Int!) {
  operationeventlog(where: {id: {_gt: $after}, source: {_eq: "sage_on_start"}}, order_by: {id: asc}) {
    id message warning resolved timestamp
  }
}
"""
UNRESOLVED_ON_START_QUERY = """
query SageRc10OpenWarnings {
  operationeventlog(where: {source: {_eq: "sage_on_start"}, warning: {_eq: true}, resolved: {_eq: false},
                            deleted: {_eq: false}}) { id }
}
"""
MAX_EVENT_QUERY = "query SageRc10MaxEvent { operationeventlog(order_by: {id: desc}, limit: 1) { id } }"
CHANNEL_QUERY = ("query SageRc10Channel($id: Int!) { chat_channel_by_pk(id: $id) { id chat_model locked ai_metadata "
                 "apitokens_id } }")
TOKEN_QUERY = "query SageRc10Token($id: Int!) { apitokens_by_pk(id: $id) { id active deleted } }"


# --------------------------------------------------------------------------------------------------
# Route file, channel configs, run file
# --------------------------------------------------------------------------------------------------

def read_route(path: Path) -> dict[str, str]:
    """KEY=VALUE route file: owner-only, no credential, every key present, the plan's gateway model."""
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"{path} must be owner-only (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if any(word in key.upper() for key in values for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        raise SystemExit(f"{path} must not contain a credential")
    missing = [key for key in ROUTE_KEYS if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    if values["GATEWAY_MODEL"] != EXPECTED_GATEWAY_MODEL:
        raise SystemExit(f"GATEWAY_MODEL must be {EXPECTED_GATEWAY_MODEL}; no substitute")
    if values["SUPERVISED_MODEL"] != EXPECTED_SUPERVISED_MODEL:
        raise SystemExit(f"SUPERVISED_MODEL must be {EXPECTED_SUPERVISED_MODEL}; no substitute")
    gateway = urlsplit(values["GATEWAY_ENDPOINT"])
    if gateway.scheme != "https" or not gateway.hostname or (gateway.hostname or "").endswith("api.openai.com"):
        raise SystemExit("GATEWAY_ENDPOINT must be the gateway's https base URL")
    return values


def channel_model(route: dict[str, str], key: str) -> str:
    provider, model_key, _mode, extra = CHANNELS[key]
    return route[model_key] + (MISSING_MODEL_SUFFIX if extra.get("missing_model") else "")


def channel_config(route: dict[str, str], key: str, bloodhound: dict[str, str] | None = None) -> dict[str, Any]:
    """One channel's native configuration: no provider key or endpoint, ever. Channel H also carries the BloodHound
    connection settings copied from the operator's BloodHound channel (``bloodhound``), as that channel holds them."""
    provider, _model_key, mode, extra = CHANNELS[key]
    config: dict[str, Any] = {"provider": provider, "model": channel_model(route, key), "mode": mode,
                              "autonomous_solve": False, "max_steps": MAX_STEPS}
    config.update({k: v for k, v in extra.items() if k != "missing_model"})
    if key == "H":
        config.update({k: (bloodhound or {}).get(k, "") for k in BLOODHOUND_KEYS})
    return {"config": config, "channel_metadata_display": {"display": "expanded; max=15"}}


def expected_url(route: dict[str, str], step: str) -> str:
    origin, api = WIRE_STEPS[step]
    base = route["GATEWAY_ENDPOINT"] if origin == "gateway" else OPENAI_ENDPOINT
    return base.rstrip("/") + ("/responses" if api == "responses" else "/chat/completions")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, run: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, indent=2, sort_keys=True, default=str) + "\n")
    os.chmod(path, 0o600)


def evidence_dir(run: dict[str, Any]) -> Path:
    if not run.get("evidence_dir"):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run["evidence_dir"] = str(REPO_ROOT / ".sage_history/2026/11/diagnostics/mythic-sdk-rc10"
                                  / f"{stamp}-live-check")
    path = Path(run["evidence_dir"])
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# --------------------------------------------------------------------------------------------------
# Dotenv blocks (pure; values leave these functions only into the installed copy, never the run file)
# --------------------------------------------------------------------------------------------------

def block_values(repo_local: str, block: str) -> dict[str, str]:
    """The block's keys from the repo .env.local (commented or not), e.g. ``# Bedrock`` -> its four AWS values."""
    split = _split()
    inside, values = False, {}
    for line in repo_local.splitlines():
        stripped = line.strip()
        if split._is_header(stripped):
            inside = stripped.lstrip("#").strip() == block
            continue
        if not inside:
            continue
        key, separator, value = stripped.lstrip("#").strip().partition("=")
        if separator and key.strip() in BLOCK_KEYS[block] and value.strip():
            values.setdefault(key.strip(), value.strip())
    return values


def block_presence(repo_local: str) -> dict[str, list[str]]:
    """Which of each block's keys the repo .env.local holds: names only."""
    return {block: sorted(block_values(repo_local, block)) for block in BLOCK_KEYS}


def phase_local(original: bytes, phase: str, values: dict[str, str]) -> bytes:
    """The installed .env.local for a provider phase: every active provider line commented out, then the block."""
    lines = []
    for line in original.decode().splitlines():
        name, separator, value = line.partition("=")
        if separator and value.strip() and name.strip() in PROVIDER_LINE_KEYS and not line.lstrip().startswith("#"):
            lines.append("#" + line)
        else:
            lines.append(line)
    block = PHASE_BLOCK[phase]
    lines.append(PHASE_HEADER.format(block=block, phase=phase))
    lines += [f"{key}={values[key]}" for key in BLOCK_KEYS[block]]
    return ("\n".join(lines) + "\n").encode()


def active_assignments(text: str) -> dict[str, str]:
    """Active provider assignments by name -> the header of the block each sits in (no values)."""
    split, header, found = _split(), "", {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if split._is_header(stripped):
                header = stripped.lstrip("#").strip()
            continue
        key, separator, value = line.partition("=")
        if separator and value.strip() and key.strip() in PROVIDER_LINE_KEYS:
            found[key.strip()] = header
    return found


def phase_file_blockers(phase: str, text: str) -> list[str]:
    """The installed .env.local holds exactly the phase's block keys, all under the phase's own header."""
    assignments = active_assignments(text)
    block = PHASE_BLOCK[phase]
    expected = {key: PHASE_HEADER.format(block=block, phase=phase).lstrip("#").strip() for key in BLOCK_KEYS[block]}
    return [] if assignments == expected else [f"installed .env.local does not hold only the {block} block"]


# --------------------------------------------------------------------------------------------------
# Pure readers (tested offline)
# --------------------------------------------------------------------------------------------------

def native_answers(rows: dict[str, Any]) -> list[str]:
    texts = [str(m.get("message") or "").strip() for m in rows.get("messages", [])
             if str(m.get("author_type") or "") != "user" and not _tool_card(m)]
    return [text for text in texts if text and not text.startswith(_split().NOTICE_PREFIXES)]


def _tool_card(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    card = metadata.get("tool_use") if metadata.get("special_type") == "tool_use" else None
    return card if isinstance(card, dict) else None


def answered(rows: dict[str, Any]) -> bool:
    return rows.get("status") == "complete" and bool(native_answers(rows))


def model_notice(rows: dict[str, Any], model: str) -> bool:
    """Sage's provider notice for this chat's model: names the model value and the Model setting."""
    return any(MODEL_NOTICE_ACTION in text and f"`{model}`" in text
               for text in (str(m.get("message") or "") for m in rows.get("messages", [])))


def span_round_trip(spans: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """A tool call an LLM span for ``model`` produced, consumed as a tool result by a later span (Phoenix)."""
    llm = [s for s in spans if s.get("kind") == "LLM"]
    produced = [(i, call) for i, s in enumerate(llm) if s.get("model") == model for call in s.get("produced_calls") or []]
    consumed = [call for i, call in produced if any(call in (later.get("consumed_results") or []) for later in llm[i + 1:])]
    return {"llm_spans": len(llm), "model_spans": sum(1 for s in llm if s.get("model") == model),
            "calls": [call for _i, call in produced], "consumed": consumed,
            "other_models": sorted({s.get("model") for s in llm if s.get("model") and s.get("model") != model})}


def generation_calls(output_value: Any) -> list[list[str]]:
    """Tool-call names (valid or invalid) per generation of an LLM span's recorded LLMResult (``output.value``).

    LangChain hands the agent ``generations[0][0]`` only. A gateway that puts the text in choice 0 and the call in
    choice 1 yields a second generation the agent never sees, so the first entry is what Sage received.
    """
    try:
        data = json.loads(output_value) if isinstance(output_value, str) else None
        generations = (data or {}).get("generations") or [[]]
        first = generations[0] if isinstance(generations[0], list) else []
    except (ValueError, TypeError, AttributeError, IndexError):
        return []
    calls = []
    for generation in first:
        message = generation.get("message") if isinstance(generation, dict) else None
        kwargs = message.get("kwargs") if isinstance(message, dict) else None
        kwargs = kwargs if isinstance(kwargs, dict) else {}
        calls.append([str(call.get("name") or "") for key in ("tool_calls", "invalid_tool_calls")
                      for call in (kwargs.get(key) or []) if isinstance(call, dict)])
    return calls


def first_generation_task_call_ids(output_value: Any) -> list[str]:
    """Ids of the task tool's calls in the first generation of an LLM span's recorded LLMResult (what Sage received)."""
    try:
        data = json.loads(output_value) if isinstance(output_value, str) else None
        generations = (data or {}).get("generations") or [[]]
        first = generations[0] if isinstance(generations[0], list) else []
    except (ValueError, TypeError, AttributeError, IndexError):
        return []
    message = first[0].get("message") if first and isinstance(first[0], dict) else None
    kwargs = message.get("kwargs") if isinstance(message, dict) else None
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    return [str(call.get("id") or "") for call in (kwargs.get("tool_calls") or [])
            if isinstance(call, dict) and call.get("name") == TASK_TOOL and call.get("id")]


def m2_formatted(db: Path, after: int, until: int, call_ids: list[str]) -> dict[str, Any]:
    """The formatted task result the model's follow-up carried for the one task call (Phoenix input messages).

    Exactly one task call must have been received, and every span that consumed it must carry the same text;
    otherwise ``content`` is None and M2's presentation reader fails for want of a value."""
    if len(set(call_ids)) != 1:
        return {"call_id": "", "content": None, "spans": [], "reason": f"{len(set(call_ids))} task call(s) received"}
    call_id = call_ids[0]
    spans = [span for span in _dep().spans(db, after, until, result_for=call_id) if "consumed_content" in span]
    texts = {text for span in spans for text in span["consumed_content"]}
    content = next(iter(texts)) if len(texts) == 1 and isinstance(next(iter(texts)), str) else None
    reason = "" if content is not None else f"{len(spans)} span(s) consumed the call with {len(texts)} distinct text(s)"
    return {"call_id": call_id, "content": content, "spans": [span["id"] for span in spans], "reason": reason}


def phoenix_records_tool_results(db: Path, limit: int = 400) -> dict[str, Any]:
    """Readiness for M2's presentation reader: the newest LLM span whose input carries a tool result also records that
    result's text (Phoenix input messages). M2 itself fails closed when its own follow-up span has no text."""
    dep = _dep()
    try:
        until = _base().phoenix_mark(db)
        window = dep.spans(db, max(0, until - limit), until)
    except Exception as exc:  # unreadable store: a readiness blocker, never a crash
        return {"span": None, "text": False, "error": type(exc).__name__}
    for span in reversed([s for s in window if s.get("kind") == "LLM" and s.get("consumed_results")]):
        call = span["consumed_results"][0]
        named = dep.spans(db, span["id"] - 1, span["id"], result_for=call)
        texts = (named[0].get("consumed_content") if named else None) or []
        return {"span": span["id"], "text": bool(texts) and all(isinstance(t, str) and t for t in texts)}
    return {"span": None, "text": False}


def phoenix_tool_result_blockers(result: dict[str, Any] | None) -> list[str]:
    if (result or {}).get("text") is True:
        return []
    return ["Phoenix's newest tool-result span does not record the result's text, so M2 cannot be read"]


def m2_generations(db: Path, after: int, until: int) -> list[dict[str, Any]]:
    """Each LLM span in (after, until]: id, model, status and its tool calls per generation (Phoenix, read in place)."""
    with _base()._phoenix(db) as conn:
        rows = conn.execute("SELECT id, status_code, attributes FROM spans WHERE span_kind = 'LLM' AND id > ? "
                            "AND id <= ? ORDER BY id", (after, until)).fetchall()
    spans = []
    for span_id, status, attributes in rows:
        try:
            attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
        except ValueError:
            attrs = {}
        llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
        output = attrs.get("output") if isinstance(attrs.get("output"), dict) else {}
        spans.append({"id": span_id, "model": str(llm.get("model_name") or ""), "status": status,
                      "generations": generation_calls(output.get("value")),
                      "task_call_ids": first_generation_task_call_ids(output.get("value"))})
    return spans


def m2_gate(spans: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """M2's gate: did the supervised model, on its route and without a provider error, put the task call in a first
    generation? ``answered`` is false for an empty window, another model or an errored span: those are judged, never
    "not run"."""
    first = [calls for span in spans for calls in (span.get("generations") or [[]])[:1]]
    return {"llm_spans": len(spans),
            "answered": bool(spans) and all(s.get("model") == model and s.get("status") != "ERROR" for s in spans),
            "first_generation_call": any(TASK_TOOL in calls for calls in first),
            "later_generation_calls": [calls for span in spans for calls in (span.get("generations") or [])[1:]
                                       if calls]}


def thinking_blocks(db: Path, after: int, until: int) -> int:
    """Thinking or redacted-thinking content types in LLM span outputs in (after, until] (an observation)."""
    base = _base()
    count = 0
    with base._phoenix(db) as conn:
        rows = conn.execute("SELECT attributes FROM spans WHERE span_kind = 'LLM' AND id > ? AND id <= ?",
                            (after, until)).fetchall()
    for (attributes,) in rows:
        try:
            attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
        except ValueError:
            continue
        llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
        count += sum(1 for path, value in base._walk(llm.get("output_messages") or [])
                     if path.endswith("type") and value in {"thinking", "redacted_thinking"})
    return count


def listeners(ss_output: str) -> dict[str, bool]:
    """P1: whether 6006 and 4317 are bound on every IPv4 address (``ss -ltnH``).

    ``0.0.0.0:port`` is the IPv4 wildcard. gRPC turns Phoenix's ``0.0.0.0:4317`` into a dual-stack socket, which ``ss``
    prints as ``*:port`` and which also accepts IPv4 on every address. ``[::]:port`` is an IPv6-only socket and does not.
    """
    bound = set()
    for line in ss_output.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            bound.add(fields[3])

    def every_ipv4(port: int) -> bool:
        return f"0.0.0.0:{port}" in bound or f"*:{port}" in bound

    return {"http_0.0.0.0:6006": every_ipv4(6006), "grpc_0.0.0.0:4317": every_ipv4(4317)}


def notice_events(events: list[dict[str, Any]], forbidden: list[str]) -> dict[str, Any]:
    """P3: the default-password warning among on-start events, and whether its text leaks a forbidden value."""
    found = [e for e in events if e.get("warning") and NOTICE_PHRASE in str(e.get("message") or "")]
    leaked = any(value and value in str(e.get("message") or "") for e in found for value in forbidden)
    return {"warnings": len(found), "leaks": leaked}


# --------------------------------------------------------------------------------------------------
# Pass rules (pure; tested offline)
# --------------------------------------------------------------------------------------------------

def evaluate(run: dict[str, Any], observed: dict[str, Any], route: dict[str, str]) -> dict[str, Any]:
    """A1, M1-M3, P1-P3, W1-W4, A1n/A2n, B1/B2, MCP1 and L, each with its numbers."""
    split, rc11 = _split(), _rc11()
    requests = run.get("requests") or {}
    rows = observed.get("rows") or {}
    wire = observed.get("wire") or {}
    spans = observed.get("spans") or {}
    stopped = run.get("stopped") or {}
    reason = str(stopped.get("reason") or "")
    not_run: dict[str, str] = {}
    phase_stop = ""
    check_side = reason.startswith("check error") or (
        reason.startswith("phase ") and ("credential" in reason or "not ready" in reason or "restart" in reason))
    def cut_off(step: str) -> bool:
        """Sent, then stopped by the check's own error before Sage finished the request (Mythic's rows decide)."""
        return bool((requests.get(step) or {}).get("check_error")) and (rows.get(step) or {}).get("status") not in {
            "complete", "error"}

    if check_side:
        # The check (a failed phase proof, a container that never became ready, or the check's own error) stopped
        # the set: a criterion with a step never sent, or cut off before Sage finished it, is not run. A step Sage
        # finished is judged from Mythic's rows like any other.
        phase_stop = reason
        for name, steps in CRITERION_STEPS.items():
            if any(step not in requests or cut_off(step) for step in steps):
                not_run[name] = reason

    def route_problems(step: str) -> list[str]:
        origin, api = WIRE_STEPS[step]
        model = route["OPENAI_REASONING_MODEL"] if step.startswith("W1") else (
            route["OPENAI_PLAIN_MODEL"] if step == "W2" else route["GATEWAY_MODEL"])
        problems = [] if wire.get(step) else ["no captured request"]
        for item in wire.get(step, []):
            if item["url"].rstrip("/") != expected_url(route, step):
                problems.append("url")
            if item["model"] != model:
                problems.append("model")
            if not (item["status"] and 200 <= int(item["status"]) < 300):
                problems.append(f"status {item['status']}")
            if api == "responses":
                if (item["store"] is not False or split.REASONING_INCLUDE not in (item["include"] or [])
                        or item["access_programs"] != {"cyber": "standard"}):
                    problems.append("responses projection")
            elif item["fields"]:
                problems.append("responses fields on chat completions")
        return problems

    # M2's gate: the change's effect starts at the task call Sage receives. A supervised model that answered on its
    # route without that call in a first generation never reached the card, so M2 did not run.
    gate_m2 = m2_gate((observed.get("generations") or {}).get("M2") or [], route["SUPERVISED_MODEL"])
    if "M2" in requests and "M2" not in not_run and gate_m2["answered"] and not gate_m2["first_generation_call"]:
        not_run["M2"] = "M2 made no task call in a first generation"

    routes = {step: route_problems(step) or "ok" for step in WIRE_STEPS if step in requests}
    # A gate step whose route was right (or whose model answered) but made no tool call never exercised the round
    # trip: the check did not run. A wrong route or a provider failure is a failure of the change, whatever it called.
    for gate in GATE_STEPS:
        if gate not in requests:
            continue
        if gate in WIRE_STEPS:
            exercised = routes.get(gate) != "ok" or bool(split.round_trip(wire.get(gate, []))["calls"])
        else:
            trip = span_round_trip(spans.get(gate, []), channel_model(route, "A" if gate.startswith("A") else "B"))
            exercised = trip["model_spans"] == 0 or bool(trip["calls"])
        if not exercised:
            not_run.setdefault(GATE_CRITERION[gate], f"{gate} made no tool call")

    detail: dict[str, Any] = {}
    criteria: dict[str, bool] = {}

    a1 = run.get("a1") or {}
    packages = a1.get("packages") or {}
    detail["A1"] = {k: a1.get(k) for k in ("differing_from_constraints", "pip_check_returncode", "audit_ran",
                                           "audit_findings")} | {"packages": packages}
    criteria["A1"] = (all(packages.get(n) == v for n, v in EXPECTED_PACKAGES.items())
                      and not a1.get("differing_from_constraints") and a1.get("pip_check_returncode") == 0
                      and a1.get("audit_ran") is True and a1.get("audit_findings") == 0)

    criteria["M1"] = answered(rows.get("M1") or {})

    m2 = requests.get("M2") or {}
    tasks = observed.get("tasks") or []
    task = tasks[0] if len(tasks) == 1 else {}
    # The task's result is compared stage by stage, by identity (task_result_readers.py): Mythic's rows equal the
    # task's own lifecycle card; the follow-up's formatted result equals its call's own card; and the formatted result
    # is the task's identity prefix, the raw value and only Sage's known notes. No other card is searched.
    readers = _readers()
    m2_rows = rows.get("M2") or {}
    formatted = (observed.get("formatted") or {}).get("M2") or {}
    callback = run.get("callback")
    transport = readers.transport_holds(task, m2_rows, callback=callback) if task else {"holds": False,
                                                                                         "reasons": ["no one task"]}
    presentation = readers.presentation_holds(formatted.get("content"), m2_rows, str(formatted.get("call_id") or ""))
    annotation = (readers.annotation_holds(formatted.get("content"), m2_rows, task, callback=callback) if task
                  else {"holds": False, "reasons": ["no one task"]})
    wait_failed = any("wait_failed" in json.dumps(card) or "wait_failed" in str(message.get("tool_output") or "")
                      for message in m2_rows.get("messages", []) if (card := _tool_card(message)) is not None)
    detail["M2"] = {"cards_seen": m2.get("cards_seen"), "cards_accepted": m2.get("cards_accepted"),
                    "tasks": len(tasks), "status": m2_rows.get("status"), "wait_failed": wait_failed,
                    "transport": transport, "presentation": presentation, "annotation": annotation,
                    "formatted_source": {key: formatted.get(key) for key in ("call_id", "spans", "reason")},
                    "llm_spans": gate_m2["llm_spans"], "first_generation_call": gate_m2["first_generation_call"],
                    "later_generation_calls": gate_m2["later_generation_calls"]}
    criteria["M2"] = (m2.get("cards_seen") == 1 and m2.get("cards_accepted") == 1 and len(tasks) == 1
                      and m2_rows.get("status") == "complete" and not wait_failed
                      and transport["holds"] and presentation["holds"] and annotation["holds"])

    before = rc11.parse_status("\n".join(rc11._texts(rows.get("M3_status") or {}))) or {}
    scan_text = "\n".join(rc11._texts(rows.get("M3_scan") or {}))
    after = rc11.parse_status(scan_text) or {}
    pause = rc11.parse_status("\n".join(rc11._texts(rows.get("M3_pause") or {}))) or {}
    detail["M3"] = {"before": before, "after": after, "pause": pause,
                    "paused_after_cleanup": observed.get("watcher_paused")}
    criteria["M3"] = ((rows.get("M3_scan") or {}).get("status") == "complete" and bool(after)
                      and rc11.scan_completed(after)
                      and after.get("scans") is not None and before.get("scans") is not None
                      and after["scans"] > before["scans"] and "Watcher control denied" not in scan_text
                      and (rows.get("M3_pause") or {}).get("status") == "complete" and pause.get("state") == "paused"
                      and observed.get("watcher_paused") is True)

    probes = run.get("p1") or {}
    health = probes.get("healthz") or []
    detail["P1"] = probes
    criteria["P1"] = (all((probes.get("listeners") or {}).values()) and bool(probes.get("listeners"))
                      and len(health) == 3 and all(code == 200 for code in health)
                      and probes.get("graphql_unauthenticated") == 401)
    if (phase_stop and not criteria["P1"] and len(health) < 3 and all(code == 200 for code in health)
            and all((probes.get("listeners") or {}).values()) and probes.get("graphql_unauthenticated") == 401):
        not_run["P1"] = phase_stop

    p2 = {}
    for step, phase, _key, _prompt, _cards, backed in STEPS:
        if not backed or step not in requests or (requests[step] or {}).get("check_error"):
            continue
        llm = [s for s in spans.get(step, []) if s.get("kind") == "LLM"]
        entry = {"llm_spans": len(llm)}
        if phase in CAPTURE_PHASES:
            entry["captured_requests"] = len(wire.get(step, []))
        p2[step] = entry
    traces = observed.get("traces") or {}
    detail["P2"] = {"steps": p2, "snapshot_traces": traces.get("snapshot"), "missing": traces.get("missing")}
    criteria["P2"] = (bool(p2) and all(e["llm_spans"] >= 1 and e.get("captured_requests", e["llm_spans"])
                                       == e["llm_spans"] for e in p2.values())
                      and isinstance(traces.get("snapshot"), int) and traces["snapshot"] > 0
                      and traces.get("missing") == 0)

    p3 = observed.get("notice") or {}
    detail["P3"] = p3
    criteria["P3"] = p3.get("warnings", 0) >= 1 and p3.get("leaks") is False

    detail["wire_routes"] = routes
    o1, o2 = wire.get("W1_1", []), wire.get("W1_2", [])
    o1_reasoning = [pair for item in o1 for pair in split.reasoning_items(item)]
    o1_calls = [call for item in o1 for call in split.produced_calls(item)]
    first_o2 = split.replayed(o2[0]) if o2 else {"calls": {}, "results": {}, "reasoning": []}
    replay = {"o1_reasoning": len(o1_reasoning), "o1_calls": len(o1_calls), **split.carried(o1, o1_calls, first_o2),
              "reasoning_replayed": sum(1 for pair in o1_reasoning if pair in first_o2["reasoning"]),
              "round_trip": split.round_trip(o1)}
    detail["W1"] = replay
    criteria["W1"] = (routes.get("W1_1") == "ok" and routes.get("W1_2") == "ok"
                      and bool(replay["round_trip"]["consumed"]) and replay["o1_reasoning"] >= 1
                      and replay["calls_replayed"] == replay["results_replayed"] == replay["o1_calls"] >= 1
                      and replay["reasoning_replayed"] == replay["o1_reasoning"]
                      and answered(rows.get("W1_1") or {}) and answered(rows.get("W1_2") or {}))
    criteria["W2"] = routes.get("W2") == "ok" and answered(rows.get("W2") or {})
    for name, step in (("W3", "W3"), ("W4", "W4")):
        trip = split.round_trip(wire.get(step, []))
        detail[name] = trip
        criteria[name] = routes.get(step) == "ok" and bool(trip["consumed"]) and answered(rows.get(step) or {})

    for name, first, second, missing, key in (("A1n", "A1n_1", "A1n_2", "A2n", "A"), ("B1", "B1_1", "B1_2", "B2", "B")):
        model = channel_model(route, key)
        trip = span_round_trip(spans.get(first, []), model)
        second_llm = [s for s in spans.get(second, []) if s.get("kind") == "LLM"]
        carried = set(trip["calls"]) <= set((second_llm[0].get("input_calls") or [])) if second_llm else False
        thinking = (observed.get("thinking") or {}).get(first)
        detail[name] = {**trip, "second_carries_calls": carried, "second_llm_spans": len(second_llm),
                        "thinking_blocks_first": thinking}
        criteria[name] = (bool(trip["consumed"]) and not trip["other_models"] and answered(rows.get(first) or {})
                          and carried and answered(rows.get(second) or {}))
        wrong = channel_model(route, "X" if key == "A" else "Y")
        notice_name = "A2n" if key == "A" else "B2"
        detail[notice_name] = {"status": (rows.get(missing) or {}).get("status"),
                               "names_model_and_setting": model_notice(rows.get(missing) or {}, wrong)}
        criteria[notice_name] = model_notice(rows.get(missing) or {}, wrong)

    connect = rows.get("MCP1_connect") or {}
    connect_text = _mcp().answer(connect)
    connection = next((line for line in connect_text.splitlines() if line.startswith("**Connection:**")), "")
    # Sage reports ingest Ready only on a live connection (sage_chat/slash.py, `_handle_bloodhound`).
    connected = connect.get("status") == "complete" and bool(connection) and _mcp().INGEST_READY in connect_text
    mcp_text = _mcp().answer(rows.get("MCP1") or {})
    servers = _mcp().listed_tools(mcp_text)
    detail["MCP1"] = {"connect_status": connect.get("status"), "connection_line": connection[:300],
                      "ingest_ready": _mcp().INGEST_READY in connect_text, "servers": servers,
                      "status": (rows.get("MCP1") or {}).get("status")}
    criteria["MCP1"] = (connected and (rows.get("MCP1") or {}).get("status") == "complete"
                        and list(servers) == ["BloodHound"]
                        and all(servers["BloodHound"].get(tool) == 1 for tool in _mcp().REQUIRED_TOOLS))

    totals = observed.get("totals") or {}
    files = run.get("files_after_cleanup") or {}
    detail["L"] = {**totals, "target_tasks": len(tasks), "stopped": run.get("stopped"),
                   "active_seconds": run.get("active_seconds"), **files}
    contained = (all(int(r.get("sends") or 1) == 1 and not r.get("timeout") for r in requests.values())
                 and isinstance(totals.get("provider_calls"), int)
                 and totals["provider_calls"] <= LIMITS["provider_calls"]
                 and isinstance(totals.get("provider_tokens"), int)
                 and totals["provider_tokens"] <= LIMITS["provider_tokens"]
                 and len(tasks) <= LIMITS["target_tasks"]
                 and isinstance(run.get("active_seconds"), (int, float))
                 and run["active_seconds"] <= LIMITS["active_seconds"]
                 and files.get("env_local_restored") is True and files.get("env_restored") is True)
    complete = not run.get("stopped") and [s for s, *_ in STEPS if s in requests] == [s for s, *_ in STEPS]
    criteria["L"] = contained and complete
    if phase_stop and contained:
        not_run["L"] = phase_stop
    # A criterion whose steps never ran is recorded as not run (None), never as passed; any criterion that ran and
    # failed still fails the attempt.
    for name in not_run:
        criteria[name] = None
    detail["not_run"] = not_run
    values = list(criteria.values())
    verdict = "fail" if any(v is False for v in values) else ("not_run" if any(v is None for v in values) else "pass")
    result = {"criteria": criteria, "detail": detail, "verdict": verdict}
    if verdict == "not_run":
        result["reason"] = "; ".join(sorted(set(not_run.values())))
    return result


# --------------------------------------------------------------------------------------------------
# Probes (read-only)
# --------------------------------------------------------------------------------------------------

def phoenix_trace_ids(db: Path) -> list[str]:
    with _base()._phoenix(db) as conn:
        return sorted(str(row[0]) for row in conn.execute("SELECT trace_id FROM traces"))


def ss_listeners() -> dict[str, bool]:
    completed = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True, timeout=20)
    return listeners(completed.stdout)


def http_status(url: str, *, method: str = "GET", body: bytes | None = None) -> int | None:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except Exception:  # noqa: BLE001 - unreachable is "no status"
        return None


def graphql_unauthenticated(phoenix_url: str) -> int | None:
    return http_status(phoenix_url.rstrip("/") + "/graphql", method="POST",
                       body=json.dumps({"query": "{ projects { edges { node { name } } } }"}).encode())


def container_phoenix_secrets() -> list[str]:
    """Both Phoenix secrets as Sage holds them, read in memory only to check P3's text; never stored or printed."""
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c",
                                "import os,pathlib,sys;sys.path.insert(0,'/Mythic');"
                                "from dotenv_bootstrap import load_sage_dotenv;load_sage_dotenv('/Mythic');"
                                "p=pathlib.Path(os.environ.get('PHOENIX_WORKING_DIR') or "
                                "'/Mythic/.phoenix')/'sage-phoenix-secrets.json';print(p.read_text() if p.exists() "
                                "else '{}')"], capture_output=True, text=True, timeout=30)
    try:
        values = json.loads(completed.stdout.strip() or "{}")
    except ValueError:
        values = {}
    return [str(v) for v in values.values() if isinstance(v, str)]


_WATCHER_PROFILE_PROBE = r"""
import json, sqlite3, sys
sys.path.insert(0, "/Mythic")
from dotenv_bootstrap import load_sage_dotenv
load_sage_dotenv("/Mythic")
out = {"profile_present": False, "owner_channel_id": None, "paused": None, "lifecycle_state": None, "error": None}
try:
    from runtime_db import database_path
    with sqlite3.connect(f"file:{database_path()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT owner_channel_id, paused, lifecycle_state FROM watcher_profiles "
                          "WHERE operation_id = ?", (sys.argv[1],)).fetchall()
    if rows:
        out.update(profile_present=True, owner_channel_id=int(rows[0][0]), paused=bool(rows[0][1]),
                   lifecycle_state=rows[0][2])
except Exception as error:
    out["error"] = type(error).__name__
print(json.dumps(out))
"""


# Inside the container: the Phoenix bind address and default password the deployed Sage starts with (Sage's dotenv,
# then Sage's own defaults, exactly as main.py applies them). Names and booleans only.
_PHOENIX_SETTINGS_PROBE = r"""
import json, os, sys
sys.path.insert(0, "/Mythic")
from dotenv_bootstrap import load_sage_dotenv
load_sage_dotenv("/Mythic")
from ai.phoenix_auth import SHIPPED_DEFAULT_ADMIN_PASSWORD, apply_phoenix_defaults
defaults = apply_phoenix_defaults()
from phoenix.config import auth_enabled_for_host, get_env_host
host = get_env_host()
print(json.dumps({"host": host, "auth_enabled": auth_enabled_for_host(host), "from_sage_defaults": defaults,
                  "password_is_shipped": os.environ.get("PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD")
                  == SHIPPED_DEFAULT_ADMIN_PASSWORD}))
"""


def phoenix_settings() -> dict[str, Any]:
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", _PHOENIX_SETTINGS_PROBE],
                               capture_output=True, text=True, timeout=60)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


def phoenix_blockers(settings: dict[str, Any], listening: dict[str, bool], unauthenticated: int | None) -> list[str]:
    """P1 and P3 can only pass when the deployed Phoenix is on every address with sign-in on and the shipped password."""
    blockers = []
    if settings.get("host") != "0.0.0.0" or settings.get("auth_enabled") is not True:
        blockers.append("the deployed Sage would not start Phoenix on every address with sign-in on")
    if settings.get("password_is_shipped") is not True:
        blockers.append("the deployed Sage's first admin password is not the shipped one, so no warning can fire")
    if not listening or not all(listening.values()):
        blockers.append("Phoenix is not listening on 0.0.0.0 for HTTP and gRPC")
    if unauthenticated != 401:
        blockers.append("Phoenix answers GraphQL without a token")
    return blockers


UPDATE_CHANNEL_MUTATION = """
mutation SageRc10ClearChannel($channelId: Int!, $metadata: jsonb!) {
  chatUpdateChannel(channel_id: $channelId, ai_metadata: $metadata) { status error }
}
"""


def without_bloodhound(metadata: Any) -> dict[str, Any]:
    """The channel's settings with the BloodHound connection values blanked; every other setting unchanged."""
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    config = dict(metadata.get("config") or {})
    config.update({key: "" for key in BLOODHOUND_KEYS if key in config})
    return {**metadata, "config": config}


async def clear_channel_bloodhound(client: Any, channel_id: int) -> bool:
    """Blank channel H's copied BloodHound settings after the set; True when Mythic accepted the update."""
    base = _base()
    try:
        row = (await base._query(client, CHANNEL_QUERY, id=channel_id)).get("chat_channel_by_pk") or {}
        result = await base.nc.mythic.execute_custom_query(
            client, UPDATE_CHANNEL_MUTATION,
            variables={"channelId": channel_id, "metadata": without_bloodhound(row.get("ai_metadata"))})
        return (result.get("chatUpdateChannel") or {}).get("status") == "success"
    except Exception:  # noqa: BLE001 - reported as False; the operator can blank them in Mythic
        return False


async def channel_token_active(client: Any, channel: dict[str, Any] | None) -> bool:
    """Whether Mythic would accept a new message in this channel: it is bound to an active, undeleted API token."""
    token_id = (channel or {}).get("apitokens_id")
    if not token_id:
        return False
    token = (await _base()._query(client, TOKEN_QUERY, id=int(token_id))).get("apitokens_by_pk") or {}
    return token.get("active") is True and token.get("deleted") is False


def watcher_profile(operation_id: Any) -> dict[str, Any]:
    return _rc11()._container_probe(_WATCHER_PROFILE_PROBE, operation_id, 60)


# Inside the container with Sage's own dotenv loader: which provider keys Sage would load and from where, and a keyed,
# non-inference listing of the phase's provider. Names, booleans and status codes only.
_PHASE_PROBE = r"""
import json, os, ssl, sys
sys.path.insert(0, "/Mythic")
from dotenv import dotenv_values
import dotenv_bootstrap as boot
phase, model = sys.argv[1], sys.argv[2]
KEYS = {"A": ("API_KEY",), "B": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION")}
environ = dict(os.environ)
out = {"container_env": sorted(k for k in KEYS[phase] if os.environ.get(k)), "sources": {}, "status": None,
       "listed": False, "tls_verified": False}
for path in boot.dotenv_paths("/Mythic"):
    for key in boot.apply_dotenv(dotenv_values(path), environ):
        if key in KEYS[phase]:
            out["sources"].setdefault(key, os.path.basename(path))
try:
    if phase == "A":
        import httpx
        from ssl_bootstrap import configure_sage_ssl_cert_file
        bundle = configure_sage_ssl_cert_file()
        context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
        response = httpx.get("https://api.anthropic.com/v1/models", headers={
            "x-api-key": environ.get("API_KEY", ""), "anthropic-version": "2023-06-01"}, timeout=20, verify=context,
            params={"limit": 1000})
        out["status"] = response.status_code
        listed = {row.get("id") for row in (response.json().get("data") or [])} if response.status_code == 200 else set()
        out["listed"], out["tls_verified"] = model in listed, True
    else:
        import boto3
        client = boto3.client("bedrock", region_name=environ.get("AWS_DEFAULT_REGION"),
                              aws_access_key_id=environ.get("AWS_ACCESS_KEY_ID"),
                              aws_secret_access_key=environ.get("AWS_SECRET_ACCESS_KEY"),
                              aws_session_token=environ.get("AWS_SESSION_TOKEN"))
        profiles, token = [], None
        while True:
            page = client.list_inference_profiles(**({"nextToken": token} if token else {}))
            profiles += [p.get("inferenceProfileId") for p in page.get("inferenceProfileSummaries") or []]
            token = page.get("nextToken")
            if not token:
                break
        out["status"], out["listed"], out["tls_verified"] = 200, model in profiles, True
except Exception as error:
    out["error"] = type(error).__name__
print(json.dumps(out))
"""


def phase_probe(phase: str, model: str) -> dict[str, Any]:
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", _PHASE_PROBE, phase, model],
                               capture_output=True, text=True, timeout=120)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


def phase_probe_blockers(phase: str, probe: dict[str, Any]) -> list[str]:
    keys = BLOCK_KEYS[PHASE_BLOCK[phase]]
    blockers = []
    if probe.get("container_env"):
        blockers.append("the container environment sets a provider key")
    if probe.get("sources") != {key: ".env.local" for key in keys}:
        blockers.append("Sage would not load every block key from the installed .env.local")
    if probe.get("status") != 200 or probe.get("listed") is not True or not probe.get("tls_verified"):
        blockers.append("the keyed model listing did not return 200 with the phase's model")
    return blockers


def gateway_probe(route: dict[str, str]) -> dict[str, Any]:
    """The split check's keyed, non-inference ``GET /models`` from the container (the key Sage loads, verified TLS),
    naming both models channel C/L and channel S use on the gateway."""
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", _split()._CONTAINER_PROBE,
                                route["GATEWAY_ENDPOINT"], route["GATEWAY_MODEL"], route["SUPERVISED_MODEL"]],
                               capture_output=True, text=True, timeout=90)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


def gateway_blockers(local: dict[str, Any], probe: dict[str, Any], route: dict[str, str]) -> list[str]:
    """The split check's phase-1 credential rules for GATEWAY_MODEL, and SUPERVISED_MODEL listed by the same key."""
    models = probe.get("models") if isinstance(probe.get("models"), dict) else {}
    blockers = _split().phase_blockers(1, local, {**probe, "models": {
        route["GATEWAY_MODEL"]: models.get(route["GATEWAY_MODEL"])}}, route)
    if models.get(route["SUPERVISED_MODEL"]) is not True:
        blockers.append("the gateway's keyed model listing does not name SUPERVISED_MODEL")
    return blockers


# Inside the container with Sage's own dotenv loader: the Watcher's route by name, and a keyed, non-inference listing at
# the Watcher's endpoint with the Watcher's own key (the Watcher never falls back to API_KEY). Names, booleans and the
# status code only.
_WATCHER_ROUTE_PROBE = r"""
import json, os, ssl, sys
sys.path.insert(0, "/Mythic")
from dotenv_bootstrap import load_sage_dotenv
load_sage_dotenv("/Mythic")
KEYS = ("SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT", "SAGE_WATCHER_API_KEY",
        "SAGE_WATCHER_APITOKEN")
value = lambda key: os.environ.get(key, "").strip()
endpoint, model = value("SAGE_WATCHER_API_ENDPOINT"), value("SAGE_WATCHER_MODEL")
out = {"present": {key: bool(value(key)) for key in KEYS}, "provider": value("SAGE_WATCHER_PROVIDER"), "model": model,
       "endpoint_is_gateway": endpoint.rstrip("/") == sys.argv[1].rstrip("/"), "status": None, "listed": False,
       "tls_verified": False}
if endpoint and value("SAGE_WATCHER_API_KEY"):
    try:
        import httpx
        from ssl_bootstrap import configure_sage_ssl_cert_file
        bundle = configure_sage_ssl_cert_file()
        context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
        response = httpx.get(endpoint.rstrip("/") + "/models", timeout=20, verify=context,
                             headers={"Authorization": "Bearer " + value("SAGE_WATCHER_API_KEY")})
        out["status"] = response.status_code
        listed = {row.get("id") for row in (response.json().get("data") or [])} if response.status_code == 200 else set()
        out["listed"], out["tls_verified"] = model in listed, True
    except Exception as error:
        out["error"] = type(error).__name__
print(json.dumps(out))
"""


def watcher_route_probe(route: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(["docker", "exec", "-w", "/Mythic", "sage", "python3", "-c", _WATCHER_ROUTE_PROBE,
                                route["GATEWAY_ENDPOINT"]], capture_output=True, text=True, timeout=90)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe did not report", "returncode": completed.returncode}


def watcher_route_blockers(probe: dict[str, Any]) -> list[str]:
    """The Watcher's five settings are active where Sage reads them, its endpoint is the gateway, and its own key lists
    SAGE_WATCHER_MODEL there."""
    present = probe.get("present") if isinstance(probe.get("present"), dict) else {}
    blockers = []
    missing = [key for key in WATCHER_ROUTE_KEYS if present.get(key) is not True]
    if missing:
        blockers.append(f"the Watcher's route lacks {', '.join(missing)} where Sage reads it")
    if probe.get("endpoint_is_gateway") is not True:
        blockers.append("the Watcher's endpoint is not the route's gateway")
    if probe.get("status") != 200 or probe.get("listed") is not True or probe.get("tls_verified") is not True:
        blockers.append("the Watcher's keyed model listing did not return 200 with SAGE_WATCHER_MODEL")
    return blockers


async def repause_watcher(client: Any, profile: dict[str, Any]) -> dict[str, Any] | None:
    """Cleanup: send `/watcher pause` in the owner channel when the stored profile is not paused; None otherwise."""
    if profile.get("paused") is not False or not profile.get("owner_channel_id"):
        return None
    nc = _base().nc
    try:
        message = await nc.create_message(client, int(profile["owner_channel_id"]), "/watcher pause")
        request_id = int(message["chat_request_id"])
        result = await nc.wait_for_request(client, request_id, timeout_seconds=SLASH_TIMEOUT)
        return {"request_id": request_id, "status": ((result or {}).get("request") or {}).get("status")}
    except Exception as error:  # noqa: BLE001 - reported; M3 then fails on the stored profile
        return {"error": type(error).__name__}


def _host_block_probe(block: str, values: dict[str, str], route: dict[str, str]) -> dict[str, Any]:
    """Readiness proof of a repo block from this host: names and status only."""
    if sorted(values) != sorted(BLOCK_KEYS[block]):
        return {"present": sorted(values), "status": None}
    sys.path.insert(0, str(RUNTIME))
    try:
        if block == "Bedrock":
            import boto3

            client = boto3.client("bedrock", region_name=values["AWS_DEFAULT_REGION"],
                                  aws_access_key_id=values["AWS_ACCESS_KEY_ID"],
                                  aws_secret_access_key=values["AWS_SECRET_ACCESS_KEY"],
                                  aws_session_token=values["AWS_SESSION_TOKEN"])
            profiles, token = [], None
            while True:
                page = client.list_inference_profiles(**({"nextToken": token} if token else {}))
                profiles += [p.get("inferenceProfileId") for p in page.get("inferenceProfileSummaries") or []]
                token = page.get("nextToken")
                if not token:
                    break
            return {"present": sorted(values), "status": 200, "listed": route["BEDROCK_MODEL"] in profiles}
        import httpx

        from ssl_bootstrap import configure_sage_ssl_cert_file

        bundle = configure_sage_ssl_cert_file()
        context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
        if block == "Anthropic":
            response = httpx.get(ANTHROPIC_ENDPOINT + "/models", params={"limit": 1000}, timeout=20, verify=context,
                                 headers={"x-api-key": values["API_KEY"], "anthropic-version": "2023-06-01"})
            models = [route["ANTHROPIC_MODEL"]]
        else:
            response = httpx.get(OPENAI_ENDPOINT + "/models", timeout=20, verify=context,
                                 headers={"Authorization": "Bearer " + values["API_KEY"]})
            models = [route["OPENAI_REASONING_MODEL"], route["OPENAI_PLAIN_MODEL"]]
        listed = {row.get("id") for row in (response.json().get("data") or [])} if response.status_code == 200 else set()
        return {"present": sorted(values), "status": response.status_code, "listed": all(m in listed for m in models)}
    except Exception as error:  # noqa: BLE001 - the class names the failure; the value never leaves
        return {"present": sorted(values), "status": None, "error": type(error).__name__}


def record_a1(pip_audit: str) -> dict[str, Any]:
    rc11 = _rc11()
    packages = rc11.installed_packages()
    pins = _dep()._branch_constraints()
    tooling = {"pip", "setuptools", "wheel", "uv"}
    differing = {n: [v, pins.get(n)] for n, v in packages.items() if n not in tooling and pins.get(n) != v}
    check = subprocess.run(["docker", "exec", "sage", "python3", "-m", "pip", "check", "--disable-pip-version-check"],
                           capture_output=True, text=True, timeout=120)
    with tempfile.TemporaryDirectory(prefix="sage-rc10-audit-") as scratch:
        listing = Path(scratch) / "installed.txt"
        listing.write_text("".join(f"{n}=={v}\n" for n, v in sorted(packages.items()) if n not in tooling))
        audit = subprocess.run([pip_audit, "-r", str(listing), "--no-deps", "--disable-pip", "--progress-spinner",
                                "off", "-f", "json"], capture_output=True, text=True, timeout=600)
    try:
        report = json.loads(audit.stdout)
        dependencies = report.get("dependencies", report) if isinstance(report, dict) else report
        findings = sum(len(d.get("vulns") or []) for d in dependencies if isinstance(d, dict))
        ran = len(dependencies) > 0
    except (ValueError, TypeError):
        findings, ran = None, False
    return {"packages": {n: packages.get(n) for n in EXPECTED_PACKAGES}, "count": len(packages),
            "differing_from_constraints": differing, "pip_check_returncode": check.returncode,
            "audit_ran": ran, "audit_findings": findings}


# --------------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------------

async def cmd_snapshot(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = _base()
    phoenix = Path(args.phoenix_db)
    unresolved = (await base._query(client, UNRESOLVED_ON_START_QUERY)).get("operationeventlog") or []
    if unresolved:
        raise SystemExit("an unresolved sage_on_start warning exists; resolve it in Mythic's event log first")
    evidence = evidence_dir(run)
    backup = evidence / "phoenix-before-rc10.db"
    source = sqlite3.connect(f"file:{phoenix.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    os.chmod(backup, 0o600)
    ids = phoenix_trace_ids(phoenix)
    events = (await base._query(client, MAX_EVENT_QUERY)).get("operationeventlog") or []
    run["snapshot"] = {"at": time.time(), "backup": str(backup), "backup_sha256": _sha256(backup.read_bytes()),
                       "traces": len(ids), "trace_ids_sha256": _sha256("\n".join(ids).encode()),
                       "trace_ids_file": str(evidence / "trace-ids-before.txt"),
                       "max_event_id": int(events[0]["id"]) if events else 0}
    Path(run["snapshot"]["trace_ids_file"]).write_text("\n".join(ids) + "\n")
    return run["snapshot"]


async def cmd_readiness(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, rc11, dep, split = _base(), _rc11(), _dep(), _split()
    route = read_route(Path(args.route_file))
    installed = Path(_deployment()._installed_service_dir())
    containers = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
    parity = split._run_json([str(REPO_ROOT / ".venv/bin/python"), str(_DEPLOYMENT_PATH), "--mode", "container",
                              "parity"])
    callback = (await base._query(client, CALLBACK_QUERY, cb=int(args.callback))).get("callback") or []
    profile = watcher_profile(client.current_operation_id)
    owner = (await base._query(client, CHANNEL_QUERY, id=int(profile.get("owner_channel_id") or 0))
             ).get("chat_channel_by_pk") if profile.get("owner_channel_id") else None
    bloodhound = (await base._query(client, CHANNEL_QUERY, id=int(args.bloodhound_channel))).get("chat_channel_by_pk")
    bh_config = ((bloodhound or {}).get("ai_metadata") or {}).get("config") or {}
    packages = dep.container_packages()
    repo_local = (RUNTIME / ".env.local").read_text() if (RUNTIME / ".env.local").exists() else ""
    installed_local = (installed / ".env.local").read_text() if (installed / ".env.local").exists() else ""
    secrets = await split._operator_secret_names(client)
    report = {
        "route": {key: route[key] for key in ROUTE_KEYS if key != "GATEWAY_ENDPOINT"},
        "parity": {"returncode": parity["returncode"], "ready": bool(parity["payload"].get("ready")),
                   "blockers": parity["payload"].get("blockers")},
        "snapshot_taken": bool(run.get("snapshot")),
        "sage_container_running": bool(containers and containers[0].get("container_running")),
        "open_requests": (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or [],
        "container_packages": {"differing_from_constraints": packages["differing_from_constraints"]},
        "callback_active": bool(callback and callback[0].get("active")),
        "watcher": {**profile, "owner_channel_model": (owner or {}).get("chat_model"),
                    "owner_locked": (owner or {}).get("locked"),
                    "owner_token_active": await channel_token_active(client, owner)},
        "bloodhound_channel": _mcp().channel_bloodhound(bh_config),
        "phoenix_readable": Path(args.phoenix_db).is_file(),
        "phoenix_tool_results": (phoenix_records_tool_results(Path(args.phoenix_db))
                                 if Path(args.phoenix_db).is_file() else {"span": None, "text": False}),
        "phoenix_health": http_status(args.phoenix_url.rstrip("/") + "/healthz"),
        "phoenix_settings": phoenix_settings(),
        "phoenix_listeners": ss_listeners(),
        "phoenix_graphql_unauthenticated": graphql_unauthenticated(args.phoenix_url),
        "task_baseline": await base._max_task(client),
        "credentials": {
            "installed_local": split.dotenv_state(installed_local, route["GATEWAY_ENDPOINT"]),
            "gateway_container": gateway_probe(route),
            "watcher_route": watcher_route_probe(route),
            "watcher_connect": rc11.watcher_connect(client.current_operation_id),
            "repo_blocks": block_presence(repo_local),
            "host_proofs": {block: _host_block_probe(block, block_values(repo_local, block), route)
                            for block in BLOCK_KEYS},
            "operator_secrets": secrets,
        },
        "limits": LIMITS,
        "check_sha256": _sha256(Path(__file__).read_bytes()),
    }
    blockers = []
    if not report["snapshot_taken"]:
        blockers.append("no pre-deploy snapshot in this run file")
    if not report["sage_container_running"]:
        blockers.append("Sage container not consuming")
    if report["open_requests"]:
        blockers.append("open chat requests")
    if report["container_packages"]["differing_from_constraints"]:
        blockers.append("container packages differ from this branch's constraints")
    if not report["callback_active"]:
        blockers.append("the callback is not active")
    watcher = report["watcher"]
    if not (watcher.get("profile_present") and watcher.get("paused") is True
            and watcher.get("owner_channel_model") == "Sage Watcher" and watcher.get("owner_locked")):
        blockers.append("the Watcher's stored profile is not paused in a locked Sage Watcher owner channel")
    if watcher.get("owner_token_active") is not True:
        blockers.append("the Watcher's owner channel has no active backing token, so Mythic would refuse its messages")
    bh = report["bloodhound_channel"]
    if not all(bh["settings_present"].values()) or bh["ce_status"] is None:
        blockers.append("the BloodHound channel lacks its settings or BloodHound CE is not answering")
    if not report["phoenix_readable"] or report["phoenix_health"] != 200:
        blockers.append("Phoenix unreadable or not answering /healthz")
    blockers += phoenix_tool_result_blockers(report.get("phoenix_tool_results"))
    blockers += phoenix_blockers(report["phoenix_settings"], report["phoenix_listeners"],
                                 report["phoenix_graphql_unauthenticated"])
    credentials = report["credentials"]
    blockers += gateway_blockers(credentials["installed_local"], credentials["gateway_container"], route)
    blockers += watcher_route_blockers(credentials["watcher_route"])
    blockers += rc11.watcher_connect_blockers(credentials["watcher_connect"])
    blockers += rc11.watcher_reads_blockers(credentials["watcher_connect"].get("reads"))
    for block, proof in credentials["host_proofs"].items():
        if proof.get("status") != 200 or proof.get("listed") is not True:
            blockers.append(f"the repo .env.local's # {block} block is missing or its key did not list the model")
    if not secrets.get("resolved"):
        blockers.append("the run operator's user secrets could not be resolved")
    elif set(secrets.get("names") or []) & set(SHADOWING_SECRETS):
        blockers.append("a user secret would shadow the installed provider settings")
    verdict = "deployment_required" if not report["parity"]["ready"] else ("not_ready" if blockers else "ready_for_live")
    report["blockers"], report["verdict"] = blockers, verdict
    run.setdefault("readiness_history", []).append({"at": time.time(), "verdict": verdict, "blockers": blockers})
    run["readiness"] = report
    return report


async def _on_start_after(client: Any, after_event: int) -> bool:
    rows = (await _base()._query(client, EVENT_LOG_QUERY, after=after_event)).get("operationeventlog") or []
    return bool(rows)


async def _await_ready(client: Any, after_event: int, *, phase: str, capture_record: Path | None) -> str:
    """Mythic shows Sage running and Sage answered an on-start newer than ``after_event`` (and, captured, its wire)."""
    base, split = _base(), _split()
    deadline = time.monotonic() + PHASE_READY_SECONDS
    while True:
        rows = (await base._query(client, base.nc.READINESS_QUERY)).get("consuming_container") or []
        running = bool(rows and rows[0].get("container_running"))
        answered_on_start = await _on_start_after(client, after_event)
        if capture_record is not None:
            answered_on_start = answered_on_start and split.sage_answered_on_start(split.read_wire(capture_record))
        if running and answered_on_start:
            return ""
        if time.monotonic() >= deadline:
            return f"phase {phase} container not ready"
        await asyncio.sleep(2)


async def _max_event(client: Any) -> int:
    rows = (await _base()._query(client, MAX_EVENT_QUERY)).get("operationeventlog") or []
    return int(rows[0]["id"]) if rows else 0


def _arm_capture(run: dict[str, Any]) -> None:
    """Start the wire receiver and add the marked capture block to the installed .env (the split check's mechanism)."""
    split = _split()
    capture = run["capture"]
    installed = Path(capture["installed"])
    host_dir = split._private_dir(installed / f".mythic-sdk-rc10-check-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
    container_dir = f"/Mythic/{host_dir.name}"
    evidence = Path(run["evidence_dir"])
    record, ready = evidence / "wire.jsonl", host_dir / "receiver-ready.json"
    budget = split.budget_config()
    budget["resources"]["provider_requests"]["limit"] = LIMITS["provider_calls"] + 1
    budget["resources"]["provider_tokens"]["limit"] = LIMITS["provider_tokens"] + 1
    (host_dir / "budget.json").write_text(json.dumps(budget, sort_keys=True))
    os.chmod(host_dir / "budget.json", 0o600)
    receiver = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_receive", "--record", str(record), "--parent", str(host_dir),
         "--ready", str(ready)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=open(evidence / "receiver.log", "ab"), start_new_session=True)
    deadline = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not ready.exists():
        receiver.terminate()
        raise RuntimeError("the wire receiver did not start")
    fifo = f"{container_dir}/{Path(json.loads(ready.read_text())['directory']).name}"
    block = split.env_block(container_dir, fifo)
    original_env = Path(capture["env_backup"]).read_bytes()
    capture.update(host_dir=str(host_dir), record=str(record), receiver_pid=receiver.pid, block=True)
    (installed / ".env").write_bytes(split.insert_block(original_env, block))


def _write_phase_files(run: dict[str, Any], phase: str) -> None:
    capture = run["capture"]
    installed = Path(capture["installed"])
    original_local = Path(capture["local_backup"]).read_bytes()
    local_path = installed / ".env.local"
    if phase in PHASE_BLOCK:
        values = block_values((RUNTIME / ".env.local").read_text(), PHASE_BLOCK[phase])
        local_path.write_bytes(phase_local(original_local, phase, values))
    else:
        local_path.write_bytes(original_local)
    os.chmod(local_path, int(capture.get("local_mode", 0o600)))
    if phase == "W" and not capture.get("block"):
        _arm_capture(run)


async def _switch_phase(client: Any, run: dict[str, Any], phase: str, route: dict[str, str]) -> str:
    """Write the phase's installed files, recreate the container, and prove the phase's credential from inside."""
    split = _split()
    after_event = await _max_event(client)
    _write_phase_files(run, phase)
    started = _deployment()._start_container()
    record = Path(run["capture"]["record"]) if phase in CAPTURE_PHASES else None
    failure = await _await_ready(client, after_event, phase=phase, capture_record=record)
    local_text = (Path(run["capture"]["installed"]) / ".env.local").read_text()
    entry: dict[str, Any] = {"phase": phase, "returncode": started.get("returncode"), "ready": not failure}
    if phase in ("A", "B"):
        model = route["ANTHROPIC_MODEL"] if phase == "A" else route["BEDROCK_MODEL"]
        probe = phase_probe(phase, model)
        entry.update(probe=probe, blockers=phase_file_blockers(phase, local_text) + phase_probe_blockers(phase, probe))
    elif phase == "O":
        probe = split._container_probe(2, route)
        entry.update(probe=probe, blockers=split.phase_blockers(2, split.dotenv_state(local_text), probe, route))
    else:
        probe = split._container_probe(1, route)
        entry.update(probe=probe, blockers=split.phase_blockers(
            1, split.dotenv_state(local_text, route["GATEWAY_ENDPOINT"]), probe, route))
    run.setdefault("phases", []).append(entry)
    if started.get("returncode") != 0:
        return f"phase {phase} container did not restart"
    if failure:
        return failure
    return f"phase {phase} credential: {'; '.join(entry['blockers'])}" if entry["blockers"] else ""


async def _send(client: Any, run: dict[str, Any], name: str, channel_id: int, prompt: str, *, cards: bool,
                timeout: int) -> dict[str, Any]:
    nc = _base().nc
    entry: dict[str, Any] = {"channel_id": channel_id, "sent_at": time.time(), "sent_ns": time.time_ns(), "sends": 1}
    message = await nc.create_message(client, channel_id, prompt)
    entry["request_id"] = int(message["chat_request_id"])
    run["requests"][name] = entry
    if cards:
        entry.update(cards_seen=0, cards_accepted=0, cards_rejected=0)
    deadline = time.monotonic() + timeout
    try:
        while True:
            result = await nc.wait_for_request(client, entry["request_id"],
                                               timeout_seconds=max(1.0, deadline - time.monotonic()),
                                               stop_on_input_requested=cards)
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
    entry["finished_at"] = time.time()
    return entry


def measured(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The steps that carry a Phoenix window (sent, and recorded after the send); verify reads spans for these only."""
    return {name: info for name, info in (run.get("requests") or {}).items()
            if "request_id" in info and "phoenix_after" in info and "phoenix_until" in info}


def _totals(run: dict[str, Any], phoenix: Path) -> dict[str, Any]:
    """Provider calls and tokens so far: captured wire in phases W and O, LLM spans elsewhere."""
    split, dep = _split(), _dep()
    capture = run.get("capture") or run.get("capture_released") or {}
    wire_steps = {name: info for name, info in measured(run).items() if name in WIRE_STEPS}
    calls = tokens = 0
    if capture.get("record") and Path(capture["record"]).exists() and wire_steps:
        everything = split.exchanges(split.read_wire(Path(capture["record"])))
        calls += len(everything)
        tokens += sum(split.usage_tokens(item) for item in everything)
    for name, info in measured(run).items():
        if name in WIRE_STEPS:
            continue
        llm = dep._llm(dep.spans(phoenix, int(info["phoenix_after"]), int(info["phoenix_until"])))
        calls += len(llm)
        tokens += sum(s["prompt_tokens"] + s["completion_tokens"] for s in llm)
    return {"provider_calls": calls, "provider_tokens": tokens}


async def cmd_cleanup(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base, split = _base(), _split()
    actions: dict[str, Any] = {}
    channel_ids = {int(cid) for cid in (run.get("channels") or {}).values()}
    for row in (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or []:
        if int(row.get("channel_id") or 0) in channel_ids:
            await base.nc.mythic.execute_custom_query(client, base.nc.CANCEL_REQUEST_MUTATION,
                                                      variables={"request_id": int(row["id"])})
            actions.setdefault("cancelled", []).append(int(row["id"]))
    if (run.get("channels") or {}).get("H"):
        actions["bloodhound_settings_cleared"] = await clear_channel_bloodhound(client, int(run["channels"]["H"]))
    capture = run.get("capture") or {}
    if capture:
        installed = Path(capture["installed"])
        original_env, original_local = Path(capture["env_backup"]).read_bytes(), Path(capture["local_backup"]).read_bytes()
        if _sha256(original_env) != capture["env_original_sha256"] or _sha256(original_local) != capture[
                "local_original_sha256"]:
            raise SystemExit("a saved backup does not match its recorded hash; restore the installed files by hand")
        (installed / ".env").write_bytes(original_env)
        (installed / ".env.local").write_bytes(original_local)
        os.chmod(installed / ".env.local", int(capture.get("local_mode", 0o600)))
        after_event = await _max_event(client)
        actions["start"] = _deployment()._start_container().get("returncode")
        actions["ready"] = not await _await_ready(client, after_event, phase="cleanup", capture_record=None)
        if capture.get("receiver_pid"):
            (Path(capture["host_dir"]) / "stop").write_text("")
            pid = int(capture["receiver_pid"])
            deadline = time.monotonic() + 15
            while split._pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.5)
            if split._pid_alive(pid):
                os.kill(pid, 15)
            actions["receiver_stopped"] = not split._pid_alive(pid)
        run["files_after_cleanup"] = {
            "env_restored": _sha256((installed / ".env").read_bytes()) == capture["env_original_sha256"],
            "env_local_restored": _sha256((installed / ".env.local").read_bytes()) == capture["local_original_sha256"]}
        if all(run["files_after_cleanup"].values()):
            # The backups hold the live provider keys; once both restores match their hashes they are not needed.
            for backup in (capture["env_backup"], capture["local_backup"]):
                Path(backup).unlink(missing_ok=True)
            run["files_after_cleanup"]["backups_removed"] = True
        if all(run["files_after_cleanup"].get(k) for k in ("env_restored", "env_local_restored")) and actions.get(
                "receiver_stopped", True):
            run["capture_released"] = run.pop("capture")
    if args is not None and getattr(args, "phoenix_url", None):
        run.setdefault("p1", {}).setdefault("healthz", []).append(
            http_status(args.phoenix_url.rstrip("/") + "/healthz"))
    profile = watcher_profile(client.current_operation_id)
    repause = await repause_watcher(client, profile)
    if repause is not None:
        actions["watcher_repause"] = repause
        profile = watcher_profile(client.current_operation_id)
    actions["watcher_after"] = profile
    actions["open_requests_after"] = (await base._query(client, base.OPEN_REQUESTS_QUERY)).get("chat_request") or []
    run["cleanup"] = {**actions, "at": time.time()}
    return run["cleanup"]


async def cmd_run(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if (run.get("readiness") or {}).get("verdict") != "ready_for_live":
        raise SystemExit("readiness has not reported ready_for_live in this run file")
    if run.get("requests"):
        raise SystemExit("this run file already holds an attempt; a new attempt needs a new run file")
    gate = subprocess.run(["sage-line", "check-attempt", args.plan], capture_output=True, text=True)
    if gate.returncode != 0:
        raise SystemExit(f"sage-line check-attempt refused: {gate.stdout.strip()} {gate.stderr.strip()}".strip())
    base = _base()
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    installed = Path(_deployment()._installed_service_dir())
    evidence = evidence_dir(run)
    backups = {}
    for name in (".env", ".env.local"):
        data = (installed / name).read_bytes()
        backup = evidence / f"installed{name}.original"
        backup.write_bytes(data)
        os.chmod(backup, 0o600)
        backups[name] = (str(backup), _sha256(data))
    run["capture"] = {"installed": str(installed), "env_backup": backups[".env"][0],
                      "env_original_sha256": backups[".env"][1], "local_backup": backups[".env.local"][0],
                      "local_original_sha256": backups[".env.local"][1],
                      "local_mode": stat.S_IMODE((installed / ".env.local").stat().st_mode)}
    run.update(authorized_by=args.authorized_by, callback=int(args.callback),
               bloodhound_channel=int(args.bloodhound_channel), started_at=time.time(),
               task_baseline=await base._max_task(client), phoenix_start=base.phoenix_mark(phoenix),
               requests={}, channels={}, phases=[], a1=record_a1(args.pip_audit),
               p1={"listeners": ss_listeners(), "graphql_unauthenticated": graphql_unauthenticated(args.phoenix_url),
                   "healthz": [http_status(args.phoenix_url.rstrip("/") + "/healthz")]})
    profile = watcher_profile(client.current_operation_id)
    source = (await base._query(client, CHANNEL_QUERY, id=int(args.bloodhound_channel))).get("chat_channel_by_pk")
    bloodhound = {k: str((((source or {}).get("ai_metadata") or {}).get("config") or {}).get(k) or "")
                  for k in BLOODHOUND_KEYS}
    started = time.monotonic()
    phase = "N"
    try:
        for name, step_phase, key, prompt, cards, _backed in STEPS:
            if step_phase != phase:
                if phase == "N":
                    run["p1"]["healthz"].append(http_status(args.phoenix_url.rstrip("/") + "/healthz"))
                failure = await _switch_phase(client, run, step_phase, route)
                phase = step_phase
                if failure:
                    run["stopped"] = {"step": name, "reason": failure}
                    break
            mark = base.phoenix_mark(phoenix)
            timeout = WHOAMI_TIMEOUT if cards else (SLASH_TIMEOUT if prompt.startswith("/") else STEP_TIMEOUT)
            try:
                if key == "WATCHER":
                    channel_id = int(profile["owner_channel_id"])
                else:
                    if key not in run["channels"]:
                        created = await base.nc.create_locked_channel(
                            client, name=f"rc10 live check {key}", description="Sage Mythic SDK rc10 live check",
                            metadata=channel_config(route, key, bloodhound if key == "H" else None))
                        run["channels"][key] = int(created["chat_channel_id"])
                    channel_id = run["channels"][key]
                await _send(client, run, name, channel_id, prompt.format(callback=int(args.callback)),
                            cards=cards, timeout=timeout)
            except Exception as error:  # noqa: BLE001 - the check's own failure is recorded, not raised past cleanup
                # `_send` files the request only once Mythic has accepted it: a step absent from `requests` was never
                # sent; one present was sent and is judged from Mythic's rows.
                if name in run["requests"]:
                    run["requests"][name]["check_error"] = type(error).__name__
                else:
                    run["unsent_error"] = {"step": name, "error": type(error).__name__}
                run["stopped"] = {"step": name, "reason": f"check error: {type(error).__name__}"}
            entry = run["requests"].get(name)
            if entry is None:
                break
            try:
                entry.update(phase=step_phase, phoenix_after=mark, phoenix_until=base.phoenix_mark(phoenix),
                             tasks_after=await base._max_task(client))
                totals = _totals(run, phoenix)
                tasks = (await base._query(client, TASKS_AFTER_QUERY, after=run["task_baseline"])).get("task") or []
            except Exception as error:  # noqa: BLE001
                entry.setdefault("check_error", type(error).__name__)
                run["stopped"] = run.get("stopped") or {"step": name, "reason": f"check error: {type(error).__name__}"}
                break
            if run.get("stopped"):
                break
            if len(tasks) > LIMITS["target_tasks"] or (tasks and "M2" not in run["requests"]):
                run["stopped"] = {"step": name, "reason": "an unexpected target task"}
            elif totals["provider_calls"] > LIMITS["provider_calls"]:
                run["stopped"] = {"step": name, "reason": "provider call limit"}
            elif totals["provider_tokens"] > LIMITS["provider_tokens"]:
                run["stopped"] = {"step": name, "reason": "provider token limit"}
            elif time.monotonic() - started > LIMITS["active_seconds"]:
                run["stopped"] = {"step": name, "reason": "active time limit"}
            elif entry.get("timeout"):
                run["stopped"] = {"step": name, "reason": entry["timeout"]}
            if run.get("stopped"):
                break
    finally:
        run["finished_at"] = time.time()
        run["active_seconds"] = round(time.monotonic() - started, 1)
        run["phoenix_end"] = base.phoenix_mark(phoenix)
        run["cleanup_result"] = await cmd_cleanup(client, run, args)
    return {"channels": run["channels"], "requests": {n: {k: r.get(k) for k in ("request_id", "timeout", "cards_seen")}
                                                       for n, r in run["requests"].items()},
            "stopped": run.get("stopped"), "phases": [{k: p.get(k) for k in ("phase", "returncode", "ready", "blockers")}
                                                      for p in run["phases"]],
            "cleanup": run["cleanup_result"]}


async def cmd_verify(client: Any, run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if run.get("verdict"):
        raise SystemExit("this run was already read; a result is read once")
    base, split, dep = _base(), _split(), _dep()
    route = read_route(Path(args.route_file))
    phoenix = Path(args.phoenix_db)
    rows = {}
    for name, info in (run.get("requests") or {}).items():
        if "request_id" not in info:  # the check's own error before the request was sent
            continue
        data = await base._query(client, REQUEST_ROWS_QUERY, rid=int(info["request_id"]))
        rows[name] = {"messages": data.get("chat_message") or [],
                      "status": (data.get("chat_request_by_pk") or {}).get("status")}
    capture = run.get("capture") or run.get("capture_released") or {}
    windows = measured(run)
    wire: dict[str, list[dict[str, Any]]] = {}
    if capture.get("record") and Path(capture["record"]).exists():
        steps = {n: i for n, i in windows.items() if n in WIRE_STEPS and "sent_ns" in i and "request_id" in i}
        wire, _unattributed = split.attribute(split.read_wire(Path(capture["record"])), steps)
    spans = {name: dep.spans(phoenix, int(info["phoenix_after"]), int(info["phoenix_until"]))
             for name, info in windows.items()}
    thinking = {name: thinking_blocks(phoenix, int(info["phoenix_after"]), int(info["phoenix_until"]))
                for name, info in windows.items() if name in ("A1n_1", "B1_1")}
    generations = {name: m2_generations(phoenix, int(info["phoenix_after"]), int(info["phoenix_until"]))
                   for name, info in windows.items() if name == "M2"}
    tasks = (await base._query(client, TASKS_AFTER_QUERY, after=int(run["task_baseline"]))).get("task") or []
    for task in tasks:  # read after the request ended (T2): a partial wait then differs from Mythic's rows
        if type(task.get("id")) is int:
            subtasks = (await base._query(client, SUBTASKS_QUERY, task_id=task["id"])).get("task")
            task["subtask_count"] = len(subtasks) if isinstance(subtasks, list) else None
    formatted = {}
    if "M2" in windows:
        info = windows["M2"]
        call_ids = [call for span in generations.get("M2") or [] for call in span.get("task_call_ids") or []]
        formatted["M2"] = m2_formatted(phoenix, int(info["phoenix_after"]), int(info["phoenix_until"]), call_ids)
    before = set(Path(run["snapshot"]["trace_ids_file"]).read_text().split())
    live = set(phoenix_trace_ids(phoenix))
    events = (await base._query(client, EVENT_LOG_QUERY, after=int(run["snapshot"]["max_event_id"]))
              ).get("operationeventlog") or []
    observed = {
        "rows": rows, "wire": wire, "spans": spans, "generations": generations, "thinking": thinking, "tasks": tasks,
        "formatted": formatted, "traces": {"snapshot": len(before), "missing": len(before - live)},
        "notice": notice_events(events, [SHIPPED_PASSWORD, *container_phoenix_secrets()]),
        "watcher_paused": ((run.get("cleanup") or {}).get("watcher_after") or {}).get("paused"),
        "totals": _totals(run, phoenix),
    }
    result = evaluate(run, observed, route)
    run["verdict"] = result
    run["verified_at"] = time.time()
    return result


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

async def _main(args: argparse.Namespace) -> int:
    run_file = Path(args.run_file)
    run = _load(run_file)
    client = await _base().nc.login()
    handlers = {"snapshot": cmd_snapshot, "readiness": cmd_readiness, "run": cmd_run, "cleanup": cmd_cleanup,
                "verify": cmd_verify}
    try:
        result = await handlers[args.command](client, run, args)
    finally:
        _save(run_file, run)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "readiness", "run", "cleanup", "verify", "_receive"):
        command = sub.add_parser(name)
        if name == "_receive":
            command.add_argument("--record", required=True)
            command.add_argument("--parent", required=True)
            command.add_argument("--ready", required=True)
            continue
        command.add_argument("--run-file", required=True)
        if name in ("snapshot", "readiness", "run", "verify"):
            command.add_argument("--phoenix-db", required=True)
        if name in ("readiness", "run", "verify"):
            command.add_argument("--route-file", required=True)
        if name in ("readiness", "run"):
            command.add_argument("--callback", required=True, type=int)
            command.add_argument("--bloodhound-channel", required=True, type=int)
        if name in ("readiness", "run", "cleanup"):
            command.add_argument("--phoenix-url", default="http://127.0.0.1:6006")
        if name == "run":
            command.add_argument("--plan", required=True)
            command.add_argument("--authorized-by", required=True)
            command.add_argument("--pip-audit", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "_receive":
        raise SystemExit(_split()._serve(Path(args.record), Path(args.parent), Path(args.ready)))
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
