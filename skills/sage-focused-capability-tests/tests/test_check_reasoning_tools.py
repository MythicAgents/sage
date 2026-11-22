"""Offline checks for the reasoning-tool live check's local logic (no Mythic, no provider).

The wire records and Mythic rows below are synthetic, shaped like the receiver's records and Mythic's rows; they
prove the pass rules accept a correct set and reject each falsifier the check specification names. The capture
test drives the real observation channel, receiver, metered transport and OpenAI SDK over a local stand-in.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import openai
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_reasoning_tools.py"
SPEC = importlib.util.spec_from_file_location("check_reasoning_tools", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))

ENDPOINT = "https://api.openai.com/v1"
E1 = {"message": "Function tools with reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions. "
      "To use function tools, use /v1/responses or set reasoning effort to none",
      "type": "invalid_request_error", "param": None, "code": None}
E2 = {"message": "Unsupported value reasoning_effort does not support none for this model. Supported values are "
      "low, medium, high, and xhigh.", "type": "invalid_request_error", "param": "reasoning.effort",
      "code": "unsupported_value"}
CALLBACKS = json.dumps([{"display_id": 5, "host": "WORKSTATION-5"}])


def _b64(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


class Wire:
    def __init__(self):
        self.records: list[dict] = []
        self.count = 0

    def exchange(self, *, api, model, effort, items, status=200, reply=None, tools=("list_callbacks",)):
        self.count += 1
        rid = f"r{self.count}"
        url = f"{ENDPOINT}/responses" if api == "responses" else f"{ENDPOINT}/chat/completions"
        body = {"model": model, "tools": [{"type": "function", "name": t} for t in tools]}
        if api == "responses":
            body.update(input=items, store=False, access_programs={"cyber": "standard"},
                        reasoning={"effort": effort})
        else:
            body.update(messages=items, reasoning_effort=effort,
                        tools=[{"type": "function", "function": {"name": t}} for t in tools])
        self.records.append({"kind": "model_request", "record": {"time_ns": 1, "payload": {
            "request_id": rid, "method": "POST", "url": url, "body_base64": _b64(body)}}})
        self.records.append({"kind": "model_response", "record": {"time_ns": 2, "payload": {
            "request_id": rid, "status_code": status, "outcome": "returned", "body_base64": _b64(reply or {})}}})


def _ok(output, total=12_000, out=300):
    return {"output": output, "usage": {"total_tokens": total, "output_tokens": out}}


def _call(call_id):
    return {"type": "function_call", "call_id": call_id, "name": "list_callbacks", "arguments": "{}"}


def _message(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def _history(*parts):
    items = []
    for part in parts:
        if isinstance(part, str):
            items.append({"role": "user", "content": part})
        else:
            items.extend([_call(part[0]), {"type": "function_call_output", "call_id": part[0], "output": CALLBACKS}])
    return items


def _passing():
    """A correct two-model set: wire per step, Mythic rows per step, and the run record."""
    wire, marks, rows = Wire(), {}, {}

    def step(name, fn, status, texts):
        start = len(wire.records)
        fn()
        marks[name] = (start, len(wire.records))
        rows[name] = {"status": status, "messages": [{"message": "", "metadata": {"tool_use": {}}}]
                      + [{"message": text, "metadata": {}} for text in texts]}

    for channel, model, rejected_api, rejected_effort, error, action in (
            ("A", "gpt-5.6-sol", "chat_completions", "low", E1,
             f"{check.TOOLS_ACTION} ... {check.TOOLS_KEEPS}"),
            ("B", "gpt-6.1-sol", "responses", "none", E2, check.EFFORT_ACTION)):
        c1, c3 = f"call_{channel.lower()}1", f"call_{channel.lower()}3"
        seed_answer, tool_answer = f"Label amber-seven. One callback {channel}.", f"Still amber-seven {channel}."
        step(f"{channel}1", lambda: (
            wire.exchange(api="responses", model=model, effort="low", items=_history(check.SEED),
                          reply=_ok([{"type": "reasoning", "id": "rs_0011223344"}, _call(c1)])),
            wire.exchange(api="responses", model=model, effort="low", items=_history(check.SEED, (c1,)),
                          reply=_ok([_message(seed_answer)]))), "complete", [seed_answer])
        step(f"{channel}2", lambda: wire.exchange(
            api=rejected_api, model=model, effort=rejected_effort, items=_history(check.SEED, (c1,), check.TOOL),
            status=400, reply={"error": error}), "error", [f"The model provider rejected this request. To fix it, {action}."])
        step(f"{channel}3", lambda: (
            wire.exchange(api="responses", model=model, effort="low",
                          items=_history(check.SEED, (c1,), check.TOOL, check.TOOL), reply=_ok([_call(c3)])),
            wire.exchange(api="responses", model=model, effort="low",
                          items=_history(check.SEED, (c1,), check.TOOL, check.TOOL, (c3,)),
                          reply=_ok([_message(tool_answer)]))),
            "complete", ["Applied this chat's updated OpenAI settings (...)", tool_answer])
        step(f"{channel}4", lambda: wire.exchange(
            api="responses", model=model, effort="low",
            items=_history(check.SEED, (c1,), check.TOOL, check.TOOL, (c3,), check.RECALL),
            reply=_ok([_message("Your label is amber-seven.")])), "complete", ["Your label is amber-seven."])
    run = {"task_baseline": 9, "started_at": 0, "finished_at": 600,
           "steps": {name: {"tasks_after": 9} for name in marks}}
    return wire.records, marks, rows, run


def _evaluate(records, marks, rows, run, totals=None):
    wire = {name: check.exchanges(records[a:b]) for name, (a, b) in marks.items() if name in run["steps"]}
    every = check.exchanges(records)
    return check.evaluate(run, wire, rows, totals or {"transport_requests": len(every), "unpaired": 0})


def _reply(records, marks, step, index):
    """The decoded reply body of the step's index-th exchange, for mutation."""
    a, _b = marks[step]
    record = records[a + 2 * index + 1]["record"]["payload"]
    return record, json.loads(base64.b64decode(record["body_base64"]))


