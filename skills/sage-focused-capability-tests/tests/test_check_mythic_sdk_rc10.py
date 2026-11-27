"""Offline checks for the Mythic SDK rc10 live check's pass rules and file handling (no Mythic, no provider).

Wire records, Phoenix span summaries and event-log rows below are synthetic, shaped like the receiver's records, the
spans table and Mythic's operationeventlog. M2's generations, MCP1's replies and M3's status, scan and pause replies come
from recorded output (`fixtures/mythic_sdk_rc10_r3_recorded.json`, provenance inside); the one `/watcher resume` reply
and the falsifier variants are rendered by Sage's own `render_watcher_status`. M2's card rows and the follow-up's tool
result come from a separate recording (`fixtures/task_result_request85.json`, provenance inside) with Mythic's storage
step applied; its task rows are synthetic (the raw value split in two), and pairing it with the other recording's
generations is a synthetic combination. They prove the rules accept a correct set and reject each falsifier.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_mythic_sdk_rc10.py"
SPEC = importlib.util.spec_from_file_location("check_mythic_sdk_rc10", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
split = check._split()

from sage_chat.findings_watcher import render_watcher_status  # noqa: E402

GATEWAY = "https://gateway.invalid:7443"
ROUTE = {"GATEWAY_ENDPOINT": GATEWAY, "GATEWAY_MODEL": "gpt-5.6-cyber", "SUPERVISED_MODEL": "bedrock-claude-4-6-sonnet",
         "OPENAI_REASONING_MODEL": "gpt-5.6-sol", "OPENAI_PLAIN_MODEL": "gpt-4.1", "ANTHROPIC_MODEL": "claude-haiku-5-5",
         "BEDROCK_MODEL": "us.anthropic.claude-haiku-5-5-v1:0"}
CALLBACK = 2
OWNER = 233
CALLBACKS = json.dumps([{"display_id": 2, "host": "WORKSTATION-A", "user": "lab.user", "integrity": 2}])
RECORDED = json.loads((ROOT / "skills/sage-focused-capability-tests/tests/fixtures/mythic_sdk_rc10_r3_recorded.json")
                      .read_text())
RESULT85 = json.loads((ROOT / "skills/sage-focused-capability-tests/tests/fixtures/task_result_request85.json")
                      .read_text())


def _m2_rows() -> dict:
    """Request 85's two cards as Mythic stores them (tool_use.output moved to tool_output), plus the answer."""
    messages = []
    for card in RESULT85["cards"]:
        metadata = copy.deepcopy(card["metadata"])
        output = metadata["tool_use"].pop("output", "")
        messages.append({"author_type": "ai", "message": card["content"], "metadata": metadata,
                         "tool_output": output or ""})
    messages.append({"author_type": "ai", "message": "whoami returned LAB\\lab.user.", "metadata": {}})
    return {"status": "complete", "messages": messages}


def _m2_raw() -> str:
    return next(m["tool_output"] for m in _m2_rows()["messages"]
                if (m["metadata"].get("tool_use") or {}).get("tool_call_id", "").startswith("mythic-task:"))


def _m2_task() -> dict:
    """Synthetic: Mythic's rows for request 85's task, the recorded raw value split after 30 bytes."""
    data = _m2_raw().encode()
    return {"id": 9022, "display_id": RESULT85["task"], "completed": True, "status": "success", "command_name": "whoami",
            "callback": {"display_id": RESULT85["callback"]}, "subtask_count": 0,
            "responses": [{"id": 1, "response_text": base64.b64encode(data[:30]).decode()},
                          {"id": 2, "response_text": base64.b64encode(data[30:]).decode()}]}


def _recorded_rows(request_id: int) -> dict:
    return copy.deepcopy(RECORDED["mythic"][str(request_id)])


def _recorded_generations(window: str) -> list[dict]:
    """A recorded Phoenix window as `m2_generations` returns it, built through the real output parser."""
    return [{"id": span["id"], "model": span["model"], "status": span["status_code"],
             "generations": check.generation_calls(span["output_value"]),
             "task_call_ids": check.first_generation_task_call_ids(span["output_value"])}
            for span in RECORDED["phoenix"][window]["spans"]]


def _b64(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


class Wire:
    def __init__(self):
        self.records: list[dict] = []

    def add(self, url, body, reply, status=200):
        request_id = f"r{len(self.records)}"
        self.records.append({"kind": "model_request", "record": {"payload": {
            "request_id": request_id, "url": url, "body_base64": _b64(body)}}})
        self.records.append({"kind": "model_response", "record": {"payload": {
            "request_id": request_id, "status_code": status, "body_base64": _b64(reply), "outcome": "returned"}}})


def _responses_body(model, items):
    return {"model": model, "input": items, "store": False, "include": ["reasoning.encrypted_content"],
            "access_programs": {"cyber": "standard"}}


def _responses_reply(output, total=900):
    return {"object": "response", "status": "completed", "output": output, "usage": {"total_tokens": total}}


def _cc_body(model, messages):
    return {"model": model, "messages": messages}


def _cc_reply(message, total=700):
    return {"object": "chat.completion", "choices": [{"index": 0, "message": message}], "usage": {"total_tokens": total}}


def _cc_call(call_id):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "list_callbacks", "arguments": "{}"}}]}


def _cc_round_trip(wire, url, model, call_id):
    user = [{"role": "user", "content": check.LIST}]
    wire.add(url, _cc_body(model, user), _cc_reply(_cc_call(call_id)))
    wire.add(url, _cc_body(model, user + [_cc_call(call_id), {"role": "tool", "tool_call_id": call_id,
                                                              "content": CALLBACKS}]),
             _cc_reply({"role": "assistant", "content": "One callback."}))


def _wire() -> dict[str, list[dict]]:
    by_step: dict[str, list[dict]] = {}

    def take(step, build):
        wire = Wire()
        build(wire)
        by_step[step] = split.exchanges(wire.records)

    take("W3", lambda w: _cc_round_trip(w, GATEWAY + "/chat/completions", ROUTE["GATEWAY_MODEL"], "call_L"))
    take("W4", lambda w: _cc_round_trip(w, GATEWAY + "/chat/completions", ROUTE["GATEWAY_MODEL"], "call_K"))
    take("W2", lambda w: w.add(check.OPENAI_ENDPOINT + "/chat/completions",
                               _cc_body(ROUTE["OPENAI_PLAIN_MODEL"], [{"role": "user", "content": check.LIST}]),
                               _cc_reply({"role": "assistant", "content": "One callback."})))
    url, model = check.OPENAI_ENDPOINT + "/responses", ROUTE["OPENAI_REASONING_MODEL"]
    user = [{"role": "user", "content": check.LIST}]
    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-1", "summary": []}
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_O", "name": "list_callbacks", "arguments": "{}"}
    result = {"type": "function_call_output", "call_id": "call_O", "output": CALLBACKS}
    answer = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One."}]}

    def o1(w):
        w.add(url, _responses_body(model, user), _responses_reply([reasoning, call]))
        w.add(url, _responses_body(model, user + [reasoning, call, result]), _responses_reply([answer]))

    def o2(w):
        w.add(url, _responses_body(model, user + [reasoning, call, result, answer,
                                                  {"role": "user", "content": check.INTEGRITY}]),
              _responses_reply([answer]))

    take("W1_1", o1)
    take("W1_2", o2)
    return by_step


def _llm(model, produced=(), consumed=(), inputs=(), tokens=(1000, 100)):
    return {"kind": "LLM", "model": model, "produced_calls": list(produced), "consumed_results": list(consumed),
            "input_calls": list(inputs), "prompt_tokens": tokens[0], "completion_tokens": tokens[1]}


def _spans(wire) -> dict[str, list[dict]]:
    gateway = ROUTE["GATEWAY_MODEL"]
    spans = {
        "M1": [_llm(gateway, produced=["c1"]), _llm(gateway, consumed=["c1"], inputs=["c1"])],
        "M2": [_llm(gateway, produced=["c2"]), _llm(gateway, consumed=["c2"], inputs=["c2"])],
    }
    for key, first, second in (("A", "A1n_1", "A1n_2"), ("B", "B1_1", "B1_2")):
        model = check.channel_model(ROUTE, key)
        spans[first] = [_llm(model, produced=[f"toolu_{key}"]), _llm(model, consumed=[f"toolu_{key}"],
                                                                     inputs=[f"toolu_{key}"])]
        spans[second] = [_llm(model, inputs=[f"toolu_{key}"])]
    for step, items in wire.items():
        model = items[0]["model"] if items else ""
        spans[step] = [_llm(model) for _ in items]
    return spans


def _rows(step: str, text: str = "An answer.", status: str = "complete", **extra) -> dict:
    return {"status": status, "messages": [{"author_type": "ai", "message": text, "metadata": {}, **extra}]}


def _status(scans, state="running", error="-", pending=0):
    return render_watcher_status({
        "operation_id": "1", "status": state, "owner_channel_name": "Sage Watcher", "owner_channel_id": OWNER,
        "generation": 4, "provider": "openai", "model": "gpt-5.6-cyber", "bot_username": "sage_watcher_bot",
        "last_success_at": "2026-11-25T00:00:00Z", "active_findings": 0, "pending_deliveries": pending,
        "interval_seconds": 300, "channel": "#sage-findings", "findings_channel_id": 12, "scan_count": scans,
        "last_error_code": error})


def _notice(model):
    return (f"The model provider rejected this chat's request before the model answered (HTTP 404): not found\n\n"
            f"Route used: provider anthropic · model `{model}` (channel) · endpoint provider default.\n\n"
            "To fix it, change **Model** to a model this provider and key can use, or run `/model <name>`, then "
            "send your request again. This chat is still usable and keeps its conversation; nothing was run.")


