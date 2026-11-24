"""Offline checks for the dependency-advisories live check's local logic (no Mythic, no provider, no container).

Span windows, Mythic rows and dotenv files below are synthetic, shaped like Phoenix's spans table, Mythic's chat rows
and the installed dotenv files. They prove the pass rules accept a correct set and reject each falsifier, and that the
span reader extracts calls and results from Phoenix's nested attribute shape.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_dependency_advisories.py"
SPEC = importlib.util.spec_from_file_location("check_dependency_advisories", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

MODEL = "route-model"
ENDPOINT = "https://provider.invalid:7443"


def _llm(span_id, *, produced=(), input_calls=(), results=(), status="OK", exceptions=(), model=MODEL):
    return {"id": span_id, "kind": "LLM", "status": status, "exceptions": list(exceptions), "model": model,
            "prompt_tokens": 100, "completion_tokens": 10, "produced_calls": list(produced),
            "input_calls": list(input_calls), "consumed_results": list(results)}


def _chain(span_id, exceptions=()):
    return {"id": span_id, "kind": "CHAIN", "status": "OK", "exceptions": list(exceptions)}


def _rows(status="complete", text="Two callbacks: WORKSTATION-A as lab.user."):
    return {"status": status, "messages": [{"message": text, "metadata": {}}]}


def _passing():
    run = {"route": {"MODEL": MODEL}, "task_baseline": 41, "channels": {"conversation": 1, "supervised": 2},
           "stored_endpoints": {"conversation": True, "supervised": True}, "container_openai": "3.58.0",
           "phoenix_health": [200, 200, 200, 200, 200], "started_at": 0.0, "finished_at": 300.0}
    windows = {
        "C1": [_llm(1, produced=["call_a"]), _chain(2), _llm(3, input_calls=["call_a"], results=["call_a"])],
        "C2": [_llm(4, input_calls=["call_a"], results=["call_a"]), _chain(5)],
        "S1": [_llm(6), _chain(7, exceptions=["langgraph.errors.GraphInterrupt"])],
    }
    rows = {"C1": _rows(), "C2": _rows(text="Callback 3 runs at integrity 3."),
            "S1": _rows(text="Suggested next step: enumerate shares.")}
    return run, windows, rows


def test_a_correct_set_passes():
    run, windows, rows = _passing()
    result = check.evaluate(run, windows, rows, 41)
    assert result["verdict"] == "pass", result["criteria"]
    assert result["observations"]["p2_requests"] == ["C1"]
    assert result["observations"]["c2_carried_c1_calls"] == ["call_a"]


def test_no_provider_tool_call_means_the_check_did_not_run():
    run, windows, rows = _passing()
    for window in windows.values():
        for span in window:
            if span["kind"] == "LLM":
                span.update(produced_calls=[], input_calls=[], consumed_results=[])
    assert check.evaluate(run, windows, rows, 41)["verdict"] == "did_not_run"


@pytest.mark.parametrize(("criterion", "mutate"), [
    ("P1", lambda run, windows, rows: windows["C2"][0].update(status="ERROR")),
    ("P1", lambda run, windows, rows: windows["S1"][1]["exceptions"].append("openai.APIConnectionError")),
    ("P1", lambda run, windows, rows: windows["C1"][1]["exceptions"].append("ssl.SSLCertVerificationError")),
    ("P1", lambda run, windows, rows: windows["C1"][0].update(model="another-model")),
    ("P1", lambda run, windows, rows: run.update(container_openai="2.54.0")),
    ("P1", lambda run, windows, rows: run["stored_endpoints"].update(supervised=False)),
    ("P2", lambda run, windows, rows: rows["C1"].update(status="error")),
    ("P2", lambda run, windows, rows: windows["C1"][2].update(consumed_results=[])),
    ("P3", lambda run, windows, rows: windows["C2"][0].update(input_calls=[], consumed_results=[])),
    ("P3", lambda run, windows, rows: windows["C2"][0].update(input_calls=["call_other"],
                                                              consumed_results=["call_other"])),
    ("P4", lambda run, windows, rows: rows["S1"].update(status="running")),
    ("P4", lambda run, windows, rows: rows["S1"]["messages"].append(
        {"message": "Invalid `http_client` argument; this SDK uses httpx2", "metadata": {}})),
    ("P5", lambda run, windows, rows: run["phoenix_health"].append(None)),
    ("P5", lambda run, windows, rows: windows.update(S1=[])),
    ("P6", lambda run, windows, rows: run.update(stopped_for_task=True)),
    # The check itself stopped S1 (timeout or approval-card limit): Mythic records it cancelled.
    ("P4", lambda run, windows, rows: rows["S1"].update(status="cancelled")),
    ("P4", lambda run, windows, rows: run.update(requests={"S1": {"timeout": "request timeout"}})),
    ("P7", lambda run, windows, rows: run.update(requests={"S1": {"timeout": "approval card limit"}})),
    ("P7", lambda run, windows, rows: run.update(stopped={"request": "C2", "reason": "provider call limit"})),
    ("P7", lambda run, windows, rows: rows.pop("S1")),
])
def test_each_falsifier_fails_its_criterion(criterion, mutate):
    run, windows, rows = copy.deepcopy(_passing())
    mutate(run, windows, rows)
    result = check.evaluate(run, windows, rows, 41)
    assert result["criteria"][criterion] is False
    assert result["verdict"] == "fail"


def test_failed_provider_calls_without_a_tool_call_are_a_failed_attempt_not_a_check_that_did_not_run():
    run, windows, rows = _passing()
    for window in windows.values():
        for span in window:
            if span["kind"] == "LLM":
                span.update(produced_calls=[], input_calls=[], consumed_results=[], status="ERROR",
                            exceptions=["openai.APIConnectionError"])
    assert check.evaluate(run, windows, rows, 41)["verdict"] == "fail"


def test_a_created_task_fails_p6():
    run, windows, rows = _passing()
    assert check.evaluate(run, windows, rows, 42)["criteria"]["P6"] is False


def test_installed_credentials_report_names_and_booleans_only(tmp_path):
    (tmp_path / ".env.local").write_text(
        "SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS=1\n# LiteLLM\nprovider=openai\nmodel=route-model\n"
        f"API_ENDPOINT={ENDPOINT}/\nAPI_KEY=synthetic-secret-never-reported\n# OpenAI\n#API_KEY=commented\n")
    (tmp_path / ".env").write_text("LANGGRAPH_STRICT_MSGPACK=true\n")
    report = check.installed_credentials(tmp_path, ENDPOINT)
    assert "synthetic-secret-never-reported" not in json.dumps(report)
    assert report[".env.local"] == {"present": True, "credentials": ["API_KEY"], "endpoint_matches_route": [True],
                                    "provider_is_openai": [True],
                                    "active_eval_flags": ["SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS"]}
    blockers = check.credential_blockers({
        "installed": report, "container": {"container_env": [], "source": ".env.local:API_KEY", "status": 200,
                                           "models": {MODEL: True}, "tls_verified": True},
        "operator_secrets": {"resolved": True, "names": []}, "channel_config_sets_key": False},
        {"MODEL": MODEL, "API_ENDPOINT": ENDPOINT})
    assert blockers == []


@pytest.mark.parametrize(("local", "probe", "secrets", "expected"), [
    ("API_KEY=a\nOPENAI_API_KEY=b\nprovider=openai\nAPI_ENDPOINT=" + ENDPOINT, {}, [], "exactly one active API_KEY"),
    ("API_KEY=a\nprovider=openai\nAPI_ENDPOINT=https://other.invalid", {}, [], "exactly one active provider block"),
    ("API_KEY=a\nprovider=anthropic\nAPI_ENDPOINT=" + ENDPOINT, {}, [], "exactly one active provider block"),
    ("API_KEY=a\nprovider=openai\nAPI_ENDPOINT=" + ENDPOINT, {"container_env": ["API_KEY"]}, [], "container environment"),
    ("API_KEY=a\nprovider=openai\nAPI_ENDPOINT=" + ENDPOINT, {"status": 401}, [], "keyed verified-TLS"),
    ("API_KEY=a\nprovider=openai\nAPI_ENDPOINT=" + ENDPOINT, {"tls_verified": False}, [], "keyed verified-TLS"),
    ("API_KEY=a\nprovider=openai\nAPI_ENDPOINT=" + ENDPOINT, {}, ["API_KEY"], "user secret would shadow"),
])
def test_each_credential_falsifier_blocks(tmp_path, local, probe, secrets, expected):
    (tmp_path / ".env.local").write_text(local + "\n")
    container = {"container_env": [], "source": ".env.local:API_KEY", "status": 200, "models": {MODEL: True},
                 "tls_verified": True, **probe}
    blockers = check.credential_blockers({
        "installed": check.installed_credentials(tmp_path, ENDPOINT), "container": container,
        "operator_secrets": {"resolved": True, "names": secrets}, "channel_config_sets_key": False},
        {"MODEL": MODEL, "API_ENDPOINT": ENDPOINT})
    assert any(expected in blocker for blocker in blockers), blockers


def test_the_route_file_refuses_a_key_and_loose_permissions(tmp_path):
    route = tmp_path / "route"
    route.write_text(f"API_ENDPOINT={ENDPOINT}\nMODEL={MODEL}\n")
    route.chmod(0o600)
    assert check.read_route(route) == {"API_ENDPOINT": ENDPOINT, "MODEL": MODEL}
    assert "API_KEY" not in check.channel_config(check.read_route(route), "conversation")["config"]
    route.chmod(0o644)
    with pytest.raises(SystemExit, match="owner-only"):
        check.read_route(route)
    route.chmod(0o600)
    route.write_text(f"API_ENDPOINT={ENDPOINT}\nMODEL={MODEL}\nAPI_KEY=x\n")
    with pytest.raises(SystemExit, match="must not contain an API key"):
        check.read_route(route)


def test_the_span_reader_extracts_calls_and_results_from_phoenix_attributes(tmp_path):
    db = tmp_path / "phoenix.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, status_code TEXT, events TEXT, "
                     "attributes TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER)")
        attributes = {"llm": {"model_name": MODEL,
                              "input_messages": [
                                  {"message": {"role": "assistant",
                                               "tool_calls": [{"tool_call": {"id": "call_a"}}]}},
                                  {"message": {"role": "tool", "tool_call_id": "call_a"}}],
                              "output_messages": [
                                  {"message": {"role": "assistant",
                                               "tool_calls": [{"tool_call": {"id": "call_b"}}]}}]}}
        conn.execute("INSERT INTO spans VALUES (1, 'LLM', 'OK', '[]', ?, 120, 7)", (json.dumps(attributes),))
        conn.execute("INSERT INTO spans VALUES (2, 'CHAIN', 'ERROR', ?, '{}', NULL, NULL)",
                     (json.dumps([{"name": "exception", "attributes": {"exception.type": "openai.APIConnectionError"}}]),))
    window = check.spans(db, 0, 2)
    assert window[0] == {"id": 1, "kind": "LLM", "status": "OK", "exceptions": [], "model": MODEL,
                         "prompt_tokens": 120, "completion_tokens": 7, "produced_calls": ["call_b"],
                         "input_calls": ["call_a"], "consumed_results": ["call_a"]}
    assert window[1]["exceptions"] == ["openai.APIConnectionError"]
    assert check.TRANSPORT_EXCEPTION.search(window[1]["exceptions"][0])
    assert not check.TRANSPORT_EXCEPTION.search("langgraph.errors.GraphInterrupt")
