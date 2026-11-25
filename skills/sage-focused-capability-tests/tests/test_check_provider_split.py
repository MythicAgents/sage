"""Offline checks for the provider-split live check's local logic (no Mythic, no provider).

The wire records and Mythic rows below are synthetic, shaped like the receiver's records and Mythic's rows; they prove
the pass rules accept a correct set and reject each falsifier. The attribution and send-gate tests replay a fixture
reduced from live attempt 1's real capture (see its provenance field); the binding rows are recorded lab rows. The settings rule is read against Sage's real model
definitions, and the capture test drives Sage's real model factory, metered transport, observation channel and the
check's receiver for both APIs over a local stand-in provider.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_provider_split.py"
SPEC = importlib.util.spec_from_file_location("check_provider_split", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))

GATEWAY = "https://gateway.invalid:7443"
ROUTE = {"GATEWAY_ENDPOINT": GATEWAY, "GATEWAY_MODEL": "bedrock-claude-4-6-sonnet",
         "OPENAI_REASONING_MODEL": "gpt-5.6-sol", "OPENAI_PLAIN_MODEL": "gpt-4.1"}
CALLBACKS = json.dumps([{"display_id": 5, "host": "WORKSTATION-5", "user": "lab.user", "integrity": 2}])


def _b64(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


class Wire:
    """Synthetic receiver records, one request/response pair at a time."""

    def __init__(self):
        self.records: list[dict] = []

    def add(self, url: str, body: dict, reply: dict, status: int = 200) -> None:
        request_id = f"r{len(self.records)}"
        self.records.append({"kind": "model_request", "record": {"payload": {
            "request_id": request_id, "url": url, "body_base64": _b64(body)}}})
        self.records.append({"kind": "model_response", "record": {"payload": {
            "request_id": request_id, "status_code": status, "body_base64": _b64(reply), "outcome": "returned"}}})


def _responses_body(model, items):
    return {"model": model, "input": items, "store": False, "include": ["reasoning.encrypted_content"],
            "access_programs": {"cyber": "standard"}, "tools": [{"type": "function", "name": "list_callbacks"}]}


def _responses_reply(output, total=900):
    return {"object": "response", "status": "completed", "output": output, "usage": {"total_tokens": total}}


def _cc_body(model, messages):
    return {"model": model, "messages": messages, "tools": [{"type": "function",
                                                             "function": {"name": "list_callbacks"}}]}


def _cc_reply(message, total=700):
    return {"object": "chat.completion", "choices": [{"index": 0, "message": message}],
            "usage": {"total_tokens": total}}


def _cc_call(call_id):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "list_callbacks", "arguments": "{}"}}]}


def _passing():
    """Records, marks, rows and run of a set that meets every rule."""
    wire = Wire()
    marks = {}
    gateway_url, openai_url = GATEWAY + "/chat/completions", check.OPENAI_ENDPOINT + "/responses"
    model = ROUTE["GATEWAY_MODEL"]

    def mark(step, start):
        marks[step] = (start, len(wire.records))

    # L1: gateway, call then answer
    start = len(wire.records)
    l1_user = [{"role": "user", "content": check.LIST}]
    wire.add(gateway_url, _cc_body(model, l1_user), _cc_reply(_cc_call("tooluse_L1")))
    l1_history = l1_user + [_cc_call("tooluse_L1"), {"role": "tool", "tool_call_id": "tooluse_L1",
                                                      "content": CALLBACKS}]
    wire.add(gateway_url, _cc_body(model, l1_history), _cc_reply({"role": "assistant", "content": "One callback."}))
    mark("L1", start)
    # M1: openai provider on the gateway
    start = len(wire.records)
    wire.add(gateway_url, _cc_body(model, l1_user), _cc_reply(_cc_call("tooluse_M1")))
    wire.add(gateway_url, _cc_body(model, l1_user + [_cc_call("tooluse_M1"), {
        "role": "tool", "tool_call_id": "tooluse_M1", "content": CALLBACKS}]),
        _cc_reply({"role": "assistant", "content": "One callback."}))
    mark("M1", start)
    # O1: Responses with reasoning
    start = len(wire.records)
    o_user = [{"role": "user", "content": check.LIST}]
    reasoning = {"type": "reasoning", "id": "rs_o1", "summary": [], "encrypted_content": "opaque-o1"}
    call = {"type": "function_call", "id": "fc_o1", "call_id": "call_o1", "name": "list_callbacks", "arguments": "{}"}
    wire.add(openai_url, _responses_body("gpt-5.6-sol", o_user), _responses_reply([reasoning, call]))
    o_history = o_user + [reasoning, call, {"type": "function_call_output", "call_id": "call_o1",
                                            "output": CALLBACKS}]
    answer = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One callback."}]}
    wire.add(openai_url, _responses_body("gpt-5.6-sol", o_history), _responses_reply([answer]))
    mark("O1", start)
    # O2: first request replays O1's reasoning, call and result
    start = len(wire.records)
    o2_input = o_history + [{"role": "assistant", "content": "One callback."},
                            {"role": "user", "content": check.INTEGRITY}]
    wire.add(openai_url, _responses_body("gpt-5.6-sol", o2_input), _responses_reply([answer]))
    mark("O2", start)
    # G1: non-reasoning model on Responses
    start = len(wire.records)
    g_call = {"type": "function_call", "id": "fc_g1", "call_id": "call_g1", "name": "list_callbacks",
              "arguments": "{}"}
    wire.add(openai_url, _responses_body("gpt-4.1", o_user), _responses_reply([g_call]))
    wire.add(openai_url, _responses_body("gpt-4.1", o_user + [g_call, {"type": "function_call_output",
                                                                    "call_id": "call_g1", "output": CALLBACKS}]),
             _responses_reply([answer]))
    mark("G1", start)
    # L2: after two restarts, replays L1's call and result
    start = len(wire.records)
    wire.add(gateway_url, _cc_body(model, l1_history + [{"role": "assistant", "content": "One callback."},
                                                        {"role": "user", "content": check.INTEGRITY}]),
             _cc_reply({"role": "assistant", "content": "Callback 5, integrity 2."}))
    mark("L2", start)

    rows = {step: {"status": "complete", "messages": [{"message": "An answer.", "metadata": {}}]} for step in marks}
    run = {"steps": {step: {"tasks_after": 18} for step in marks}, "task_baseline": 18,
           "channels": {"L": 1, "M": 2, "O": 3, "G": 4}, "settings_error": "", "active_seconds": 600.0,
           "recreations": [{"phase": 2, "returncode": 0, "process_start": True},
                           {"phase": 3, "returncode": 0, "process_start": True}],
           "files_after_cleanup": {"env_local_restored": True, "env_restored": True}}
    return wire.records, marks, rows, run


def _evaluate(records, marks, rows, run, stored=None, bindings=None):
    wire = {step: check.exchanges(records[a * 1:b]) for step, (a, b) in marks.items()}
    items = check.exchanges(records)
    totals = {"provider_calls": len(items), "provider_tokens": sum(check.usage_tokens(i) for i in items),
              "unpaired": 0, "active_seconds": run.get("active_seconds")}
    return check.evaluate(run, wire, rows, totals, ROUTE, stored or {"L": "litellm"},
                          bindings if bindings is not None else _bindings(), LAB_OPERATION)


def _body(records, marks, step, index, side="request"):
    kind = "model_request" if side == "request" else "model_response"
    found = [r for r in records[marks[step][0]:marks[step][1]] if r["kind"] == kind][index]
    return found


def _rewrite(record, mutate):
    payload = record["record"]["payload"]
    body = json.loads(base64.b64decode(payload["body_base64"]))
    mutate(body)
    payload["body_base64"] = _b64(body)


def test_a_correct_set_passes_every_rule():
    result = _evaluate(*_passing())
    assert result["verdict"] == "pass", result
    assert all(result["criteria"].values())
    assert result["detail"]["P3_stateless_replay"]["reasoning_replayed"] == 1


@pytest.mark.parametrize(("step", "mutate"), [
    pytest.param("O1", lambda b: b.pop("store"), id="responses-without-store"),
    pytest.param("G1", lambda b: b.update(include=[]), id="responses-without-encrypted-reasoning"),
    pytest.param("O2", lambda b: b.update(access_programs={"cyber": "daybreak_red"}), id="daybreak-not-off"),
    pytest.param("L1", lambda b: b.update(store=False), id="chat-completions-with-store"),
    pytest.param("M1", lambda b: b.update(access_programs={"cyber": "standard"}), id="chat-completions-daybreak"),
    pytest.param("M1", lambda b: b.update(model="gpt-4.1"), id="wrong-model"),
])
def test_a_wrong_projection_or_model_fails_the_route(step, mutate):
    records, marks, rows, run = copy.deepcopy(_passing())
    _rewrite(_body(records, marks, step, 0), mutate)
    result = _evaluate(records, marks, rows, run)
    assert result["criteria"]["P1_route"] is False
    assert result["detail"]["P1_route"][step] != "ok"


@pytest.mark.parametrize(("step", "url"), [
    pytest.param("O1", GATEWAY + "/chat/completions", id="openai-default-sent-chat-completions"),
    pytest.param("L1", GATEWAY + "/responses", id="gateway-sent-responses"),
    pytest.param("M1", check.OPENAI_ENDPOINT + "/chat/completions", id="gateway-key-to-openai"),
])
def test_a_wrong_api_or_host_fails_the_route(step, url):
    records, marks, rows, run = copy.deepcopy(_passing())
    _body(records, marks, step, 0)["record"]["payload"]["url"] = url
    assert _evaluate(records, marks, rows, run)["criteria"]["P1_route"] is False


def test_a_non_2xx_response_fails_the_route_and_the_model_rule():
    records, marks, rows, run = copy.deepcopy(_passing())
    _body(records, marks, "G1", 1, "response")["record"]["payload"]["status_code"] = 400
    result = _evaluate(records, marks, rows, run)
    assert result["criteria"]["P1_route"] is False and result["criteria"]["P4_non_reasoning_model"] is False


@pytest.mark.parametrize("gate", ["L1", "O1"])
def test_no_tool_call_in_a_gate_step_means_the_check_did_not_run(gate):
    records, marks, rows, run = copy.deepcopy(_passing())
    for index in range(2):
        response = _body(records, marks, gate, index, "response")
        if gate == "L1":
            _rewrite(response, lambda b: b["choices"][0]["message"].pop("tool_calls", None))
        else:
            _rewrite(response, lambda b: b.update(output=[o for o in b["output"] if o["type"] != "function_call"]))
    assert _evaluate(records, marks, rows, run)["verdict"] == "not_run"


def test_a_failed_phase_proof_means_the_check_did_not_run():
    records, marks, rows, run = copy.deepcopy(_passing())
    run["stopped"] = {"step": "O1", "reason": "phase 2 credential: keyed model listing did not return 200"}
    assert _evaluate(records, marks, rows, run)["verdict"] == "not_run"


def test_a_result_that_never_comes_back_fails_the_round_trip():
    records, marks, rows, run = copy.deepcopy(_passing())
    _rewrite(_body(records, marks, "O1", 1), lambda b: b.update(input=[i for i in b["input"]
                                                                      if i.get("type") != "function_call_output"]))
    assert _evaluate(records, marks, rows, run)["criteria"]["P2_round_trip"] is False


def test_a_step_without_a_native_answer_fails():
    records, marks, rows, run = copy.deepcopy(_passing())
    rows["G1"] = {"status": "error", "messages": [{"message": "The model provider rejected this request",
                                                   "metadata": {}}]}
    rows["M1"] = {"status": "complete", "messages": [{"message": "", "metadata": {"tool_use": {}}}]}
    result = _evaluate(records, marks, rows, run)
    assert result["criteria"]["P4_non_reasoning_model"] is False
    assert result["criteria"]["P5_migration"] is False


@pytest.mark.parametrize("drop", ["reasoning", "function_call", "function_call_output"])
def test_o2_missing_o1s_reasoning_call_or_result_fails_stateless_replay(drop):
    records, marks, rows, run = copy.deepcopy(_passing())
    _rewrite(_body(records, marks, "O2", 0), lambda b: b.update(input=[i for i in b["input"]
                                                                      if i.get("type") != drop]))
    assert _evaluate(records, marks, rows, run)["criteria"]["P3_stateless_replay"] is False


def test_a_changed_reasoning_payload_fails_stateless_replay():
    records, marks, rows, run = copy.deepcopy(_passing())

    def change(body):
        for item in body["input"]:
            if item.get("type") == "reasoning":
                item["encrypted_content"] = "different"
    _rewrite(_body(records, marks, "O2", 0), change)
    assert _evaluate(records, marks, rows, run)["criteria"]["P3_stateless_replay"] is False


def test_o1_without_encrypted_reasoning_fails_stateless_replay():
    records, marks, rows, run = copy.deepcopy(_passing())
    _rewrite(_body(records, marks, "O1", 0, "response"),
             lambda b: b.update(output=[o for o in b["output"] if o["type"] != "reasoning"]))
    assert _evaluate(records, marks, rows, run)["criteria"]["P3_stateless_replay"] is False


@pytest.mark.parametrize("change", ["lost-history", "provider-changed", "one-restart", "changed-result"])
def test_restart_rule_needs_two_restarts_the_stored_provider_and_l1s_pair(change):
    records, marks, rows, run = copy.deepcopy(_passing())
    stored = {"L": "litellm"}
    if change == "lost-history":
        _rewrite(_body(records, marks, "L2", 0), lambda b: b.update(messages=b["messages"][-1:]))
    elif change == "provider-changed":
        stored = {"L": "openai"}
    elif change == "one-restart":
        run["recreations"] = run["recreations"][:1]
    else:
        def mutate(body):
            for message in body["messages"]:
                if message.get("role") == "tool":
                    message["content"] = "[]"
        _rewrite(_body(records, marks, "L2", 0), mutate)
    assert _evaluate(records, marks, rows, run, stored)["criteria"]["P6_restart"] is False


@pytest.mark.parametrize("change", ["target-task", "tokens", "calls", "env-local", "env", "stopped", "minutes",
                                    "missing-step"])
def test_containment_rejects_each_breach(change):
    records, marks, rows, run = copy.deepcopy(_passing())
    if change == "target-task":
        run["steps"]["G1"]["tasks_after"] = 19
    elif change == "tokens":
        _rewrite(_body(records, marks, "O1", 0, "response"), lambda b: b.update(usage={"total_tokens": 400_001}))
    elif change == "calls":
        for _ in range(30):
            records += records[marks["L2"][0]:marks["L2"][1]]
    elif change == "env-local":
        run["files_after_cleanup"]["env_local_restored"] = False
    elif change == "env":
        run["files_after_cleanup"]["env_restored"] = False
    elif change == "stopped":
        run["stopped"] = {"step": "G1", "reason": "provider call limit"}
    elif change == "minutes":
        run["active_seconds"] = 30 * 60 + 1
    else:
        run["steps"].pop("L2")
    assert _evaluate(records, marks, rows, run)["criteria"]["P8_containment"] is False


def test_a_settings_error_fails_p7():
    records, marks, rows, run = copy.deepcopy(_passing())
    run["settings_error"] = "Sage provider choices are []"
    assert _evaluate(records, marks, rows, run)["criteria"]["P7_settings"] is False


def test_sse_bodies_are_read_like_json_for_both_apis():
    chunks = [{"object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "tooluse_x", "function": {"name": "list_", "arguments": "{"}}]}}]},
              {"object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
                  {"index": 0, "function": {"name": "callbacks", "arguments": "}"}}]}}], "usage": {"total_tokens": 9}}]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    body = check._body(base64.b64encode(sse.encode()).decode())
    assert check.produced_calls({"api": "chat_completions", "reply": body}) == [
        {"call_id": "tooluse_x", "name": "list_callbacks", "arguments": "{}"}]
    assert check.usage_tokens({"reply": body}) == 9
    done = {"type": "response.completed", "response": _responses_reply([{"type": "function_call", "call_id": "c",
                                                                        "name": "n", "arguments": "{}"}])}
    body = check._body(base64.b64encode(f"event: response.completed\ndata: {json.dumps(done)}\n\n".encode()).decode())
    assert check.produced_calls({"api": "responses", "reply": body})[0]["call_id"] == "c"


# --------------------------------------------------------------------------------------------------
# Route file, dotenv phases, capture block and budget
# --------------------------------------------------------------------------------------------------

def _route(tmp_path, body, mode=0o600):
    path = tmp_path / "route"
    path.write_text(body)
    path.chmod(mode)
    return path


def test_the_route_file_is_owner_only_keyless_and_exact(tmp_path):
    good = "".join(f"{k}={v}\n" for k, v in ROUTE.items())
    assert check.read_route(_route(tmp_path, good)) == ROUTE
    for body, mode in ((good, 0o644), (good + "API_KEY=x\n", 0o600),
                       (good.replace("gpt-4.1", "gpt-4o"), 0o600),
                       (good.replace(GATEWAY, "http://gateway.invalid"), 0o600),
                       (good.replace(GATEWAY, "https://api.openai.com/v1"), 0o600),
                       (good.replace("GATEWAY_MODEL=bedrock-claude-4-6-sonnet\n", ""), 0o600)):
        with pytest.raises(SystemExit):
            check.read_route(_route(tmp_path, body, mode))


def test_no_channel_carries_a_key_an_endpoint_or_an_api():
    for channel in check.CHANNELS:
        config = check.channel_config(ROUTE, channel)["config"]
        assert not set(config) & {"API_KEY", "API_ENDPOINT", "SAGE_OPENAI_API", "SAGE_OPENAI_ACCESS_PROGRAM"}
        assert config["mode"] == "conversation" and config["max_steps"] == check.MAX_STEPS
    assert check.channel_config(ROUTE, "L")["config"]["provider"] == "litellm"
    assert check.channel_config(ROUTE, "M")["config"]["provider"] == "openai"
    assert check.channel_config(ROUTE, "O")["config"]["SAGE_REASONING_EFFORT"] == "low"
    assert "SAGE_REASONING_EFFORT" not in check.channel_config(ROUTE, "G")["config"]


INSTALLED_LOCAL = (
    "SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS=1\n# LiteLLM\nprovider=openai\nmodel=bedrock-claude-4-6-sonnet\n"
    f"API_ENDPOINT={GATEWAY}\nAPI_KEY=gateway-placeholder\nSAGE_WATCHER_PROVIDER=openai\n"
    f"SAGE_WATCHER_API_ENDPOINT={GATEWAY}\nSAGE_WATCHER_API_KEY=watcher-placeholder\n"
)
REPO_LOCAL = (
    "# Header text\n#\n# LiteLLM\nprovider=openai\nAPI_KEY=gateway-placeholder\n# OpenRouter\n#API_KEY=other-placeholder\n"
    "# OpenAI\n#provider=openai\n#model=gpt-5.5\n#API_ENDPOINT=https://api.openai.com/v1\n#API_KEY=openai-placeholder\n"
    "# LM Studio\n#API_KEY=lm-placeholder\n"
)


def test_the_openai_blocks_key_is_found_and_only_that_one():
    assert check.openai_block_key(REPO_LOCAL) == "openai-placeholder"
    with pytest.raises(SystemExit):
        check.openai_block_key(REPO_LOCAL.replace("#API_KEY=openai-placeholder\n", ""))


def test_phase_two_makes_the_openai_block_the_only_active_provider_and_restores_exactly():
    original = INSTALLED_LOCAL.encode()
    phase1 = check.dotenv_state(INSTALLED_LOCAL, GATEWAY)
    assert phase1["credentials"] == [{"name": "API_KEY", "block": "LiteLLM"}]
    assert phase1["endpoints"] == ["API_ENDPOINT"] and phase1["api_endpoint_is_gateway"]
    written = check.phase2_local(original, "openai-placeholder")
    phase2 = check.dotenv_state(written.decode(), GATEWAY)
    assert phase2["credentials"] == [{"name": "API_KEY", "block": check.PHASE2_HEADER.lstrip("#").strip()}]
    assert phase2["endpoints"] == [] and phase2["provider_keys"] == ["API_KEY"]
    assert "SAGE_WATCHER_API_KEY=watcher-placeholder" in written.decode()  # the Watcher's own block is untouched
    good_probe = {"container_env": [], "source": ".env.local:API_KEY", "status": 200, "tls_verified": True,
                  "endpoints_after_dotenv": [], "models": {"gpt-5.6-sol": True, "gpt-4.1": True}}
    assert check.phase_blockers(2, phase2, good_probe, ROUTE) == []
    assert check.phase_blockers(2, phase1, good_probe, ROUTE)
    assert check.phase_blockers(2, phase2, {**good_probe, "endpoints_after_dotenv": ["OPENAI_BASE_URL"]}, ROUTE)
    assert check.phase_blockers(2, phase2, {**good_probe, "models": {"gpt-5.6-sol": True, "gpt-4.1": False}}, ROUTE)
    gateway_probe = {**good_probe, "endpoints_after_dotenv": ["API_ENDPOINT"],
                     "models": {"bedrock-claude-4-6-sonnet": True}}
    assert check.phase_blockers(1, phase1, gateway_probe, ROUTE) == []
    assert check.phase_blockers(3, phase1, {**gateway_probe, "container_env": ["API_KEY"]}, ROUTE)
    assert check.phase_blockers(1, check.dotenv_state(INSTALLED_LOCAL, "https://other.invalid"), gateway_probe, ROUTE)


def test_the_capture_block_is_added_once_and_never_preempted():
    block = check.env_block("/Mythic/x", "/Mythic/x/fifo")
    added = check.insert_block(b"A=1", block)
    assert added.startswith(b"A=1\n") and added.endswith(block.encode())
    with pytest.raises(SystemExit):
        check.insert_block(added, block)
    assert check.preempted_keys(b"SAGE_EVAL_MAX_OUTPUT_TOKENS=8\n", block) == ["SAGE_EVAL_MAX_OUTPUT_TOKENS"]
    assert check.preempted_keys(INSTALLED_LOCAL.encode(), block) == []


def test_the_budget_holds_the_frozen_limits_and_the_ledger_accepts_it(tmp_path):
    from ai.resource_budget import ResourceBudgetExceeded, ResourceLedger

    config = tmp_path / "budget.json"
    config.write_text(json.dumps(check.budget_config()))
    ledger = ResourceLedger(config, tmp_path / "ledger.jsonl")
    for _ in range(check.LIMITS["provider_calls"]):
        ledger.settle_provider(ledger.reserve({"provider_requests": 1, "provider_tokens": check.TOKEN_RESERVATION},
                                              "provider"), 1000)
    with pytest.raises(ResourceBudgetExceeded):
        ledger.reserve({"provider_requests": 1, "provider_tokens": 1}, "provider")
    for _ in range(50):
        ledger.reserve({"control_requests": 1}, "control-rpc-application-request")


def test_the_settings_rule_accepts_sages_real_definitions_and_rejects_the_old_ones():
    from sage_chat.models import SAGE_MODELS

    deployed = [json.dumps(model.to_json()) for model in SAGE_MODELS]
    assert check.settings_error(deployed) == ""
    old = [model.to_json() for model in SAGE_MODELS]
    for option in old[0]["metadata"]["configuration_options"]:
        if option["name"] == "provider":
            option["choices"] = [c for c in option["choices"] if c["value"] not in ("litellm", "openai_compatible")]
    assert "provider choices" in check.settings_error([json.dumps(m) for m in old])


def test_the_watcher_state_reads_paused_and_running_profiles(tmp_path):
    import sqlite3

    database = tmp_path / "sage.db"
    connection = sqlite3.connect(database)
    connection.execute("create table watcher_profiles (operation_id text, lifecycle_state text, paused integer)")
    connection.execute("insert into watcher_profiles values ('1', 'running', 0), ('2', 'paused', 1)")
    connection.commit()
    connection.close()
    assert check.watcher_state(database)["active"] == ["1"]
    assert check.watcher_state(tmp_path / "missing.db") == {"readable": False}


# --------------------------------------------------------------------------------------------------
# Capture: Sage's real factory, metering and observation channel, read back by the check's parser
# --------------------------------------------------------------------------------------------------

def test_the_receiver_records_what_sage_sends_on_both_family_routes(tmp_path, monkeypatch):
    from ai import observation_channel
    from ai.langgraph import model as model_module

    for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "LANGSMITH_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    observation_channel._reset_after_fork()
    parent = tmp_path / "capture"
    parent.mkdir(mode=0o700)
    record, ready = tmp_path / "wire.jsonl", tmp_path / "ready.json"
    receiver = subprocess.Popen([sys.executable, str(SCRIPT), "_receive", "--record", str(record),
                                 "--parent", str(parent), "--ready", str(ready)])

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={
                "id": "resp_x", "object": "response", "created_at": 0, "model": "gpt-5.6-sol", "status": "completed",
                "output": [{"id": "msg_x", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "READY", "annotations": []}]}],
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}})
        return httpx.Response(200, json={
            "id": "chatcmpl-x", "object": "chat.completion", "created": 0, "model": "bedrock-claude-4-6-sonnet",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "tooluse_x", "type": "function",
                                                                      "function": {"name": "list_callbacks",
                                                                                   "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", json.loads(ready.read_text())["directory"])
        config = tmp_path / "budget.json"
        config.write_text(json.dumps(check.budget_config()))
        monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(config))
        monkeypatch.setenv("SAGE_RESOURCE_BUDGET_LEDGER", str(tmp_path / "ledger.jsonl"))
        for channel in ("L", "O"):
            settings = check.channel_config(ROUTE, channel)["config"]
            sage = model_module.Model.__new__(model_module.Model)
            sage.provider, sage.model = settings["provider"], settings["model"]
            configurable = {"api_key": "offline-placeholder"}
            if channel == "L":
                configurable["base_url"] = GATEWAY  # the gateway endpoint the installed .env.local supplies
            if settings.get("SAGE_REASONING_EFFORT"):
                configurable["reasoning_effort"] = settings["SAGE_REASONING_EFFORT"]
            sage.config = {"configurable": configurable}
            sage._eval_arm_deadline_epoch = None
            sage._provider_retry_state = None
            sage._controller_runtime_telemetry = {}
            llm = sage._get_base_chat_model()
            metered = llm.root_client._client._transport
            assert type(metered).__name__ == "MeteredHTTPTransport"
            metered.transport = httpx.MockTransport(provider)
            llm.invoke("Offline request")
    finally:
        (parent / "stop").write_text("")
        receiver.wait(timeout=15)
        observation_channel._reset_after_fork()
    items = check.exchanges(check.read_wire(record))
    assert [item["api"] for item in items] == ["chat_completions", "responses"]
    assert items[0]["url"] == check.expected_url(ROUTE, "L") and items[0]["fields"] == []
    assert check.produced_calls(items[0]) == [{"call_id": "tooluse_x", "name": "list_callbacks", "arguments": "{}"}]
    assert items[1]["url"] == check.expected_url(ROUTE, "O")
    assert items[1]["store"] is False and items[1]["access_programs"] == {"cyber": "standard"}
    assert check.REASONING_INCLUDE in items[1]["include"]
    assert [check.usage_tokens(item) for item in items] == [5, 5]


# --------------------------------------------------------------------------------------------------
# r2: attribution by capture time, the send gate, and the channel binding (P9)
# --------------------------------------------------------------------------------------------------

ATTEMPT1 = json.loads((Path(__file__).parent / "fixtures/provider_split_attempt1_capture.json").read_text())


def _attempt1_ids(*indices):
    by_index = {r["index"]: r for r in ATTEMPT1["records"]}
    return [by_index[i]["record"]["payload"]["request_id"] for i in indices]


def _ids(items):
    return [item["request_id"] for item in items]


def _upto(time_ns):
    return [r for r in ATTEMPT1["records"] if r["record"]["time_ns"] <= time_ns]


def test_attempt_one_capture_attributes_each_request_to_the_step_in_flight():
    by_step, unattributed = check.attribute(ATTEMPT1["records"], ATTEMPT1["steps"])
    assert _ids(by_step["O1"]) == _attempt1_ids(26580, 26655)
    assert _ids(by_step["O2"]) == _attempt1_ids(26824)
    assert _ids(by_step["L1"]) == _attempt1_ids(13113, 13180)
    assert _ids(by_step["M1"]) == _attempt1_ids(13405, 13472)
    assert _ids(by_step["G1"]) == _attempt1_ids(27019, 27087)
    assert unattributed == []
    assert {item["model"] for item in by_step["O1"]} == {"gpt-5.6-sol"}


def test_attempt_one_record_count_windows_were_what_misplaced_o1():
    """The windows attempt 1 used: O1 held none of its own requests and O2 held all three."""
    records = ATTEMPT1["records"]
    window = {step: [r for r in records if info["wire_after"] <= r["index"] < info["wire_until"]]
              for step, info in ATTEMPT1["steps"].items()}
    assert check.exchanges(window["O1"]) == []
    assert _ids(check.exchanges(window["O2"])) == _attempt1_ids(26580, 26655, 26824)


def test_a_step_with_no_captured_completion_closes_at_the_next_send():
    steps = copy.deepcopy(ATTEMPT1["steps"])
    records = [r for r in ATTEMPT1["records"]
               if not (r["kind"] == "control_request" and r["record"]["payload"].get("routing_key") == "chat_response"
                       and json.loads(base64.b64decode(r["record"]["payload"]["body_base64"]))["request_id"] == 33)]
    by_step, unattributed = check.attribute(records, steps)
    assert by_step["O1"] == []
    assert _ids(by_step["O2"]) == _attempt1_ids(26580, 26655, 26824)
    assert unattributed == []


def test_a_request_outside_every_step_is_reported_unattributed():
    steps = {"L1": dict(ATTEMPT1["steps"]["L1"])}
    by_step, unattributed = check.attribute(ATTEMPT1["records"], steps)
    assert _ids(by_step["L1"]) == _attempt1_ids(13113, 13180)
    assert len(unattributed) == 7


def test_the_send_gate_holds_until_sage_answers_mythics_on_start():
    steps = ATTEMPT1["steps"]
    # Phase 2: O1 went out after the new process started and before Sage answered on-start.
    assert check.sage_answered_on_start(_upto(steps["O1"]["sent_ns"])) is False
    assert check.sage_answered_on_start(_upto(steps["O2"]["sent_ns"])) is True
    # Phase 1: L1 went out after the armed process answered on-start.
    assert check.sage_answered_on_start(_upto(steps["L1"]["sent_ns"])) is True
    # Phase 3: L2 was refused after the third process started; Sage never answered on-start in the capture.
    assert check.sage_answered_on_start(ATTEMPT1["records"]) is False
    assert check.sage_answered_on_start([]) is False


class _GateBase:
    def __init__(self, running):
        self.running = running
        self.nc = type("NC", (), {"READINESS_QUERY": "readiness"})

    async def _query(self, _client, query, **_kwargs):
        assert query == "readiness"
        return {"consuming_container": [{"id": 1, "container_running": self.running}]}


def _wire_file(tmp_path, records):
    path = tmp_path / "wire.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


@pytest.mark.parametrize(("running", "records", "expected"), [
    pytest.param(True, "before_on_start", "phase 2 container not ready: Sage has not answered Mythic's on-start",
                 id="stale-running-before-on-start"),
    pytest.param(False, "after_on_start", "phase 2 container not ready: Mythic does not show the Sage container running",
                 id="on-start-but-not-running"),
    pytest.param(True, "after_on_start", "", id="ready"),
])
def test_the_gate_needs_mythics_running_flag_and_sages_on_start_answer(tmp_path, running, records, expected):
    steps = ATTEMPT1["steps"]
    cut = steps["O1"]["sent_ns"] if records == "before_on_start" else steps["O2"]["sent_ns"]
    wire = _wire_file(tmp_path, _upto(cut))
    result = asyncio.run(check.await_sage_ready(object(), _GateBase(running), wire, phase=2, timeout=0.05, poll=0.01))
    assert result == expected


def test_each_step_waits_for_the_gate_before_its_channel_and_message(tmp_path, monkeypatch):
    events = []
    wire = _wire_file(tmp_path, [])

    class _NC:
        @staticmethod
        async def create_locked_channel(_client, **kwargs):
            events.append(("channel", kwargs["name"][-1]))
            return {"chat_channel_id": len(events)}

        @staticmethod
        async def create_message(_client, channel_id, prompt):
            events.append(("send", channel_id))
            return {"chat_request_id": 100 + len(events)}

        @staticmethod
        async def wait_for_request(*_args, **_kwargs):
            return {}

    class _Base:
        nc = _NC

        @staticmethod
        async def _max_task(_client):
            return 18

        @staticmethod
        def phoenix_mark(_path):
            return 0

    async def gate(_client, _base, _record, *, phase, **_kwargs):
        events.append(("gate", phase))
        return ""

    async def switch(_run, phase, _route):
        events.append(("switch", phase))
        return ""

    async def cleanup(*_args):
        return {}

    async def subscriptions(_client):
        return None

    monkeypatch.setattr(check, "_base", lambda: _Base)
    monkeypatch.setattr(check, "await_sage_ready", gate)
    monkeypatch.setattr(check, "_switch_phase", switch)
    monkeypatch.setattr(check, "cmd_cleanup", cleanup)
    monkeypatch.setattr(check, "_subscriptions", subscriptions)
    monkeypatch.setattr(check, "read_route", lambda _path: dict(ROUTE))
    monkeypatch.setattr(check, "_identity", lambda: {})
    run = {"readiness": {"verdict": "ready_for_live"}, "capture": {"record": str(wire)}}
    args = type("Args", (), {"route_file": "route", "phoenix_db": "phoenix", "authorized_by": "operator"})()
    asyncio.run(check.cmd_run(object(), run, args))

    kinds = [kind for kind, _ in events]
    for index, kind in enumerate(kinds):
        if kind in ("channel", "send"):
            previous = [k for k in kinds[:index] if k != "channel"]
            assert previous and previous[-1] == "gate", events
    assert [phase for kind, phase in events if kind == "gate"] == [1, 1, 2, 2, 2, 3]
    assert all(isinstance(info["sent_ns"], int) for info in run["steps"].values())
    assert list(run["steps"]) == ["L1", "M1", "O1", "O2", "G1", "L2"]


# Mythic rows read from the lab on 2026-11-25 (read only): channel 12's token 31, owned by the operation's bot.
LAB_BINDING = {"apitokens_id": 31, "token": {
    "id": 31, "operator_id": 2, "operation_id": 1, "active": True, "deleted": False, "token_type": "api",
    "operator": {"username": "operation-chimera-bot", "account_type": "bot"},
    "scopes": ["apitoken.read", "apitoken.write", "callback.read", "chat-ai.read", "chat-ai.write",
               "credential.read", "credential.write", "eventlog.read", "eventlog.write", "file.read",
               "file.write", "operation.read", "operator.read", "payload.read", "payload.write",
               "response.read", "task.read", "task.write"]}}
LAB_OPERATION = {"id": 1, "name": "Operation Chimera"}


def _bindings(**change):
    token = {**LAB_BINDING["token"], **change}
    return {channel: {"apitokens_id": 31, "token": copy.deepcopy(token)} for channel in ("L", "M", "O", "G")}


def test_p9_passes_when_every_channel_is_bound_to_the_operation_bots_scoped_token():
    records, marks, rows, run = _passing()
    result = _evaluate(records, marks, rows, run)
    assert result["criteria"]["P9_channel_binding"] is True
    assert result["detail"]["P9_channel_binding"] == {c: "ok" for c in ("L", "M", "O", "G")}


@pytest.mark.parametrize("change", [
    {"operator": {"username": "sage-bot", "account_type": "bot"}},
    {"active": False}, {"deleted": True}, {"operation_id": 4},
    {"scopes": [s for s in LAB_BINDING["token"]["scopes"] if s != "credential.write"]},
    {"scopes": LAB_BINDING["token"]["scopes"] + ["*"]},
], ids=["other-bot", "inactive", "deleted", "other-operation", "missing-scope", "wildcard"])
def test_p9_rejects_each_binding_near_match(change):
    records, marks, rows, run = _passing()
    bindings = _bindings()
    bindings["O"]["token"].update(change)
    result = _evaluate(records, marks, rows, run, bindings=bindings)
    assert result["criteria"]["P9_channel_binding"] is False
    assert result["detail"]["P9_channel_binding"]["O"] != "ok"
    assert result["detail"]["P9_channel_binding"]["L"] == "ok"


def test_p9_needs_a_binding_row_for_every_run_channel():
    records, marks, rows, run = _passing()
    bindings = _bindings()
    del bindings["G"]
    assert _evaluate(records, marks, rows, run, bindings=bindings)["criteria"]["P9_channel_binding"] is False


def test_a_gate_stop_means_the_check_did_not_run():
    records, marks, rows, run = _passing()
    run["stopped"] = {"step": "L1", "reason": "phase 1 container not ready: Sage has not answered Mythic's on-start"}
    assert _evaluate(records, marks, rows, run)["verdict"] == "not_run"


def test_readiness_blocks_an_explicit_binding_and_records_the_default_path(monkeypatch):
    nc = check._base().nc
    tokens = [{**LAB_BINDING["token"], "name": "Sage native chat"}]
    bots = [{"id": 2, "username": "operation-chimera-bot", "account_type": "bot", "active": True, "deleted": False,
             "current_operation_id": 1},
            {"id": 3, "username": "sage-bot", "account_type": "bot", "active": True, "deleted": False,
             "current_operation_id": 1}]
    operations = [{"id": 1}]

    async def fake_query(_client, query, variables=None):
        if query == nc.OPERATION_BOT_QUERY:
            return {"operation_by_pk": LAB_OPERATION, "operator": bots}
        if query == nc.READINESS_QUERY:
            return {"consuming_container": [], "apitokens": tokens}
        if query == check.LIVE_OPERATIONS_QUERY:
            return {"operation": operations}
        raise AssertionError(query)

    monkeypatch.setattr(nc.mythic, "execute_custom_query", fake_query)
    client = type("LabClient", (), {"current_operation_id": 1})()
    for name in ("SAGE_BACKING_APITOKEN_ID", "SAGE_CHAT_CONTAINER_ID"):
        monkeypatch.delenv(name, raising=False)

    state = asyncio.run(check.default_channel_path(client))
    assert state == {"explicit_binding": [], "live_operations": 1,
                     "active_bots": ["operation-chimera-bot", "sage-bot"], "operation_bot": "operation-chimera-bot",
                     "token": {"id": 31, "scopes": sorted(LAB_BINDING["token"]["scopes"])}, "error": ""}
    assert check.default_path_blockers(state) == []

    monkeypatch.setenv("SAGE_BACKING_APITOKEN_ID", "31")
    operations.append({"id": 2})
    bots[0]["active"] = False
    state = asyncio.run(check.default_channel_path(client))
    assert state["explicit_binding"] == ["SAGE_BACKING_APITOKEN_ID"]
    assert state["live_operations"] == 2 and state["operation_bot"] is None and state["token"] is None
    assert "SAGE_BACKING_APITOKEN_ID" in state["error"]
    assert check.default_path_blockers(state) == [
        "an explicit channel binding is set (SAGE_BACKING_APITOKEN_ID); the check uses the default path",
        "Mythic has 2 live operations; the send gate needs exactly one",
        "the default channel path cannot pick the operation bot",
    ]