def _passing():
    wire = _wire()
    spans = _spans(wire)
    rows = {step: _rows(step) for step, *_ in check.STEPS}
    rows["M2"] = _m2_rows()
    rows["MCP1_connect"] = _recorded_rows(16)
    rows["MCP1"] = _recorded_rows(17)
    rows["M3_status"] = _recorded_rows(47)  # paused, scans 0
    rows["M3_resume"] = _rows("M3_resume", _status(scans=0, state="starting"))  # synthetic: none was recorded
    rows["M3_scan"] = _recorded_rows(29)  # running, scans 2
    rows["M3_pause"] = _recorded_rows(30)  # paused, scans 3
    rows["A2n"] = _rows("A2n", _notice(check.channel_model(ROUTE, "X")))
    rows["B2"] = _rows("B2", _notice(check.channel_model(ROUTE, "Y")))
    requests = {step: {"request_id": index + 100, "sends": 1, "sent_ns": index} for index, (step, *_) in
                enumerate(check.STEPS)}
    requests["M2"].update(cards_seen=1, cards_accepted=1, cards_rejected=0)
    run = {"requests": requests, "callback": CALLBACK, "active_seconds": 1800.0,
           "a1": {"packages": dict(check.EXPECTED_PACKAGES), "differing_from_constraints": {},
                  "pip_check_returncode": 0, "audit_ran": True, "audit_findings": 0},
           "p1": {"listeners": {"http_0.0.0.0:6006": True, "grpc_0.0.0.0:4317": True},
                  "graphql_unauthenticated": 401, "healthz": [200, 200, 200]},
           "files_after_cleanup": {"env_local_restored": True, "env_restored": True}, "phases": []}
    tasks = [_m2_task()]
    formatted = {"M2": {"call_id": RESULT85["call_id"], "content": RESULT85["follow_up"]["content"], "spans": [1278],
                        "reason": ""}}
    observed = {"rows": rows, "wire": wire, "spans": spans, "generations": {"M2": _recorded_generations("rc11_l2")},
                "thinking": {"A1n_1": 0, "B1_1": 0}, "tasks": tasks, "formatted": formatted,
                "traces": {"snapshot": 120, "missing": 0}, "notice": {"warnings": 1, "leaks": False},
                "watcher_paused": True, "totals": {"provider_calls": 30, "provider_tokens": 200_000}}
    return run, observed


def test_a_correct_set_passes_every_rule():
    run, observed = _passing()
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "pass", {k: v for k, v in result["criteria"].items() if not v}
    assert set(result["criteria"]) == {"A1", "M1", "M2", "M3", "P1", "P2", "P3", "W1", "W2", "W3", "W4", "A1n",
                                       "A2n", "B1", "B2", "MCP1", "L"}


def _set(path, value):
    def mutate(run, observed):
        target = {"run": run, "observed": observed}
        *head, last = path
        for part in head:
            target = target[part]
        target[last] = value
    return mutate


def _drop_wire_result(step):
    def mutate(run, observed):
        items = observed["wire"][step]
        observed["wire"][step] = items[:1]
    return mutate


def _change_wire(step, field, value):
    def mutate(run, observed):
        for item in observed["wire"][step]:
            item[field] = value
    return mutate


FALSIFIERS = [
    ("A1", _set(["run", "a1", "audit_findings"], 1)),
    ("A1", _set(["run", "a1", "differing_from_constraints"], {"gql": ["4.0.0", "4.4.0"]})),
    ("M1", _set(["observed", "rows", "M1", "status"], "error")),
    ("M2", _set(["run", "requests", "M2", "cards_seen"], 0)),
    ("M2", _set(["observed", "tasks"], [])),
    ("M2", lambda run, obs: obs["rows"]["M2"]["messages"][0].update(tool_output="Timed out")),
    ("M2", lambda run, obs: obs["rows"]["M2"]["messages"].insert(2, {"author_type": "ai", "message": "", "metadata": {
        "special_type": "tool_use", "tool_use": {"status": "wait_failed", "tool_name": "whoami",
                                                 "tool_call_id": "mythic-task:2:99"}}, "tool_output": ""})),
    ("M2", lambda run, obs: obs["tasks"][0]["responses"].pop()),  # the card holds more than Mythic's rows
    ("M2", lambda run, obs: obs["tasks"][0].update(subtask_count=1)),
    ("M2", lambda run, obs: obs["tasks"][0].update(display_id=23)),  # another task than the lifecycle card's
    ("M2", lambda run, obs: obs["formatted"]["M2"].update(content=None)),  # no follow-up text was read
    ("M2", lambda run, obs: obs["formatted"]["M2"].update(call_id="tooluse_other")),  # another call's result
    ("M2", lambda run, obs: obs["rows"]["M2"]["messages"][1]["metadata"]["tool_use"].update(
        result_preview=obs["formatted"]["M2"]["content"] + " (and more)")),
    ("M2", lambda run, obs: obs["formatted"]["M2"].update(
        content=obs["formatted"]["M2"]["content"].replace("task_display_id=22", "task_display_id=23"))),
    ("M2", lambda run, obs: [obs["formatted"]["M2"].update(  # card and follow-up agree, but name another task
        content=obs["formatted"]["M2"]["content"].replace("task_display_id=22", "task_display_id=23")),
        obs["rows"]["M2"]["messages"][1]["metadata"]["tool_use"].update(
            result_preview=obs["formatted"]["M2"]["content"])]),
    ("M3", lambda run, obs: obs["rows"].update(M3_scan=_rows("M3_scan", _status(scans=0)))),
    ("M3", lambda run, obs: obs["rows"].update(M3_scan=_rows("M3_scan", _status(scans=6, pending=1)))),
    ("M3", lambda run, obs: obs["rows"].update(M3_scan=_rows("M3_scan", _status(scans=6, error="TransportError")))),
    ("M3", lambda run, obs: obs["rows"].update(M3_scan=_recorded_rows(48))),  # attempt 1: a paused Watcher's scan
    ("M3", lambda run, obs: obs["rows"].update(M3_scan=_rows("M3_scan", _status(scans=2, state="paused")))),
    ("M3", lambda run, obs: obs["rows"].update(M3_pause=_rows("M3_pause", _status(scans=3)))),  # still running
    ("M3", lambda run, obs: obs["rows"].update(M3_scan=_rows("M3_scan", "Watcher control denied: `X`.\n\n"
                                                             + _status(scans=6)))),
    ("M3", _set(["observed", "watcher_paused"], False)),
    ("P1", _set(["run", "p1", "graphql_unauthenticated"], 200)),
    ("P1", _set(["run", "p1", "healthz"], [200, None, 200])),
    ("P1", _set(["run", "p1", "listeners", "grpc_0.0.0.0:4317"], False)),
    ("P2", _set(["observed", "traces", "missing"], 3)),
    ("P2", lambda run, obs: obs["spans"].update(W3=obs["spans"]["W3"][:1])),
    ("P2", lambda run, obs: obs["spans"].update(M1=[])),
    ("P3", _set(["observed", "notice"], {"warnings": 0, "leaks": False})),
    ("P3", _set(["observed", "notice"], {"warnings": 1, "leaks": True})),
    ("W1", lambda run, obs: [item.update(store=True) for item in obs["wire"]["W1_1"]]),
    ("W1", _change_wire("W1_1", "access_programs", None)),
    ("W2", _change_wire("W2", "url", check.OPENAI_ENDPOINT + "/responses")),
    ("W2", _change_wire("W2", "model", "gpt-4.1-mini")),
    ("W3", _drop_wire_result("W3")),
    ("W4", _change_wire("W4", "status", 400)),
    ("A1n", lambda run, obs: obs["spans"].update(A1n_2=[_llm(check.channel_model(ROUTE, "A"))])),
    ("A1n", lambda run, obs: obs["spans"]["A1n_1"][1].update(consumed_results=[])),
    ("A2n", _set(["observed", "rows", "A2n", "messages", 0, "message"], "Something went wrong.")),
    ("B1", _set(["observed", "rows", "B1_2", "status"], "error")),
    ("B2", _set(["observed", "rows", "B2", "messages", 0, "message"], _notice("another-model"))),
    ("MCP1", _set(["observed", "rows", "MCP1", "messages", 0, "message"],
                  "**BloodHound** — 2 tool(s)\n- `file_upload`\n- `cypher_query`")),
    ("MCP1", lambda run, obs: obs["rows"].update(MCP1=_recorded_rows(46))),  # attempt 1: nothing had connected
    ("MCP1", _set(["observed", "rows", "MCP1_connect", "messages", 1, "message"],
                  "**Connection:** BloodHound MCP not connected.\n\n**Ingest:** Unavailable until BloodHound is "
                  "connected.")),
    ("L", _set(["observed", "totals", "provider_calls"], 41)),
    ("L", _set(["run", "files_after_cleanup", "env_restored"], False)),
    ("L", _set(["run", "stopped"], {"step": "W2", "reason": "provider token limit"})),
]


@pytest.mark.parametrize("failing, mutate", FALSIFIERS, ids=[f"{name}-{i}" for i, (name, _m) in enumerate(FALSIFIERS)])
def test_each_falsifier_fails_its_rule(failing, mutate):
    run, observed = _passing()
    mutate(run, observed)
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail"
    assert result["criteria"][failing] is False


@pytest.mark.parametrize("gate", check.GATE_STEPS)
def test_a_correctly_sent_step_whose_model_made_no_tool_call_fails_its_row(gate):
    """The model seam is the row's pass condition: a model that answered on its route without the call fails the row
    (it does not make the attempt "not run"), and the missing call is recorded as the reason."""
    run, observed = _passing()
    if gate in check.WIRE_STEPS:
        for item in observed["wire"][gate]:
            item["reply"] = _cc_reply({"role": "assistant", "content": "No call."}) if item["api"] != "responses" \
                else _responses_reply([])
    else:
        for span in observed["spans"][gate]:
            span["produced_calls"] = []
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail"
    assert result["criteria"][check.GATE_CRITERION[gate]] is False
    assert gate in result["detail"]["no_tool_call"][check.GATE_CRITERION[gate]]
    assert all(value is True for name, value in result["criteria"].items() if name != check.GATE_CRITERION[gate])


@pytest.mark.parametrize("gate", ["W3", "A1n_1"])
def test_a_gate_step_whose_provider_failed_is_a_failure_not_a_skipped_check(gate):
    run, observed = _passing()
    if gate == "W3":
        for item in observed["wire"]["W3"]:
            item["status"] = 500
    else:
        observed["spans"]["A1n_1"] = []
        observed["rows"]["A1n_1"] = _rows("A1n_1", _notice(check.channel_model(ROUTE, "A")))
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail"
    assert result["criteria"]["W3" if gate == "W3" else "A1n"] is False


def test_a_failed_phase_proof_before_its_first_request_did_not_run():
    run, observed = _passing()
    for step in ("A1n_1", "A1n_2", "A2n", "B1_1", "B1_2", "B2", "W3", "W4", "W1_1", "W1_2", "W2"):
        run["requests"].pop(step)
    run["stopped"] = {"step": "A1n_1", "reason": "phase A credential: the keyed model listing did not return 200"}
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "not_run"
    later = {"A1n", "A2n", "B1", "B2", "W1", "W2", "W3", "W4", "L"}
    assert {name for name, value in result["criteria"].items() if value is None} == later
    assert all(value is True for name, value in result["criteria"].items() if name not in later)