def _request(records, marks, step, index):
    a, _b = marks[step]
    record = records[a + 2 * index]["record"]["payload"]
    return record, json.loads(base64.b64decode(record["body_base64"]))


def _set(record, body):
    record["body_base64"] = _b64(body)


def test_a_correct_two_model_set_passes_every_criterion():
    result = _evaluate(*_passing())
    assert result["verdict"] == "pass", json.dumps(result["detail"], indent=1)
    assert all(result["criteria"].values())


def test_an_orphan_call_id_fails_the_recovered_seam():
    records, marks, rows, run = _passing()
    record, body = _request(records, marks, "A3", 1)
    for item in body["input"]:
        if item.get("call_id") == "call_a3":
            item["call_id"] = "call_other"
    _set(record, body)
    result = _evaluate(records, marks, rows, run)
    assert not result["criteria"]["3_both_recovered_seams"]
    assert not result["detail"]["round_trips"]["A3"]["result_consumed_later"]


def test_a_result_that_never_reaches_a_later_request_fails():
    """Wrong ordering: the step ends on the call itself; nothing consumes its result afterwards."""
    records, marks, rows, run = _passing()
    a, _b = marks["B3"]
    del records[a + 2:a + 4]
    marks["B3"] = (a, a + 2)
    for name in ("B4",):
        start, end = marks[name]
        marks[name] = (start - 2, end - 2)
    result = _evaluate(records, marks, rows, run)
    assert not result["detail"]["round_trips"]["B3"]["result_consumed_later"]
    assert result["verdict"] == "fail"


def test_a_missing_response_body_fails_containment_and_the_seam():
    records, marks, rows, run = _passing()
    a, _b = marks["A1"]
    records.pop(a + 1)  # the first response record is lost
    marks = {name: ((s - 1 if s > a else s), (e - 1 if e > a else e)) for name, (s, e) in marks.items()}
    every = check.exchanges(records)
    result = _evaluate(records, marks, rows, run,
                       {"transport_requests": len(every),
                        "unpaired": sum(1 for i in every if i["status"] is None and i["outcome"] is None)})
    assert not result["criteria"]["5_containment_evidence"]
    assert not result["criteria"]["3_both_recovered_seams"]


@pytest.mark.parametrize(("field", "value"), [("effort", "medium"), ("model", "gpt-5.6-luna"), ("api", "other")])
def test_a_wrong_effort_model_or_route_fails_identity(field, value):
    records, marks, rows, run = _passing()
    record, body = _request(records, marks, "B3", 0)
    if field == "effort":
        body["reasoning"]["effort"] = value
    elif field == "model":
        body["model"] = value
    else:
        record["url"] = f"{ENDPOINT}/completions"
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["criteria"]["1_identity"]


def test_dropped_tools_or_a_daybreak_change_fails_identity():
    records, marks, rows, run = _passing()
    record, body = _request(records, marks, "A1", 0)
    body["tools"] = []
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["criteria"]["1_identity"]
    records, marks, rows, run = _passing()
    record, body = _request(records, marks, "A4", 0)
    body["access_programs"] = {"cyber": "daybreak_red"}
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["criteria"]["1_identity"]