def test_a_failure_that_ran_is_never_hidden_by_a_step_that_did_not_run():
    # Phase N's M2 failed, then B1's first request made no tool call: both rows failed.
    run, observed = _passing()
    observed["tasks"] = []
    for span in observed["spans"]["B1_1"]:
        span["produced_calls"] = []
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail"
    assert result["criteria"]["M2"] is False and result["criteria"]["B1"] is False
    # Phase N's P3 failed, then phase A's proof failed before its first request: still a failure.
    run, observed = _passing()
    observed["notice"] = {"warnings": 0, "leaks": False}
    for step in ("A1n_1", "A1n_2", "A2n", "B1_1", "B1_2", "B2", "W3", "W4", "W1_1", "W1_2", "W2"):
        run["requests"].pop(step)
    run["stopped"] = {"step": "A1n_1", "reason": "phase A container not ready"}
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail" and result["criteria"]["P3"] is False


def test_a_phase_stop_that_left_the_installed_files_changed_fails_containment():
    run, observed = _passing()
    for step in ("B1_1", "B1_2", "B2", "W3", "W4", "W1_1", "W1_2", "W2"):
        run["requests"].pop(step)
    run["stopped"] = {"step": "B1_1", "reason": "phase B credential: the keyed model listing did not return 200"}
    run["files_after_cleanup"]["env_local_restored"] = False
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail" and result["criteria"]["L"] is False


@pytest.mark.parametrize("settings, listening, status, expected", [
    ({"host": "0.0.0.0", "auth_enabled": True, "password_is_shipped": True}, {"a": True, "b": True}, 401, []),
    ({"host": "127.0.0.1", "auth_enabled": False, "password_is_shipped": True}, {"a": False, "b": False}, 200,
     ["every address", "listening", "without a token"]),
    ({"host": "0.0.0.0", "auth_enabled": True, "password_is_shipped": False}, {"a": True, "b": True}, 401,
     ["no warning can fire"]),
])
def test_readiness_blocks_unless_the_deployed_phoenix_can_meet_p1_and_p3(settings, listening, status, expected):
    blockers = check.phoenix_blockers(settings, listening, status)
    assert len(blockers) == len(expected)
    for fragment, blocker in zip(expected, blockers):
        assert fragment in blocker


def test_a_model_from_another_route_fails_the_provider_round_trip():
    run, observed = _passing()
    observed["spans"]["B1_1"].append(_llm("claude-sonnet-other"))
    assert check.evaluate(run, observed, ROUTE)["criteria"]["B1"] is False


def test_the_route_file_is_owner_only_keyless_complete_and_exact(tmp_path):
    path = tmp_path / "route"
    path.write_text("\n".join(f"{k}={v}" for k, v in ROUTE.items()))
    path.chmod(0o600)
    assert check.read_route(path) == ROUTE
    path.chmod(0o644)
    with pytest.raises(SystemExit, match="owner-only"):
        check.read_route(path)
    path.chmod(0o600)
    for body, message in ((path.read_text() + "\nAPI_KEY=x", "credential"),
                          (path.read_text().replace("gpt-5.6-cyber", "gpt-4.1"), "GATEWAY_MODEL"),
                          ("\n".join(f"{k}={v}" for k, v in ROUTE.items() if k != "BEDROCK_MODEL"), "missing")):
        path.write_text(body)
        with pytest.raises(SystemExit, match=message):
            check.read_route(path)


def test_no_channel_carries_a_key_or_an_endpoint_and_wrong_models_are_marked():
    for key in check.CHANNELS:
        config = check.channel_config(ROUTE, key)["config"]
        assert not set(config) & {"API_KEY", "API_ENDPOINT", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID",
                                  "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "missing_model"}
    assert check.channel_config(ROUTE, "X")["config"]["model"] == "claude-haiku-5-5" + check.MISSING_MODEL_SUFFIX
    assert check.channel_config(ROUTE, "G")["config"]["SAGE_OPENAI_API"] == "chat_completions"
    assert check.channel_config(ROUTE, "S")["config"]["mode"] == "supervised"


REPO_LOCAL = """\
# LiteLLM
API_ENDPOINT=https://gateway.invalid:7443
API_KEY=litellm-fixture-key
# OpenAI
#API_KEY=openai-fixture-key
# Anthropic
#API_KEY=anthropic-fixture-key
# Bedrock
#AWS_ACCESS_KEY_ID=fixture-access-id
#AWS_SECRET_ACCESS_KEY=fixture-secret
#AWS_SESSION_TOKEN=fixture-session
#AWS_DEFAULT_REGION=us-east-1
SAGE_WATCHER_MODEL=gpt-5.6-cyber
"""


def test_each_blocks_values_come_from_its_own_header_only():
    assert check.block_values(REPO_LOCAL, "Anthropic") == {"API_KEY": "anthropic-fixture-key"}
    assert check.block_values(REPO_LOCAL, "OpenAI") == {"API_KEY": "openai-fixture-key"}
    assert sorted(check.block_values(REPO_LOCAL, "Bedrock")) == sorted(check.BLOCK_KEYS["Bedrock"])
    assert check.block_presence(REPO_LOCAL)["Bedrock"] == sorted(check.BLOCK_KEYS["Bedrock"])


@pytest.mark.parametrize("phase", ["A", "B", "O"])
def test_a_phase_file_holds_only_its_block_and_keeps_every_other_line(phase):
    original = REPO_LOCAL.encode()
    values = check.block_values(REPO_LOCAL, check.PHASE_BLOCK[phase])
    text = check.phase_local(original, phase, values).decode()
    assert check.phase_file_blockers(phase, text) == []
    assert "SAGE_WATCHER_MODEL=gpt-5.6-cyber" in text
    assert "\n#API_KEY=litellm-fixture-key" in text and "\n#API_ENDPOINT=https://gateway.invalid:7443" in text
    assert check.phase_file_blockers(phase, REPO_LOCAL) != []


def test_p1_reads_wildcard_ipv4_listeners_only():
    ss = ("LISTEN 0 4096 0.0.0.0:6006 0.0.0.0:*\nLISTEN 0 4096 0.0.0.0:4317 0.0.0.0:*\n"
          "LISTEN 0 4096 127.0.0.1:7443 0.0.0.0:*\n")
    assert check.listeners(ss) == {"http_0.0.0.0:6006": True, "grpc_0.0.0.0:4317": True}
    assert check.listeners(ss.replace("0.0.0.0:4317", "127.0.0.1:4317"))["grpc_0.0.0.0:4317"] is False
    # What the deployed Phoenix 22 shows on the lab host (`ss -ltnH`, 2026-11-25): gRPC's dual-stack wildcard.
    deployed = "LISTEN 0      2048        0.0.0.0:6006  0.0.0.0:*\nLISTEN 0      4096              *:4317        *:*\n"
    assert check.listeners(deployed) == {"http_0.0.0.0:6006": True, "grpc_0.0.0.0:4317": True}
    # An IPv6-only wildcard does not accept IPv4.
    assert check.listeners(deployed.replace("*:4317", "[::]:4317"))["grpc_0.0.0.0:4317"] is False


def test_p3_finds_sages_warning_and_flags_a_leaked_value():
    from ai.phoenix_auth import DEFAULT_PASSWORD_NOTICE, SHIPPED_DEFAULT_ADMIN_PASSWORD

    events = [{"id": 9, "warning": False, "message": "Container sage on start response:\n Sage Watcher ..."},
              {"id": 10, "warning": True, "message": f"Failed to process sage on start response:\n "
                                                     f"{DEFAULT_PASSWORD_NOTICE}"}]
    assert check.SHIPPED_PASSWORD == SHIPPED_DEFAULT_ADMIN_PASSWORD
    assert check.NOTICE_PHRASE in DEFAULT_PASSWORD_NOTICE
    assert check.notice_events(events, [SHIPPED_DEFAULT_ADMIN_PASSWORD, "secret-value-0123456789"]) == {
        "warnings": 1, "leaks": False}
    leaked = copy.deepcopy(events)
    leaked[1]["message"] += " secret-value-0123456789"
    assert check.notice_events(leaked, ["secret-value-0123456789"])["leaks"] is True


def test_the_task_call_id_comes_from_the_first_generation_only():
    spans = {span["id"]: span for span in RECORDED["phoenix"]["rc11_l2"]["spans"]}
    assert check.first_generation_task_call_ids(spans[1263]["output_value"]) == ["tooluse_VE4QPTxFogGu5o7Gs6hyYx"]
    assert check.first_generation_task_call_ids(spans[1239]["output_value"]) == []  # another tool's call
    later = {span["id"]: span for span in RECORDED["phoenix"]["attempt1_m2"]["spans"]}[1639]
    assert check.first_generation_task_call_ids(later["output_value"]) == []  # the call only in generation 1


def test_m2_reads_the_follow_ups_text_for_the_one_task_call_in_place(tmp_path):
    """Spans table synthetic, in Phoenix's input-message shape; read through the advisories check's span reader."""
    import sqlite3

    db = tmp_path / "phoenix.db"
    call, content = RESULT85["call_id"], RESULT85["follow_up"]["content"]
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, status_code TEXT, events TEXT, "
                     "attributes TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER)")
        follow_up = {"llm": {"model_name": "m", "input_messages": [
            {"message": {"role": "tool", "name": check.TASK_TOOL, "tool_call_id": call, "content": content}}]}}
        other = {"llm": {"model_name": "m", "input_messages": [
            {"message": {"role": "tool", "tool_call_id": "call_other", "content": "another result"}}]}}
        conn.execute("INSERT INTO spans VALUES (1, 'LLM', 'OK', '[]', ?, 1, 1)", (json.dumps(other),))
        conn.execute("INSERT INTO spans VALUES (2, 'LLM', 'OK', '[]', ?, 1, 1)", (json.dumps(follow_up),))
    assert check.m2_formatted(db, 0, 2, [call]) == {"call_id": call, "content": content, "spans": [2], "reason": ""}
    assert check.m2_formatted(db, 0, 2, [call, "call_second"])["content"] is None  # two task calls
    assert check.m2_formatted(db, 0, 2, [])["content"] is None
    assert check.m2_formatted(db, 0, 1, [call])["content"] is None  # the follow-up is outside the window
    assert check.phoenix_records_tool_results(db) == {"span": 2, "text": True}
    assert check.phoenix_tool_result_blockers(check.phoenix_records_tool_results(db)) == []


def test_readiness_blocks_when_phoenix_keeps_no_tool_result_text(tmp_path):
    """Synthetic store: the newest tool-result span lost its text (or none exists, or the store is unreadable)."""
    import sqlite3

    db = tmp_path / "phoenix.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, status_code TEXT, events TEXT, "
                     "attributes TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER)")
        textless = {"llm": {"model_name": "m", "input_messages": [{"message": {"role": "tool", "tool_call_id": "c"}}]}}
        conn.execute("INSERT INTO spans VALUES (1, 'LLM', 'OK', '[]', ?, 1, 1)", (json.dumps(textless),))
    assert check.phoenix_records_tool_results(db) == {"span": 1, "text": False}
    assert check.phoenix_tool_result_blockers(check.phoenix_records_tool_results(db))
    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as conn:
        conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, status_code TEXT, events TEXT, "
                     "attributes TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER)")
    assert check.phoenix_tool_result_blockers(check.phoenix_records_tool_results(empty))
    assert check.phoenix_records_tool_results(tmp_path / "missing.db")["text"] is False
    assert check.phoenix_tool_result_blockers(None)


def test_m3_reads_a_paused_watchers_scan_count_from_sages_own_reply():
    rc11 = check._rc11()
    before = rc11.parse_status(_status(scans=5, state="paused"))
    after = rc11.parse_status(_status(scans=6, state="paused"))
    assert before["state"] == after["state"] == "paused"
    assert (before["scans"], after["scans"], after["pending"]) == (5, 6, 0)


def test_the_supervised_channel_has_room_for_its_card_and_only_h_carries_bloodhound_settings():
    settings = {"BLOODHOUND_URL": "http://127.0.0.1:8083", "BLOODHOUND_TOKEN_ID": "id-fixture",
                "BLOODHOUND_TOKEN_KEY": "key-fixture"}
    assert check.channel_config(ROUTE, "S")["config"]["max_steps"] == check.SUPERVISED_MAX_STEPS == 8
    assert check.channel_config(ROUTE, "C")["config"]["max_steps"] == check.MAX_STEPS
    h = check.channel_config(ROUTE, "H", settings)["config"]
    assert {k: h[k] for k in check.BLOODHOUND_KEYS} == settings and h["mode"] == "conversation"
    for key in set(check.CHANNELS) - {"H"}:
        assert not set(check.channel_config(ROUTE, key, settings)["config"]) & set(check.BLOODHOUND_KEYS)


def _aborted_like_attempt_1(run, observed):
    """The shape of the uncounted 2026-11-25 run: M1 and M2 sent, MCP1 failed in the check, nothing after."""
    keep = {"M1", "M2"}
    for step in list(run["requests"]):
        if step not in keep:
            run["requests"].pop(step)
    run["unsent_error"] = {"step": "MCP1", "error": "RuntimeError"}  # never sent: absent from requests
    run["stopped"] = {"step": "MCP1", "reason": "check error: RuntimeError"}
    run["p1"]["healthz"] = [200, 200]


def test_a_check_error_records_the_unsent_criteria_as_not_run():
    run, observed = _passing()
    _aborted_like_attempt_1(run, observed)
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "not_run" and "check error" in result["reason"]
    assert result["criteria"]["M1"] is True and result["criteria"]["M2"] is True
    unsent = {"MCP1", "M3", "A1n", "A2n", "B1", "B2", "W1", "W2", "W3", "W4", "P1", "L"}
    assert {name for name, value in result["criteria"].items() if value is None} == unsent


def test_a_check_error_never_hides_a_criterion_that_ran_and_failed():
    run, observed = _passing()
    _aborted_like_attempt_1(run, observed)
    run["requests"]["M2"]["cards_seen"] = 0  # what the uncounted run did, under the old 4-step cap
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail" and result["criteria"]["M2"] is False


def test_a_reused_channel_needs_an_active_backing_token(monkeypatch):
    import asyncio

    tokens = {31: {"id": 31, "active": True, "deleted": False}, 32: {"id": 32, "active": False, "deleted": False}}

    class Base:
        async def _query(self, client, query, **variables):
            return {"apitokens_by_pk": tokens.get(variables["id"])}

    monkeypatch.setattr(check, "_base", lambda: Base())
    run = asyncio.run
    assert run(check.channel_token_active(None, {"apitokens_id": 31})) is True
    assert run(check.channel_token_active(None, {"apitokens_id": 32})) is False
    assert run(check.channel_token_active(None, {"apitokens_id": None})) is False
    assert run(check.channel_token_active(None, None)) is False


def _stopped_before(run, observed, first_unsent):
    """A check error before `first_unsent` was sent: it and every later step are absent from requests."""
    names = [step for step, *_ in check.STEPS]
    for step in names[names.index(first_unsent):]:
        run["requests"].pop(step)
    run["unsent_error"] = {"step": first_unsent, "error": "RuntimeError"}
    run["stopped"] = {"step": first_unsent, "reason": "check error: RuntimeError"}
    run["p1"]["healthz"] = [200, 200]


@pytest.mark.parametrize("first_unsent, criterion", [("A1n_1", "A1n"), ("W3", "W3"), ("W1_2", "W1")])
def test_a_check_error_before_a_model_or_wire_step_is_sent_is_not_a_failure(first_unsent, criterion):
    run, observed = _passing()
    _stopped_before(run, observed, first_unsent)
    assert first_unsent not in check.measured(run)  # verify reads no window or wire for it
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "not_run", {k: v for k, v in result["criteria"].items() if v is False}
    assert result["criteria"][criterion] is None and result["criteria"]["P2"] is True


def test_a_step_cut_off_by_the_check_mid_wait_is_not_run_but_a_finished_failure_still_fails():
    run, observed = _passing()
    _stopped_before(run, observed, "MCP1")
    run["requests"]["M2"]["check_error"] = "ConnectionError"
    run["stopped"] = {"step": "M2", "reason": "check error: ConnectionError"}
    run["requests"]["M2"]["cards_seen"] = 0
    observed["rows"]["M2"]["status"] = "streaming"  # Sage had not finished when the check failed
    observed["spans"]["M2"] = []  # its Phoenix window was cut short
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "not_run" and result["criteria"]["M2"] is None and result["criteria"]["P2"] is True
    observed["rows"]["M2"]["status"] = "error"  # Sage finished the request and raised no card: a real failure
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail" and result["criteria"]["M2"] is False


def test_verify_reads_windows_only_for_steps_that_carry_them():
    run = {"requests": {"M1": {"request_id": 1, "phoenix_after": 5, "phoenix_until": 9, "sent_ns": 1},
                        "M2": {"request_id": 2, "check_error": "RuntimeError"},
                        "W3": {"request_id": 3, "sent_ns": 3}}}
    assert list(check.measured(run)) == ["M1"]


def test_cleanup_blanks_only_the_copied_bloodhound_settings():
    metadata = {"config": {"provider": "litellm", "BLOODHOUND_URL": "http://127.0.0.1:8083",
                           "BLOODHOUND_TOKEN_ID": "id-fixture", "BLOODHOUND_TOKEN_KEY": "key-fixture"},
                "channel_metadata_display": {"display": "expanded; max=15"}}
    cleared = check.without_bloodhound(metadata)
    assert cleared["config"] == {"provider": "litellm", "BLOODHOUND_URL": "", "BLOODHOUND_TOKEN_ID": "",
                                 "BLOODHOUND_TOKEN_KEY": ""}
    assert cleared["channel_metadata_display"] == metadata["channel_metadata_display"]
    assert metadata["config"]["BLOODHOUND_TOKEN_KEY"] == "key-fixture"  # the input is not changed in place


# --------------------------------------------------------------------------------------------------
# Revision 3: M2 on its own gateway model with a first-generation gate, M3 on a running Watcher, MCP1 connected first,
# and the added readiness. Recorded inputs from fixtures/mythic_sdk_rc10_r3_recorded.json.
# --------------------------------------------------------------------------------------------------

def _span(window: str, span_id: int) -> dict:
    return next(span for span in RECORDED["phoenix"][window]["spans"] if span["id"] == span_id)


def test_the_parser_reads_each_generation_of_the_recorded_outputs():
    # Span 1639: the gateway's split, text in choice 0 and the task call in choice 1.
    assert check.generation_calls(_span("attempt1_m2", 1639)["output_value"]) == [[], [check.TASK_TOOL]]
    # Span 1263: text and the call in one choice.
    assert check.generation_calls(_span("rc11_l2", 1263)["output_value"]) == [[check.TASK_TOOL]]
    assert check.generation_calls("not json") == [] and check.generation_calls(None) == []
    invalid = json.dumps({"generations": [[{"message": {"kwargs": {"tool_calls": [], "invalid_tool_calls": [
        {"name": check.TASK_TOOL, "id": "call_x", "type": "invalid_tool_call"}]}}}]]})
    assert check.generation_calls(invalid) == [[check.TASK_TOOL]]


def test_the_window_reader_reads_llm_spans_in_place_within_the_window(tmp_path):
    import sqlite3

    db = tmp_path / "phoenix.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, status_code TEXT, attributes TEXT)")
        for span in RECORDED["phoenix"]["attempt1_m2"]["spans"] + RECORDED["phoenix"]["rc11_l2"]["spans"]:
            attributes = {"llm": {"model_name": span["model"]},
                          "output": {"mime_type": "application/json", "value": span["output_value"]}}
            conn.execute("INSERT INTO spans VALUES (?, ?, ?, ?)", (span["id"], "LLM", span["status_code"],
                                                                  json.dumps(attributes)))
        conn.execute("INSERT INTO spans VALUES (1640, 'TOOL', 'OK', '{}')")
    after, until = RECORDED["phoenix"]["attempt1_m2"]["window"]
    read = check.m2_generations(db, after, until)
    assert [span["id"] for span in read] == [s["id"] for s in RECORDED["phoenix"]["attempt1_m2"]["spans"]]
    assert read == _recorded_generations("attempt1_m2")