def test_a_duplicate_native_answer_fails():
    records, marks, rows, run = _passing()
    rows["A3"]["messages"].append(dict(rows["A3"]["messages"][-1]))
    result = _evaluate(records, marks, rows, run)
    assert not result["detail"]["round_trips"]["A3"]["one_native_answer"]
    assert result["verdict"] == "fail"


def test_an_answer_not_from_the_provider_fails():
    records, marks, rows, run = _passing()
    rows["B1"]["messages"][-1]["message"] = "Inventory: 1 callback (deterministic summary)."
    assert not _evaluate(records, marks, rows, run)["detail"]["round_trips"]["B1"]["answer_from_provider"]


@pytest.mark.parametrize("totals_or_run", [
    {"totals": {"transport_requests": 33, "unpaired": 0}},
    {"run": {"finished_at": 20 * 60 + 1}},
    {"run": {"stopped": {"step": "B2", "reason": "transport request ceiling"}}},
])
def test_exhausted_budgets_fail_containment(totals_or_run):
    records, marks, rows, run = _passing()
    run.update(totals_or_run.get("run", {}))
    totals = totals_or_run.get("totals")
    assert not _evaluate(records, marks, rows, run, totals)["criteria"]["5_containment_evidence"]


def test_a_large_output_or_token_total_fails_containment():
    records, marks, rows, run = _passing()
    record, body = _reply(records, marks, "A4", 0)
    body["usage"]["output_tokens"] = 5000
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["criteria"]["5_containment_evidence"]
    records, marks, rows, run = _passing()
    record, body = _reply(records, marks, "A4", 0)
    body["usage"]["total_tokens"] = 1_100_000
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["criteria"]["5_containment_evidence"]


def test_success_on_one_model_alone_does_not_pass():
    records, marks, rows, run = _passing()
    for name in ("B1", "B2", "B3", "B4"):
        run["steps"].pop(name)
    result = _evaluate(records, marks, rows, run)
    assert result["verdict"] == "fail"
    assert not result["criteria"]["2_both_rejections"] and not result["criteria"]["5_containment_evidence"]


def test_a_target_task_fails_the_rejection_and_containment():
    records, marks, rows, run = _passing()
    run["steps"]["A2"]["tasks_after"] = 10
    result = _evaluate(records, marks, rows, run)
    assert not result["detail"]["rejections"]["A2"]["no_task"]
    assert not result["criteria"]["5_containment_evidence"]


def test_a_rejection_without_its_action_or_with_partial_work_fails():
    records, marks, rows, run = _passing()
    rows["B2"]["messages"][-1]["message"] = "Partial Work Preserved: try a different model."
    assert not _evaluate(records, marks, rows, run)["criteria"]["2_both_rejections"]
    records, marks, rows, run = _passing()
    rows["A2"]["messages"][-1]["message"] = f"To fix it, {check.TOOLS_ACTION}."  # retains-reasoning text missing
    assert not _evaluate(records, marks, rows, run)["criteria"]["2_both_rejections"]


def test_an_unrelated_400_is_not_accepted_as_the_reported_rejection():
    records, marks, rows, run = _passing()
    record, body = _reply(records, marks, "A2", 0)
    body["error"] = {"message": "No tool output found for function call call_1.", "type": "invalid_request_error",
                     "param": "input", "code": None}
    _set(record, body)
    rejection = _evaluate(records, marks, rows, run)["detail"]["rejections"]["A2"]
    assert not rejection["classified"] and not rejection["provider_said_so"]


def test_the_independent_reading_rejects_the_other_case_and_a_wrong_status():
    records, marks, rows, run = _passing()
    record, body = _reply(records, marks, "B2", 0)
    body["error"] = E1  # the tools-on-Chat-Completions error where an unsupported-None error belongs
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["detail"]["rejections"]["B2"]["provider_said_so"]
    exchange = {"status": 404, "reply": {"error": E1}}
    assert not check.independent_rejection(exchange, "tools_with_effort")
    assert check.independent_rejection({"status": 400, "reply": {"error": E1}}, "tools_with_effort")
    assert check.independent_rejection({"status": 400, "reply": {"error": E2}}, "effort_value")