def test_attempt_1s_supervised_window_fails_m2_with_its_missing_call_recorded():
    run, observed = _passing()
    run["requests"]["M2"].update(cards_seen=0, cards_accepted=0)
    observed["tasks"] = []
    observed["rows"]["M2"] = {"status": "complete", "messages": [{"author_type": "ai", "message": "Using whoami.",
                                                                  "metadata": {}}]}
    observed["generations"]["M2"] = [dict(span, model=ROUTE["SUPERVISED_MODEL"])
                                     for span in _recorded_generations("attempt1_m2")]
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail" and result["criteria"]["M2"] is False
    assert "first generation" in result["detail"]["no_tool_call"]["M2"]
    assert result["detail"]["M2"]["later_generation_calls"] == [["get_ttp_guidance"], [check.TASK_TOOL]]
    assert all(value is True for name, value in result["criteria"].items() if name != "M2")


def test_a_recorded_task_call_without_a_card_fails_m2_and_with_its_card_passes():
    run, observed = _passing()
    assert check.evaluate(run, observed, ROUTE)["criteria"]["M2"] is True
    run["requests"]["M2"].update(cards_seen=0, cards_accepted=0)
    observed["tasks"] = []
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "fail" and result["criteria"]["M2"] is False


@pytest.mark.parametrize("change, expected", [
    # Another tool's call in the first generation: the model answered on the right route and never called the task.
    (lambda spans: [dict(s, generations=[["get_ttp_guidance"]]) for s in spans], False),
    # The task call only in a later generation (the split): Sage never received it, so M2 fails.
    (lambda spans: [dict(s, generations=[[], [check.TASK_TOOL]]) for s in spans], False),
    # A provider error, another model, or no LLM span at all: also a failure.
    (lambda spans: [dict(s, generations=[[]], status="ERROR") for s in spans], False),
    (lambda spans: [dict(s, generations=[[]], model="gpt-5.6-cyber") for s in spans], False),
    (lambda spans: [], False),
])
def test_m2s_gate_accepts_only_a_first_generation_task_call(change, expected):
    run, observed = _passing()
    run["requests"]["M2"].update(cards_seen=0, cards_accepted=0)
    observed["tasks"] = []
    observed["generations"]["M2"] = change(observed["generations"]["M2"])
    assert check.evaluate(run, observed, ROUTE)["criteria"]["M2"] is expected


def test_m3_needs_a_running_scan_above_the_status_and_a_paused_reply():
    run, observed = _passing()
    detail = check.evaluate(run, observed, ROUTE)["detail"]["M3"]
    assert (detail["before"]["scans"], detail["after"]["scans"], detail["after"]["state"]) == (0, 2, "running")
    assert detail["pause"]["state"] == "paused"
    # Attempt 1's sequence: status then a scan on a paused Watcher.
    observed["rows"]["M3_scan"] = _recorded_rows(48)
    assert check.evaluate(run, observed, ROUTE)["criteria"]["M3"] is False
    # Degraded only after a completed scan is accepted, as rc11 L5.
    run, observed = _passing()
    observed["rows"]["M3_scan"] = _rows("M3_scan", _status(scans=2, state="degraded", error="FindingReasoningError"))
    assert check.evaluate(run, observed, ROUTE)["criteria"]["M3"] is True


def test_mcp1_needs_bloodhound_connected_before_the_tool_list():
    run, observed = _passing()
    assert check.evaluate(run, observed, ROUTE)["criteria"]["MCP1"] is True
    observed["rows"]["MCP1_connect"]["status"] = "error"
    assert check.evaluate(run, observed, ROUTE)["criteria"]["MCP1"] is False


def test_the_sequence_sends_bloodhound_first_and_brackets_the_scan_with_resume_and_pause():
    order = [(name, prompt) for name, _phase, _key, prompt, *_rest in check.STEPS if _phase == "N"]
    assert order == [("M1", check.LIST), ("M2", check.WHOAMI), ("MCP1_connect", "/bloodhound"),
                     ("MCP1", "/mcp tools BloodHound"), ("M3_status", "/watcher status"),
                     ("M3_resume", "/watcher resume"), ("M3_scan", "/watcher scan"), ("M3_pause", "/watcher pause")]
    assert check.CRITERION_STEPS["MCP1"] == ("MCP1_connect", "MCP1")
    assert check.CRITERION_STEPS["M3"] == ("M3_status", "M3_resume", "M3_scan", "M3_pause")
    assert check.channel_config(ROUTE, "S")["config"]["model"] == ROUTE["SUPERVISED_MODEL"]
    assert check.channel_config(ROUTE, "S")["config"]["provider"] == "litellm"
    assert check.channel_config(ROUTE, "C")["config"]["model"] == ROUTE["GATEWAY_MODEL"]


def test_a_check_error_after_resume_leaves_m3_not_run():
    run, observed = _passing()
    _stopped_before(run, observed, "M3_scan")
    result = check.evaluate(run, observed, ROUTE)
    assert result["verdict"] == "not_run" and result["criteria"]["M3"] is None


def test_the_route_file_needs_the_exact_supervised_model(tmp_path):
    path = tmp_path / "route"
    path.write_text("\n".join(f"{k}={v}" for k, v in ROUTE.items()))
    path.chmod(0o600)
    assert check.read_route(path)["SUPERVISED_MODEL"] == "bedrock-claude-4-6-sonnet"
    for body, message in (("\n".join(f"{k}={v}" for k, v in ROUTE.items() if k != "SUPERVISED_MODEL"), "missing"),
                          (path.read_text().replace("bedrock-claude-4-6-sonnet", "gpt-5.6-cyber"), "SUPERVISED_MODEL")):
        path.write_text(body)
        with pytest.raises(SystemExit, match=message):
            check.read_route(path)


INSTALLED_LITELLM = "# LiteLLM\nAPI_ENDPOINT=https://gateway.invalid:7443\nAPI_KEY=litellm-fixture-key\n"


def _gateway_probe(**models):
    return {"container_env": [], "source": ".env.local:API_KEY", "endpoints_after_dotenv": ["API_ENDPOINT"],
            "status": 200, "tls_verified": True,
            "models": {ROUTE["GATEWAY_MODEL"]: True, ROUTE["SUPERVISED_MODEL"]: True, **models}}


def test_readiness_lists_the_gateway_and_supervised_models_with_the_installed_key():
    local = split.dotenv_state(INSTALLED_LITELLM, GATEWAY)
    assert check.gateway_blockers(local, _gateway_probe(), ROUTE) == []
    blockers = check.gateway_blockers(local, _gateway_probe(**{ROUTE["SUPERVISED_MODEL"]: False}), ROUTE)
    assert len(blockers) == 1 and "SUPERVISED_MODEL" in blockers[0]
    assert check.gateway_blockers(local, _gateway_probe(**{ROUTE["GATEWAY_MODEL"]: False}), ROUTE) != []


WATCHER_KEYS = ("SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT", "SAGE_WATCHER_API_KEY",
                "SAGE_WATCHER_APITOKEN")


def _watcher_probe(**changes):
    probe = {"present": {name: True for name in WATCHER_KEYS}, "provider": "openai",
             "model": "bedrock-claude-4-6-sonnet", "endpoint_is_gateway": True, "status": 200, "listed": True,
             "tls_verified": True}
    probe.update(changes)
    return probe


@pytest.mark.parametrize("changes, fragment", [
    ({}, None),
    ({"present": {**{name: True for name in WATCHER_KEYS}, "SAGE_WATCHER_APITOKEN": False}},
     "SAGE_WATCHER_APITOKEN"),
    ({"endpoint_is_gateway": False}, "gateway"),
    ({"status": 401, "listed": False}, "SAGE_WATCHER_MODEL"),
    ({"listed": False}, "SAGE_WATCHER_MODEL"),
    ({"error": "probe did not report", "present": {}}, "SAGE_WATCHER_PROVIDER"),
])
def test_readiness_proves_the_watcher_route_with_its_own_key(changes, fragment):
    blockers = check.watcher_route_blockers(_watcher_probe(**changes))
    assert (blockers == []) if fragment is None else any(fragment in blocker for blocker in blockers)


def test_a_user_secret_for_the_watchers_route_would_shadow_it():
    assert check.WATCHER_ROUTE_KEYS == WATCHER_KEYS
    assert {"SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT",
            "SAGE_WATCHER_API_KEY"} <= set(check.SHADOWING_SECRETS)


def test_cleanup_pauses_the_watcher_again_only_when_its_stored_profile_is_not_paused(monkeypatch):
    import asyncio

    sent = []

    class NC:
        async def create_message(self, _client, channel_id, prompt):
            sent.append((channel_id, prompt))
            return {"chat_request_id": 77}

        async def wait_for_request(self, _client, request_id, **_kwargs):
            return {"request": {"id": request_id, "status": "complete"}, "messages": []}

    class Base:
        nc = NC()

    monkeypatch.setattr(check, "_base", lambda: Base())
    assert asyncio.run(check.repause_watcher(None, {"owner_channel_id": OWNER, "paused": True})) is None
    assert sent == []
    result = asyncio.run(check.repause_watcher(None, {"owner_channel_id": OWNER, "paused": False}))
    assert sent == [(OWNER, "/watcher pause")] and result == {"request_id": 77, "status": "complete"}
    assert asyncio.run(check.repause_watcher(None, {"owner_channel_id": None, "paused": False})) is None


# ---------------------------------------------------------------------------------------------------------------
# M2's recorded baseline: Mythic's own task row, the stored cards and the follow-up, through M2's readers
# (fixtures/mythic_sdk_rc10_baseline_request85.json, provenance inside; every mutation below is synthetic)
# ---------------------------------------------------------------------------------------------------------------

BASELINE85 = json.loads((ROOT / "skills/sage-focused-capability-tests/tests/fixtures/mythic_sdk_rc10_baseline_request85.json")
                        .read_text())
PREREQ = json.loads((ROOT / "skills/sage-focused-capability-tests/tests/fixtures/mythic_sdk_rc10_prerequisites_recorded.json")
                    .read_text())
# Read on 2026-11-27 from Mythic's own rows: the transport reader decodes response rows with validate=True and rejects
# Mythic's base64, which Mythic wraps at 76 columns; the SDK's waitfor_for_task_output decodes it leniently. Changing a
# frozen reader is the operator's scope decision, taken before the live attempt.
WRAPPED_BASE64 = ("scope decision pending: the frozen transport reader rejects Mythic's 76-column base64 rows, which "
                  "the SDK decodes")


def _baseline_inputs(record: dict | None = None):
    record = copy.deepcopy(record or BASELINE85)
    return record["binding"], record["task"], record["rows"], {int(seq): r for seq, r in record["wire"].items()}


def _lifecycle_output() -> str:
    return next(m["tool_output"] for m in BASELINE85["rows"]["messages"]
                if m["metadata"]["tool_use"]["tool_call_id"] == "mythic-task:2:22")