def test_a_refusal_or_lost_history_fails_continuity():
    records, marks, rows, run = _passing()
    rows["A3"]["messages"].insert(1, {"message": "Sage replaced this channel's session. Start a new Sage chat.",
                                      "metadata": {}})
    assert not _evaluate(records, marks, rows, run)["criteria"]["4_continuity_output"]
    records, marks, rows, run = _passing()
    record, body = _request(records, marks, "B4", 0)
    body["input"] = [{"role": "user", "content": check.RECALL}]
    _set(record, body)
    assert not _evaluate(records, marks, rows, run)["detail"]["continuity"]["B4"]["label_in_history"]


def test_a_visible_reasoning_id_fails_continuity():
    records, marks, rows, run = _passing()
    rows["B3"]["messages"][-1]["message"] += " rs_0011223344aa"
    assert not _evaluate(records, marks, rows, run)["detail"]["continuity"]["B3"]["no_reasoning_leak"]


# --------------------------------------------------------------------------------------------------
# Route file, .env block, budget
# --------------------------------------------------------------------------------------------------

GOOD = f"API_ENDPOINT={ENDPOINT}\nMODEL_A=gpt-5.6-sol\nMODEL_B=gpt-6.1-sol\nEFFORT=low\n"


def _route(tmp_path, body, mode=0o600):
    path = tmp_path / "route.env"
    path.write_text(body)
    path.chmod(mode)
    return path


def test_the_route_file_is_owner_only_keyless_direct_and_exact(tmp_path):
    assert check.read_route(_route(tmp_path, GOOD))["MODEL_B"] == "gpt-6.1-sol"
    for body, message in ((GOOD + "API_KEY=x\n", "credential"),
                          (GOOD.replace(ENDPOINT, "https://proxy.invalid/v1"), "direct OpenAI"),
                          (GOOD.replace("gpt-6.1-sol", "gpt-6-sol"), "no substitute"),
                          (GOOD.replace("EFFORT=low", "EFFORT=medium"), "EFFORT must be low")):
        with pytest.raises(SystemExit, match=message):
            check.read_route(_route(tmp_path, body))
    with pytest.raises(SystemExit, match="owner-only"):
        check.read_route(_route(tmp_path, GOOD, 0o644))


def test_the_env_block_is_added_once_and_removable_byte_for_byte():
    original = b"# operator settings\nAPI_ENDPOINT=https://api.openai.com/v1"
    block = check.env_block("/Mythic/.reasoning-check-x", "/Mythic/.reasoning-check-x/sage-observation-abc")
    armed = check.insert_block(original, block)
    assert armed.startswith(original + b"\n") and b"SAGE_OBSERVATION_DIRECTORY=/Mythic/" in armed
    assert b"SAGE_EVAL_MAX_OUTPUT_TOKENS=4096" in armed
    with pytest.raises(SystemExit, match="cleanup"):
        check.insert_block(armed, block)
    assert check.preempted_keys(b"API_KEY=placeholder\nSAGE_OTHER=1\n", block) == []
    assert check.preempted_keys(b"SAGE_EVAL_MAX_OUTPUT_TOKENS=99\nSAGE_OBSERVATION_DIRECTORY=\n", block) == [
        "SAGE_EVAL_MAX_OUTPUT_TOKENS"]


def test_channel_config_changes_only_the_named_setting_and_leaves_daybreak_blank():
    route = {"API_ENDPOINT": ENDPOINT, "MODEL_A": "gpt-5.6-sol", "MODEL_B": "gpt-6.1-sol", "EFFORT": "low"}
    a1 = check.channel_config(route, "A", api="responses", effort="low")["config"]
    a2 = check.channel_config(route, "A", api="chat_completions", effort="low")["config"]
    assert {k for k in a1 if a1[k] != a2.get(k)} == {"SAGE_OPENAI_API"}
    assert "SAGE_OPENAI_ACCESS_PROGRAM" not in a1
    assert a1["mode"] == "conversation" and a1["autonomous_solve"] is False and a1["max_steps"] == 4
    assert a1["SAGE_PROVIDER_RETRY_POLICY"] == "transient-v1"
    # The validator accepts every configuration the sequence uses (explicit Off would refuse A2 locally).
    from ai.openai_api import validate_route
    for step, channel, api, effort, _prompt, _kind in check.STEPS:
        validate_route(provider="openai", model=route[f"MODEL_{channel}"], api=api, access_program="")
    with pytest.raises(ValueError, match="requires OpenAI API = Responses"):
        validate_route(provider="openai", model="gpt-5.6-sol", api="chat_completions", access_program="standard")