def test_the_baseline_record_is_mythics_own_task_row_in_mythics_representation():
    binding, task, rows, wire = _baseline_inputs()
    original = BASELINE85["original"]
    assert binding == check.BASELINE
    assert [row["id"] for row in task["responses"]] == [25] and task["subtask_count"] == 0
    assert task["completed"] is True and task["status"] == "success" and task["callback"]["display_id"] == 2
    # Mythic's response row as Mythic returns it: base64 wrapped at 76 columns (the original had its line break at 76).
    assert original["response 25"]["newlines_at"] == [76] and "\n" in task["responses"][0]["response_text"]
    # The SDK's own decode (base64.b64decode per row, mythic.waitfor_for_task_output) yields the lifecycle card's value.
    assert base64.b64decode(task["responses"][0]["response_text"]).decode() == _lifecycle_output()
    # Independent sources agreed before redaction: Mythic's row, the stored card and the wire emission.
    assert (original["response 25"]["sha256"] == original["stored 873 tool_output"]["sha256"]
            == original["wire 13534 output"]["sha256"])
    assert original["stored 869 result_preview"]["sha256"] == original["wire 13588 tool message"]["sha256"]


def test_the_baseline_binds_every_input_to_its_request_channel_task_and_call():
    problems, formatted, correlation = check.baseline_bind(*_baseline_inputs())
    assert problems == []
    assert {name: correlation[name]["message_id"] for name in ("lifecycle", "call")} == {"lifecycle": 873, "call": 869}
    assert formatted.endswith(_lifecycle_output()) and formatted.startswith("[Mythic task issuance identity]")


def test_presentation_and_annotation_hold_on_the_recorded_baseline():
    positive = check.baseline_report(*_baseline_inputs())["positive"]
    assert positive["bind"]["holds"] and positive["presentation"]["holds"] and positive["annotation"]["holds"]
    assert positive["presentation"]["storage"] == "inline"


@pytest.mark.xfail(strict=True, reason=WRAPPED_BASE64)
def test_the_recorded_baseline_holds_through_all_three_readers():
    report = check.baseline_report(*_baseline_inputs())
    assert report["positive"]["transport"]["holds"], report["positive"]["transport"]["reasons"]
    assert report["holds"]


def test_the_frozen_transport_reader_holds_only_on_an_unwrapped_copy_of_the_recorded_row():
    """Synthetic: the recorded row re-encoded without Mythic's line wrapping. Documents the mismatch above."""
    binding, task, rows, wire = _baseline_inputs()
    assert check.baseline_holds(binding, task, rows, wire)["transport"]["reasons"] == ["response 25 is not base64"]
    task["responses"][0]["response_text"] = task["responses"][0]["response_text"].replace("\n", "")
    assert check.baseline_holds(binding, task, rows, wire)["holds"]


@pytest.mark.parametrize("name, owner", [(name, owner) for name, owner, _mutate in check.BASELINE_CONTROLS])
def test_each_baseline_control_fails_at_its_owner(name, owner, request):
    if owner == "transport":
        request.applymarker(pytest.mark.xfail(strict=True, reason=WRAPPED_BASE64))
    control = next(c for c in check.baseline_report(*_baseline_inputs())["controls"] if c["control"] == name)
    assert control["rejected"], control


def test_a_control_never_counts_when_its_owner_already_rejects_the_original():
    binding, task, rows, wire = _baseline_inputs()
    task["responses"][0]["response_text"] = task["responses"][0]["response_text"].replace("\n", "")
    healthy = {c["control"]: c["rejected"] for c in check.baseline_report(binding, task, rows, wire)["controls"]}
    assert all(healthy.values())  # every control rejected by its owner on the (synthetic) unwrapped row
    task["responses"] = []
    broken = check.baseline_report(binding, task, rows, wire)
    assert broken["holds"] is False
    assert {c["control"] for c in broken["controls"] if not c["rejected"]} == {
        name for name, owner, _m in check.BASELINE_CONTROLS if owner == "transport"}


def _receiver_file(path: Path, record: dict | None = None) -> Path:
    """The fixture's three records in the receiver's format (bodies re-encoded; the fixture holds them decoded)."""
    record = record or BASELINE85
    with path.open("w") as handle:
        handle.write(json.dumps({"seq": 1, "kind": "process_start", "record": {"payload": {}}}) + "\n")
        for seq, item in record["wire"].items():
            payload = {"body_base64": base64.b64encode(json.dumps(item["body"]).encode()).decode()}
            payload.update({k: item[k] for k in ("routing_key", "url") if item.get(k)})
            handle.write(json.dumps({"seq": int(seq), "kind": item["kind"], "record": {"payload": payload}}) + "\n")
    return path


def test_the_baseline_reads_the_receivers_records_from_their_bytes(tmp_path):
    wire = check.wire_bodies(_receiver_file(tmp_path / "wire.jsonl"), (13534, 13540, 13588, 99))
    assert sorted(wire) == [13534, 13540, 13588]
    expected = _baseline_inputs()[3]
    assert {seq: (r["kind"], r["body"]) for seq, r in wire.items()} == {
        seq: (r["kind"], r["body"]) for seq, r in expected.items()}


# ---------------------------------------------------------------------------------------------------------------
# M2's prerequisites through Sage's own readers (recorded inputs from fixtures/mythic_sdk_rc10_prerequisites_recorded.json;
# a fresh check-in time and every other mutation is synthetic)
# ---------------------------------------------------------------------------------------------------------------

import datetime as _dt  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

_MYTHIC_SDK = sys.modules[check._base().nc.mythic.__name__]
SYNTHETIC_KEY = "synthetic-test-key"


def _fresh(seconds_ago: float = 5) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)).replace(tzinfo=None).isoformat()


class FakeMythic:
    """Mythic's GraphQL answered from recorded rows by operation name; every call is kept. Tests change ``state``."""

    def __init__(self, **changes):
        self.calls: list[tuple[str, dict]] = []
        self.state = {"active": True, "exists": True, "last_checkin": _fresh(), "list_last_checkin": None,
                      "payload": "apollo", "whoami": True, "channel": copy.deepcopy(PREREQ["channel"]),
                      "token": copy.deepcopy(PREREQ["token"]), "container": copy.deepcopy(PREREQ["container"]),
                      "baseline": copy.deepcopy(BASELINE85), "created": []}
        self.state.update(changes)

    def _callback_row(self, last_checkin):
        row = copy.deepcopy(PREREQ["callback_row"])
        row.update(last_checkin=last_checkin, active=self.state["active"],
                   payload={"payloadtype": {"name": self.state["payload"]}})
        return row

    async def execute_custom_query(self, _client, query, variables=None, **_kwargs):
        name = re.search(r"(?:query|mutation)\s+(\w+)", query).group(1)
        variables = variables or {}
        self.calls.append((name, variables))
        s = self.state
        row = self._callback_row(s["last_checkin"])
        listed = self._callback_row(s["list_last_checkin"] or s["last_checkin"])
        answers = {
            "SageRc10Callback": lambda: {"callback": [{"display_id": 2, "active": s["active"]}] if s["exists"] else []},
            "cbinfo": lambda: {"callback": [row] if s["exists"] else []},
            "slimcb": lambda: {"callback": [{**listed, "user": "lab.user", "host": "WORKSTATION-A",
                                             "integrity_level": 2}] if s["exists"] and s["active"] else []},
            "SageRc10LoadedCommand": lambda: {"loadedcommands": [{"id": 7}] if s["whoami"] else []},
            "SageRc10Channel": lambda: {"chat_channel_by_pk": copy.deepcopy(s["channel"])
                                        if variables.get("id") == s["channel"]["id"] else
                                        {"id": variables.get("id"), "ai_metadata": {"config": {}}}},
            "SageRc10Token": lambda: {"apitokens_by_pk": copy.deepcopy(s["token"])
                                      if variables.get("id") == s["token"]["id"] else None},
            "SageOperationBot": lambda: {"operation_by_pk": PREREQ["operation"], "operator": [PREREQ["bot"]]},
            "SageChatReadiness": lambda: {"consuming_container": [s["container"]], "apitokens": [s["token"]]},
            "CreateSageChannel": lambda: (s["created"].append(variables["name"])
                                          or {"chatCreateChannel": {"status": "success", "channel_id": (
                                              s["channel"]["id"] if variables["name"].endswith(" S")
                                              else 60 + len(s["created"]))}}),
            "SageRc10BaselineTask": lambda: {"task": [s["baseline"]["task"]]},
            "SageRc10Subtasks": lambda: {"task": []},
            "SageRc10BaselineRows": lambda: {"chat_message": s["baseline"]["rows"]["messages"],
                                             "chat_request_by_pk": s["baseline"]["rows"]["request"]},
            "SageOpenAICheckMaxTask": lambda: {"task": [{"display_id": 22}]},
            "SageRc10Tasks": lambda: {"task": []},
        }
        return copy.deepcopy(answers[name]())


def _probe_root(tmp_path: Path, endpoint: str = GATEWAY) -> Path:
    """Synthetic installed files: the LiteLLM block only, with a synthetic key."""
    root = tmp_path / "installed"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("")
    (root / ".env.local").write_text(f"# LiteLLM\nAPI_ENDPOINT={endpoint}\nAPI_KEY={SYNTHETIC_KEY}\n")
    return root


def run_probe_here(script: str, argv: list[str], root: Path, *, status: int = 200, models=None,
                   environ: dict | None = None) -> dict:
    """Run a container probe's own script in this process against a synthetic installed root. Sage's real dotenv
    loader, build_model_kwargs, resolve_llm_profile and TraceConfig run; only the keyed listing's HTTP answer is
    instrumented (it records the URL and the key it was sent)."""
    import contextlib
    import io

    import httpx

    sent = []

    class Response:
        status_code = status

        def json(self):
            return {"data": [{"id": model} for model in (models if models is not None else [ROUTE["SUPERVISED_MODEL"]])]}

    def get(url, headers=None, **_kwargs):
        sent.append((url, (headers or {}).get("Authorization")))
        return Response()

    saved = (dict(os.environ), list(sys.path), list(sys.argv), httpx.get)
    out = io.StringIO()
    try:
        for name in ("API_KEY", "OPENAI_API_KEY", "API_ENDPOINT", "OPENAI_API_BASE", "OPENAI_BASE_URL", "provider",
                     "model", "SAGE_OBSERVATION_DIRECTORY", "mode", "max_steps", "autonomous_solve"):
            os.environ.pop(name, None)
        os.environ.update(environ or {})
        os.environ["SAGE_RC10_PROBE_ROOT"] = str(root)
        sys.argv, httpx.get = ["-c", *argv], get
        with contextlib.redirect_stdout(out):
            exec(compile(script, "<probe>", "exec"), {"__name__": "__main__"})  # noqa: S102 - the probe under test
    finally:
        os.environ.clear()
        os.environ.update(saved[0])
        sys.path[:], sys.argv, httpx.get = saved[1], saved[2], saved[3]
    result = json.loads(out.getvalue().strip().splitlines()[-1])
    result["_sent"] = sent
    return result


def test_the_s_route_probe_resolves_s_through_sages_own_profile_and_lists_with_that_key(tmp_path):
    root = _probe_root(tmp_path)
    config = PREREQ["channel"]["ai_metadata"]["config"]
    probe = run_probe_here(check._S_ROUTE_PROBE, [json.dumps(config), GATEWAY, ROUTE["SUPERVISED_MODEL"]], root)
    sent = probe.pop("_sent")
    recorded = {k: v for k, v in PREREQ["s_probe"].items()}
    assert probe == recorded  # the same projection the deployed container reported for channel 53
    assert sent == [(GATEWAY + "/models", "Bearer " + SYNTHETIC_KEY)]
    # Sage's own normalizers decide; a near-match in S's settings changes what they resolve.
    unsafe = {**config, "mode": "auto", "max_steps": "4", "provider": "openai"}
    changed = run_probe_here(check._S_ROUTE_PROBE, [json.dumps(unsafe), GATEWAY, ROUTE["SUPERVISED_MODEL"]], root)
    assert changed["effective"]["mode"] == "auto" and changed["effective"]["autonomous_solve"] is True
    assert changed["effective"]["max_steps"] == 4 and changed["effective"]["provider"] == "openai"
    assert check.effective_policy_reasons(changed, ROUTE)
    other = run_probe_here(check._S_ROUTE_PROBE, [json.dumps(config), GATEWAY, ROUTE["SUPERVISED_MODEL"]],
                           _probe_root(tmp_path / "x", endpoint="https://elsewhere.invalid"))
    assert other["effective"]["endpoint_is_gateway"] is False


def test_the_observation_probe_reads_tracing_as_sage_would_start_it(tmp_path):
    root = _probe_root(tmp_path)
    normal = run_probe_here(check._OBSERVATION_PROBE, list(check.TRACE_INPUT_FLAGS), root)
    normal.pop("_sent")
    assert normal == PREREQ["observation"]
    hidden = run_probe_here(check._OBSERVATION_PROBE, list(check.TRACE_INPUT_FLAGS), root,
                            environ={"OPENINFERENCE_HIDE_INPUT_MESSAGES": "true"})
    assert hidden["hidden"]["hide_input_messages"] is True
    (root / ".env.local").write_text((root / ".env.local").read_text() + "SAGE_OBSERVATION_DIRECTORY=/tmp/obs\n")
    capture = run_probe_here(check._OBSERVATION_PROBE, list(check.TRACE_INPUT_FLAGS), root)
    assert capture["capture_mode"] is True
    for probe in (hidden, capture):
        assert check.result_holds({"holds": True}, {"span": 1, "text": True}, probe)["holds"] is False
    assert check.result_holds({"holds": True}, {"span": 1, "text": True}, normal)["holds"] is True


def _callback(monkeypatch, **changes):
    fake = FakeMythic(**changes)
    monkeypatch.setattr(_MYTHIC_SDK, "execute_custom_query", fake.execute_custom_query)
    return asyncio_run(check.read_callback(object(), 2))


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_the_recorded_dead_callback_fails_although_mythic_marks_it_active(monkeypatch):
    result = _callback(monkeypatch, last_checkin=PREREQ["callback_row"]["last_checkin"])
    assert result["observed"]["active"] is True  # the old active-only admission let this callback through
    assert result["holds"] is False
    assert result["observed"]["detailed"]["status"] == "dead" and result["observed"]["listed"]["status"] == "dead"
    assert result["observed"]["detailed"]["threshold_seconds"] == 180
    # Attempt 1's request 88: the model-visible list said dead for the same callback.
    assert check.callback_holds(2, [{"display_id": 2, "active": True}], {"display_id": 2, "status": "alive",
                                "alive": True}, PREREQ["request_88_list_result"])["holds"] is False


@pytest.mark.parametrize("changes, fragment", [
    ({}, None),  # synthetic fresh check-in: both readers call it alive
    ({"exists": False}, "not active"),
    ({"active": False}, "not active"),
    ({"last_checkin": "yesterday-ish"}, "reports uncertain"),
    ({"last_checkin": None}, "reports uncertain"),
    ({"payload": "sage"}, "reports taskable"),
    ({"list_last_checkin": PREREQ["callback_row"]["last_checkin"]}, "list_callbacks reports dead"),
])
def test_the_callback_condition_needs_both_of_sages_readers_to_call_it_alive(monkeypatch, changes, fragment):
    result = _callback(monkeypatch, **changes)
    if fragment is None:
        assert result["holds"] is True, result
        assert result["observed"]["detailed"]["status"] == result["observed"]["listed"]["status"] == "alive"
    else:
        assert result["holds"] is False and any(fragment in reason for reason in result["reasons"]), result


def test_the_callback_condition_reads_the_exact_selected_callback_only():
    alive = {"display_id": 3, "status": "alive", "alive": True}
    listed = [{"id": 3, "status": "alive"}]
    assert check.callback_holds(2, [{"display_id": 3, "active": True}], alive, listed)["holds"] is False
    assert check.callback_holds(2, [], {}, {"status": "unchanged", "note": "re-read"})["holds"] is False
    assert check.callback_holds(2, [{"display_id": 2, "active": True}], {**alive, "display_id": 2},
                                [{"id": 2, "status": "alive"}, {"id": 2, "status": "alive"}])["holds"] is False


def test_the_command_condition_needs_whoami_loaded():
    assert check.command_holds(2, [{"id": 7}])["holds"] is True
    assert check.command_holds(2, [])["holds"] is False
    assert check.command_holds(2, None)["holds"] is False


def _channel(**changes):
    channel, token = copy.deepcopy(PREREQ["channel"]), copy.deepcopy(PREREQ["token"])
    probe = copy.deepcopy(PREREQ["s_probe"])
    for path, value in changes.items():
        target, key = {"channel": channel, "token": token, "config": channel["ai_metadata"]["config"],
                       "effective": probe["effective"]}[path.split(".")[0]], path.split(".")[1]
        target[key] = value
    return check.channel_holds(channel, token, PREREQ["bot"], PREREQ["container"], 1, token.get("active") is True
                               and token.get("deleted") is False, probe, ROUTE)


@pytest.mark.parametrize("change", [
    {"channel.operation_id": 2}, {"channel.chat_container_id": 9}, {"channel.locked": False},
    {"channel.chat_model": "Sage Watcher"}, {"channel.apitokens_id": 32}, {"config.provider": "openai"},
    {"config.model": ROUTE["GATEWAY_MODEL"]}, {"config.mode": "auto"}, {"config.autonomous_solve": True},
    {"config.max_steps": 4}, {"config.API_KEY": "inline"}, {"token.active": False}, {"token.deleted": True},
    {"token.operator_id": 1}, {"token.operation_id": 2}, {"token.scopes": ["chat-ai.read"]},
    {"effective.mode": "conversation"}, {"effective.max_steps": 200},
])
def test_the_actual_s_and_its_token_hold_only_as_created(change):
    assert _channel()["holds"] is True  # attempt 1's actual S (channel 53) and token 31, read back from Mythic
    result = _channel(**change)
    assert result["holds"] is False, change


def test_readiness_can_only_report_s_as_pending():
    intended = check.intended_channel(PREREQ["s_probe"], ROUTE)
    assert intended["holds"] is None and intended["pending"] is True
    bad = copy.deepcopy(PREREQ["s_probe"])
    bad["effective"]["mode"] = "conversation"
    assert check.intended_channel(bad, ROUTE)["holds"] is False


@pytest.mark.parametrize("part, change", [
    (None, None),
    ("probe", {"sources": {**PREREQ["s_probe"]["sources"], "model": "user-secret"}}),
    ("probe", {"sources": {**PREREQ["s_probe"]["sources"], "api_key": "default"}}),
    ("probe", {"effective": {**PREREQ["s_probe"]["effective"], "endpoint_is_gateway": False}}),
    ("probe", {"effective": {**PREREQ["s_probe"]["effective"], "model": ROUTE["GATEWAY_MODEL"]}}),
    ("probe", {"status": 401, "listed": False}),
    ("probe", {"container_env": ["API_KEY"]}),
    ("probe", {"error": "ImportError", "effective": None}),
    ("secrets", {"names": ["API_KEY"]}),
    ("secrets", {"resolved": False}),
    ("local", {"credentials": [{"name": "API_KEY", "block": "OpenAI"}]}),
    ("gateway", {"status": 401}),
])
def test_the_route_condition_follows_s_to_its_effective_gateway_and_key(part, change):
    parts = {"local": copy.deepcopy(PREREQ["installed_local"]), "gateway": copy.deepcopy(PREREQ["gateway_probe"]),
             "probe": copy.deepcopy(PREREQ["s_probe"]), "secrets": copy.deepcopy(PREREQ["secrets"])}
    if part:
        parts[part].update(change)
    result = check.route_holds(parts["local"], parts["gateway"], parts["probe"], parts["secrets"], ROUTE)
    assert result["holds"] is (part is None), result["reasons"]


# ---------------------------------------------------------------------------------------------------------------
# Preflight to send: cmd_run with Mythic, the container, sage-line and cleanup replaced by recorded answers, STEPS cut to
# M1 and M2, and the send instrumented. The admission code and Sage's readers stay real.
# ---------------------------------------------------------------------------------------------------------------

# Synthetic for wiring only: the recorded row re-encoded without Mythic's line wrap, so the result condition can hold
# while the frozen reader's mismatch (WRAPPED_BASE64) awaits its scope decision. The recorded row is used below to show
# that the mismatch alone keeps M2 unsent.
UNWRAPPED85 = copy.deepcopy(BASELINE85)
UNWRAPPED85["task"]["responses"][0]["response_text"] = UNWRAPPED85["task"]["responses"][0]["response_text"].replace(
    "\n", "")