def test_the_budget_matches_the_frozen_ceilings_and_the_ledger_accepts_it(tmp_path):
    from ai.resource_budget import ResourceLedger

    config = tmp_path / "budget.json"
    config.write_text(json.dumps(check.budget_config()))
    ledger = ResourceLedger(config, tmp_path / "ledger.jsonl")
    for _ in range(check.CEILINGS["transport_requests"]):
        ledger.settle_provider(ledger.reserve({"provider_requests": 1, "provider_tokens": 1}, "provider"), 1)
    from ai.resource_budget import ResourceBudgetExceeded
    with pytest.raises(ResourceBudgetExceeded):
        ledger.reserve({"provider_requests": 1, "provider_tokens": 1}, "provider")
    # Sage meters its Mythic control plane on the same ledger from startup; those calls must keep working.
    for _ in range(50):
        ledger.reserve({"control_requests": 1}, "control-rpc-application-request")
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert check.provider_reservations(lines) == check.CEILINGS["transport_requests"]
    assert check.budget_config()["calendar_seconds"] > check.CEILINGS["active_seconds"] + check.CEILINGS["cleanup_seconds"]


# --------------------------------------------------------------------------------------------------
# Capture: a captured 400 stays a typed SDK error and is recorded by the real receiver
# --------------------------------------------------------------------------------------------------

def test_a_captured_400_reaches_the_sdk_typed_and_the_receiver_records_both_sides(tmp_path, monkeypatch):
    from ai import observation_channel

    observation_channel._reset_after_fork()  # a fresh producer for this receiver, whatever ran earlier
    parent = tmp_path / "capture"
    parent.mkdir(mode=0o700)
    record, ready = tmp_path / "wire.jsonl", tmp_path / "ready.json"
    receiver = subprocess.Popen([sys.executable, str(SCRIPT), "_receive", "--record", str(record),
                                 "--parent", str(parent), "--ready", str(ready)])
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        directory = json.loads(ready.read_text())["directory"]
        monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", directory)
        from ai import resource_http
        from ai.resource_budget import ResourceLedger

        config = tmp_path / "budget.json"
        config.write_text(json.dumps(check.budget_config()))
        ledger = ResourceLedger(config, tmp_path / "ledger.jsonl")
        stand_in = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": E1}))

        def client(allow):
            transport = resource_http.MeteredHTTPTransport("provider", ledger=ledger, transport=stand_in,
                                                           allow_non2xx=allow)
            return openai.OpenAI(api_key="offline-placeholder", base_url=ENDPOINT, max_retries=0,
                                 http_client=httpx.Client(transport=transport))

        with pytest.raises(openai.BadRequestError) as raised:
            client(True).chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": "x"}],
                                                 reasoning_effort="low")
        from ai.langgraph.operator_error import provider_configuration_rejection
        assert provider_configuration_rejection(raised.value) is raised.value
        # Without the retry policy's allowance the capture boundary replaces the typed 400 (why the check sets it).
        with pytest.raises(Exception) as replaced:
            client(False).chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": "x"}])
        assert not isinstance(replaced.value, openai.BadRequestError)
    finally:
        (parent / "stop").write_text("")
        receiver.wait(timeout=15)
        observation_channel._reset_after_fork()
    items = check.exchanges(check.read_wire(record))
    assert len(items) == 2
    assert items[0]["api"] == "chat_completions" and items[0]["effort"] == "low" and items[0]["status"] == 400
    assert check.rejection_kind(items[0]) == "tools_with_effort"


def test_the_model_factory_keeps_4xx_typed_when_the_check_policy_is_metered(monkeypatch, tmp_path):
    """The check's channel settings (Transient v1 plus a configured ledger) give the provider transport allow_non2xx."""
    from ai.langgraph import model as model_module

    config = tmp_path / "budget.json"
    config.write_text(json.dumps(check.budget_config()))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(config))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_LEDGER", str(tmp_path / "ledger.jsonl"))
    sage = model_module.Model.__new__(model_module.Model)
    sage.provider, sage.model = "openai", "gpt-5.6-sol"
    sage.config = {"configurable": {"api_key": "offline-placeholder", "base_url": ENDPOINT,
                                    "reasoning_effort": "low", "provider_retry_policy": check.RETRY_POLICY,
                                    "openai_api": "chat_completions"}}
    llm = sage._get_base_chat_model()
    transport = llm.root_client._client._transport
    metered = getattr(transport, "transport", None) or getattr(transport, "_transport", None) or transport
    found = [layer for layer in (transport, metered, getattr(metered, "transport", None))
             if layer is not None and hasattr(layer, "allow_non2xx")]
    assert found and all(layer.allow_non2xx for layer in found)