class Harness:
    def __init__(self, monkeypatch, tmp_path, **state):
        import argparse
        import sqlite3
        import uuid

        tmp_path = tmp_path / uuid.uuid4().hex[:8]
        tmp_path.mkdir()
        self.evidence = tmp_path / "evidence"
        self.fake = FakeMythic(baseline=copy.deepcopy(UNWRAPPED85), **state)
        self.sent: list[tuple[str, int, str]] = []
        self.root = _probe_root(tmp_path)
        self.probe_status = 200
        self.trace_env: dict = {}
        phoenix = tmp_path / "phoenix.db"
        with sqlite3.connect(phoenix) as conn:
            conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, status_code TEXT, events TEXT, "
                         "attributes TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER)")
            span = {"llm": {"model_name": "m", "input_messages": [
                {"message": {"role": "tool", "tool_call_id": "call_x", "content": "a recorded result"}}]}}
            conn.execute("INSERT INTO spans VALUES (1, 'LLM', 'OK', '[]', ?, 1, 1)", (json.dumps(span),))
        self.args = argparse.Namespace(
            callback=2, bloodhound_channel=3, phoenix_db=str(phoenix), phoenix_url="http://127.0.0.1:6006",
            route_file="route", plan="plan.md", authorized_by="test", pip_audit="pip-audit",
            baseline_wire=str(_receiver_file(tmp_path / "wire.jsonl", UNWRAPPED85)))
        self.run = {"readiness": {"verdict": "ready_for_preparation"}, "evidence_dir": str(tmp_path / "evidence")}
        nc = check._base().nc
        monkeypatch.setattr(_MYTHIC_SDK, "execute_custom_query", self.fake.execute_custom_query)
        monkeypatch.setattr(nc, "_read_env_defaults", lambda path=None: {})
        monkeypatch.setattr(check, "STEPS", check.STEPS[:2])
        monkeypatch.setattr(check, "read_route", lambda _path: dict(ROUTE))
        monkeypatch.setattr(check, "_container_json", self._container)
        monkeypatch.setattr(check, "gateway_probe", lambda _route: copy.deepcopy(PREREQ["gateway_probe"]))
        monkeypatch.setattr(check._split(), "_operator_secret_names",
                            lambda _client: _value(copy.deepcopy(PREREQ["secrets"])))
        monkeypatch.setattr(check._deployment(), "_installed_service_dir", lambda: str(self.root))
        monkeypatch.setattr(check, "record_a1", lambda _bin: {})
        monkeypatch.setattr(check, "ss_listeners", lambda: {})
        monkeypatch.setattr(check, "graphql_unauthenticated", lambda _url: 401)
        monkeypatch.setattr(check, "http_status", lambda *_a, **_k: 200)
        monkeypatch.setattr(check, "watcher_profile", lambda _op: {"owner_channel_id": OWNER, "paused": True})
        monkeypatch.setattr(check, "_totals", lambda _run, _db: {"provider_calls": 0, "provider_tokens": 0})
        monkeypatch.setattr(check, "cmd_cleanup", lambda *_a: _value({"cleaned": True}))
        monkeypatch.setattr(check.subprocess, "run", self._subprocess)
        monkeypatch.setattr(check, "_send", self._send)
        monkeypatch.setattr(check._base(), "phoenix_mark", lambda _db: 1)
        self.before_send: dict = {}

    def _container(self, script, *argv, timeout=90):
        if script == check._S_ROUTE_PROBE:
            return run_probe_here(script, list(argv), self.root, status=self.probe_status)
        return run_probe_here(script, list(argv), self.root, environ=self.trace_env)

    def _subprocess(self, command, **_kwargs):
        assert command[:2] == ["sage-line", "check-attempt"], command  # nothing else may run a process here
        return type("Done", (), {"returncode": 0, "stdout": "allowed", "stderr": ""})()

    async def _send(self, client, run, name, channel_id, prompt, *, cards, timeout):
        self.sent.append((name, channel_id, prompt))
        entry = {"channel_id": channel_id, "request_id": 100 + len(self.sent), "sends": 1, "sent_ns": 1}
        run["requests"][name] = entry
        for key, value in self.before_send.get(name, {}).items():  # a change after this step, before the next
            self.fake.state[key] = value
        return entry

    def cmd_run(self):
        client = type("Client", (), {"current_operation_id": 1})()
        return asyncio_run(check.cmd_run(client, self.run, self.args))


async def _async_value(value):
    return value


def _value(value):
    return _async_value(value)


def _stages(run):
    return [(entry["stage"], entry["condition"], entry["holds"]) for entry in run.get("prerequisites", [])]


def test_valid_prerequisites_reach_both_sends_and_record_each_condition(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.cmd_run()
    assert [name for name, *_ in h.sent] == ["M1", "M2"]
    assert h.sent[1] == ("M2", PREREQ["channel"]["id"], "Run whoami on callback 2.")
    assert h.run["preparation"]["verdict"] == "ready_for_live" and not h.run.get("stopped")
    assert _stages(h.run) == ([("run_entry", "callback", True)]
                              + [("preparation", c, True) for c in check.PREREQUISITES]
                              + [("before_M2", c, True) for c in check.PREREQUISITES])
    for entry in h.run["prerequisites"]:
        assert entry["reader"] and isinstance(entry["observed_at"], float) and "observed" in entry
    assert h.fake.state["created"] == ["rc10 live check S", "rc10 live check C"]  # S once, in preparation
    assert h.sent[0][1] != h.sent[1][1] and h.run["channels"]["S"] == PREREQ["channel"]["id"]
    assert json.dumps(h.run, default=str).count(SYNTHETIC_KEY) == 0


@pytest.mark.parametrize("condition, change", [
    ("callback", {"list_last_checkin": PREREQ["callback_row"]["last_checkin"]}),
    ("command", {"whoami": False}),
    ("channel", {"channel": {**PREREQ["channel"], "locked": False}}),
    ("route", "probe_status"),
    ("result", "trace_env"),
    ("result", {"baseline": copy.deepcopy(BASELINE85)}),  # the recorded row: the frozen reader's mismatch alone
])
def test_each_failed_prerequisite_at_preparation_sends_nothing(monkeypatch, tmp_path, condition, change):
    h = Harness(monkeypatch, tmp_path)
    if change == "probe_status":
        h.probe_status = 401
    elif change == "trace_env":
        h.trace_env = {"OPENINFERENCE_HIDE_INPUT_MESSAGES": "true"}
    else:
        h.fake.state.update(copy.deepcopy(change))
    if condition == "callback":
        # the list reader alone disagrees at preparation: the run-entry read happened before this point
        h.fake.state["list_last_checkin"] = None
        original = check.check_prerequisites

        async def flip_after_entry(client, run, args, route, stage, channel_id=None):
            if stage == "preparation":
                h.fake.state["list_last_checkin"] = PREREQ["callback_row"]["last_checkin"]
            return await original(client, run, args, route, stage, channel_id)

        monkeypatch.setattr(check, "check_prerequisites", flip_after_entry)
    with pytest.raises(SystemExit, match="preparation: .*no prompt was sent"):
        h.cmd_run()
    assert h.sent == []
    assert h.run["preparation"]["verdict"] == "not_ready" and "requests" not in h.run
    assert not list(h.evidence.glob("installed.env*"))  # nothing backed up, nothing changed
    failed = [entry["condition"] for entry in h.run["prerequisites"] if entry["holds"] is False]
    assert failed == [condition]


def test_a_saved_green_readiness_with_a_dead_callback_refuses_at_entry(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, last_checkin=PREREQ["callback_row"]["last_checkin"])
    with pytest.raises(SystemExit, match="run entry: callback: .*nothing was changed or sent"):
        h.cmd_run()
    assert h.sent == [] and h.fake.state["created"] == [] and "preparation" not in h.run
    assert _stages(h.run) == [("run_entry", "callback", False)]


@pytest.mark.parametrize("condition, change", [
    ("callback", {"last_checkin": PREREQ["callback_row"]["last_checkin"]}),
    ("command", {"whoami": False}),
    ("channel", {"channel": {**PREREQ["channel"], "locked": False}}),
    ("channel", {"token": {**PREREQ["token"], "deleted": True}}),
    ("route", {"channel": {**PREREQ["channel"], "ai_metadata": {"config": {
        **PREREQ["channel"]["ai_metadata"]["config"], "model": "gpt-5.6-cyber"}}}}),
])
def test_a_change_after_preparation_leaves_m2_unsent_and_m2_not_run(monkeypatch, tmp_path, condition, change):
    h = Harness(monkeypatch, tmp_path)
    h.before_send = {"M1": copy.deepcopy(change)}
    h.cmd_run()
    assert [name for name, *_ in h.sent] == ["M1"]
    assert h.run["stopped"]["step"] == "M2"
    assert h.run["stopped"]["reason"].startswith("phase N not ready: before M2: ")
    assert condition in [e["condition"] for e in h.run["prerequisites"] if e["stage"] == "before_M2" and not e["holds"]]
    # The existing disposition: a check-side stop leaves the unsent row not run; M2 can never pass this way.
    verdict = check.evaluate(h.run, {"rows": {"M1": _rows("M1")}}, ROUTE)
    assert verdict["criteria"]["M2"] is None and verdict["verdict"] != "pass"


def test_the_run_refuses_a_run_file_without_a_preparation_ready_readiness(monkeypatch, tmp_path):
    for verdict in ("ready_for_live", "not_ready", None):  # the old aggregate verdict is not accepted either
        h = Harness(monkeypatch, tmp_path)
        h.run["readiness"] = {"verdict": verdict}
        with pytest.raises(SystemExit, match="ready_for_preparation"):
            h.cmd_run()
        assert h.sent == [] and h.fake.calls == []
    h = Harness(monkeypatch, tmp_path)
    h.run["preparation"] = {"verdict": "not_ready"}
    with pytest.raises(SystemExit, match="already holds"):
        h.cmd_run()


def test_readiness_reads_every_prerequisite_and_leaves_s_pending(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    failing = asyncio_run(check.check_prerequisites(type("Client", (), {"current_operation_id": 1})(), h.run, h.args,
                                                    dict(ROUTE), "readiness"))
    assert failing == []
    entries = {e["condition"]: e for e in h.run["prerequisites"]}
    assert list(entries) == list(check.PREREQUISITES)
    assert entries["channel"]["holds"] is None and entries["channel"]["pending"] is True
    assert all(entries[c]["holds"] is True for c in ("callback", "command", "route", "result"))
    assert h.fake.state["created"] == []  # readiness creates no channel
