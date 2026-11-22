"""The live check's frozen A/B sequence, offline, under the check's exact settings, graded by the check itself.

Real HeadlessSageChat, Model, graph, checkpointer, base-model factory, Transient v1 retry policy, metered provider
transport, resource ledger (the check's budget, reservation and output cap) and wire capture through the check's
own receiver. Only the provider is a stand-in: ``SolWire`` from ``test_reasoning_tool_recovery`` (transcribed
rejection messages in synthetic envelopes; synthetic successful replies). Mythic rows are projected from the
native emissions. This proves the check can pass for a correct product and that a deliberate rejection no longer
stops the run; it proves nothing about a live model's choices.

Adapted from the cold review's reproduction of its blocking finding (2026-11-22).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import sage_chat.service as service
from sage_chat import session as chat_session
from tests import test_reasoning_tool_recovery as recovery
from tests.openai_responses_harness import Process, request

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_reasoning_tools.py"
SPEC = importlib.util.spec_from_file_location("check_reasoning_tools_path", SCRIPT)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
CHANNELS = {"A": recovery.CHANNEL, "B": recovery.CHANNEL + 1}  # two fresh Mythic channels, as the live run uses
ROUTE = {"API_ENDPOINT": check.DIRECT_ENDPOINT, "MODEL_A": recovery.SOL_A, "MODEL_B": recovery.SOL_B,
         "EFFORT": "low"}


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    for name in ("SAGE_REASONING_EFFORT", "SAGE_OPENAI_API", "SAGE_OPENAI_ACCESS_PROGRAM",
                 "SAGE_OPENAI_SAFETY_IDENTIFIER", "SAGE_PROVIDER_RETRY_POLICY"):
        monkeypatch.delenv(name, raising=False)
    async def clear():
        for channel in CHANNELS.values():
            await chat_session.remove_session(str(channel))

    from ai import observation_channel

    observation_channel._reset_after_fork()
    asyncio.run(clear())
    yield
    asyncio.run(clear())
    service._CHANNEL_TURN_LOCKS.clear()
    # The channel's producer identity and sequence are process-wide; a later receiver expects a fresh producer.
    observation_channel._reset_after_fork()


def mythic_rows(window: list[dict]) -> list[dict]:
    """Mythic keeps one row per response key: later emissions under a key update that row (the terminal
    finalizes the last block in place)."""
    rows: dict[str, dict] = {}
    for emission in window:
        text = str(emission.get("content") or emission.get("error") or "")
        key = str(emission.get("response_key") or id(emission))
        row = rows.setdefault(key, {"message": "", "metadata": {}})
        if text:
            row["message"] = text
        row["metadata"] = {**row["metadata"], **(emission.get("metadata") or {})}
    return [row for row in rows.values() if row["message"] or row["metadata"]]


def _run_sequence(monkeypatch, tmp_path, wires: dict, before_step=None):
    """Drive check.STEPS through the real product under the check's settings and capture; returns what verify reads."""
    parent = tmp_path / "capture"
    parent.mkdir(mode=0o700)
    record, ready = tmp_path / "wire.jsonl", tmp_path / "ready.json"
    receiver = subprocess.Popen([sys.executable, str(SCRIPT), "_receive", "--record", str(record),
                                 "--parent", str(parent), "--ready", str(ready)])
    deadline = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", json.loads(ready.read_text())["directory"])
    (tmp_path / "budget.json").write_text(json.dumps(check.budget_config()))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(tmp_path / "budget.json"))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", str(check.TOKEN_RESERVATION))
    monkeypatch.setenv("SAGE_EVAL_MAX_OUTPUT_TOKENS", str(check.CEILINGS["output_tokens_per_call"]))
    current = {"wire": wires["A"]}
    # The metered transport wraps httpx's own transport; the stand-in replaces only that innermost layer.
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(current["wire"].handle))
    monkeypatch.setattr(httpx, "HTTPTransport", lambda *a, **k: httpx.MockTransport(current["wire"].handle))

    marks, rows, steps = {}, {}, {}

    async def scenario():
        processes = {}
        for index, (step, channel, api, effort, prompt, _kind) in enumerate(check.STEPS, start=1):
            current["wire"] = wires[channel]
            if before_step is not None:
                before_step(step)
            if channel not in processes:
                processes[channel] = Process()
            process = processes[channel]
            config = {**check.channel_config(ROUTE, channel, api=api, effort=effort)["config"],
                      "API_KEY": recovery.KEY}
            before = len(check.read_wire(record))
            mark = len(process.chat.emissions)
            await process.chat.chat(request(prompt, index, config, channel=CHANNELS[channel]))
            marks[step] = (before, len(check.read_wire(record)))
            window = process.chat.emissions[mark:]
            terminal = [e for e in window if e in process.chat.terminal_emissions]
            rows[step] = {"status": terminal[-1]["kind"] if terminal else "none", "messages": mythic_rows(window)}
            steps[step] = {"tasks_after": 0}
        for process in processes.values():
            await process.close()

    try:
        asyncio.run(scenario())
    finally:
        (parent / "stop").write_text("")
        receiver.wait(timeout=15)
    records = check.read_wire(record)
    wire = {step: check.exchanges(records[a:b]) for step, (a, b) in marks.items()}
    every = check.exchanges(records)
    run = {"task_baseline": 0, "started_at": 0, "finished_at": 60, "steps": steps}
    totals = {"transport_requests": len(every),
              "unpaired": sum(1 for item in every if item["status"] is None and item["outcome"] is None)}
    return run, wire, rows, totals


def test_the_frozen_sequence_under_the_check_settings_passes_every_criterion(monkeypatch, tmp_path):
    """Synthetic direct-text answers (the direct-text control)."""
    wires = {}
    for channel in ("A", "B"):
        first, third = f"call_{channel.lower()}1", f"call_{channel.lower()}3"
        wires[channel] = recovery.SolWire([
            *recovery._tool_round_trip(first, recovery.SEED_ANSWER, 1),
            *recovery._tool_round_trip(third, "Still amber-seven; WORKSTATION-5.", 3),
            [recovery.text_item("Your label is amber-seven.", 5)],
        ])
    run, wire, rows, totals = _run_sequence(monkeypatch, tmp_path, wires)
    result = check.evaluate(run, wire, rows, totals)
    assert result["verdict"] == "pass", json.dumps(result, indent=1, default=str)
    assert [item["status"] for item in wire["A2"]] == [400] and [item["status"] for item in wire["B2"]] == [400]
    assert all(rows[step]["status"] == "complete" for step in ("A3", "A4", "B1", "B3", "B4"))
    assert {step: detail["attribution"]["path"] for step, detail in result["detail"]["round_trips"].items()} == {
        "A1": "text", "A3": "text", "B1": "text", "B3": "text"}


# --------------------------------------------------------------------------------------------------
# The recorded finish representation (attempt 1's provider output) under the r2 rules, and their mutations
# --------------------------------------------------------------------------------------------------

def _recorded_run(monkeypatch, tmp_path):
    """Attempt 1's recorded outputs and rejection envelopes (fixture; substitutions in its provenance) replayed
    through the real product and the check's own capture."""
    from tests import openai_responses_harness as harness
    from tests import test_finish_history_persistence as recorded

    wires = {"A": recorded.RecordedWire(recorded._outputs("A1", "A3", "A4")),
             "B": recorded.RecordedWire(recorded._outputs("B1", "B3", "B4"))}

    def rows_for(step):
        rows = recorded._recorded_rows(step)
        if rows is not None:
            monkeypatch.setattr(harness, "CALLBACK_ROWS", rows)

    return _run_sequence(monkeypatch, tmp_path, wires, before_step=rows_for)


def test_the_recorded_finish_answers_and_their_history_pass_and_each_mutation_fails(monkeypatch, tmp_path):
    import copy

    run, wire, rows, totals = _recorded_run(monkeypatch, tmp_path)
    result = check.evaluate(run, wire, rows, totals)
    assert result["verdict"] == "pass", json.dumps(result, indent=1, default=str)
    paths = {step: d["attribution"]["path"] for step, d in result["detail"]["round_trips"].items()}
    assert paths == {"A1": "finish", "A3": "finish", "B1": "finish", "B3": "finish"}
    counts = {step: d["counts"] for step, d in result["detail"]["continuity"].items()}
    assert counts["A3"] == {"expected_calls": 2, "retained_calls": 2, "expected_reasoning": 2, "retained_reasoning": 2}
    assert counts["B4"] == {"expected_calls": 2, "retained_calls": 2, "expected_reasoning": 0, "retained_reasoning": 0}

    def first_input(data, step):
        return data[0][step][0]["input"]

    def last_output(data, step):
        return data[0][step][-1]["reply"]["output"]

    def finish_arguments(data, step, **changes):
        (call,) = [item for item in last_output(data, step) if item.get("type") == "function_call"]
        arguments = {**json.loads(call["arguments"]), **changes}
        call["arguments"] = json.dumps(arguments)

    def seed_index(data, step, kind):
        items = first_input(data, step)
        return next(i for i, item in enumerate(items) if item.get("type") == kind)

    def drop(data, step, kind):
        del first_input(data, step)[seed_index(data, step, kind)]

    def rename_result(data, step):
        first_input(data, step)[seed_index(data, step, "function_call_output")]["call_id"] = "call_wrong"

    def duplicate_pair(data, step):
        items = first_input(data, step)
        index = seed_index(data, step, "function_call")
        items[index + 2:index + 2] = copy.deepcopy(items[index:index + 2])

    def unconsumed(data, step):
        later = data[0][step][1]["input"]
        data[0][step][1]["input"] = [item for item in later if item.get("type") != "function_call_output"]

    def second_final(data, step):
        data[1][step]["messages"].append(copy.deepcopy(data[1][step]["messages"][-1]))

    def other_answer(data, step, source):
        answer = [m for m in data[1][source]["messages"] if "Final Report" in m["message"]][-1]["message"]
        for message in data[1][step]["messages"]:
            if "Final Report" in message["message"]:
                message["message"] = answer

    def truncated_final(data, step):
        for message in data[1][step]["messages"]:
            if "Final Report" in message["message"]:
                message["message"] = message["message"][:80]

    def set_field(data, step, field, value):
        data[0][step][0][field] = value

    mutations = {
        "wrong result call id": (lambda d: rename_result(d, "A3"), "4_continuity_output"),
        "missing result": (lambda d: drop(d, "A3", "function_call_output"), "4_continuity_output"),
        "missing reasoning": (lambda d: drop(d, "A4", "reasoning"), "4_continuity_output"),
        "duplicate history": (lambda d: duplicate_pair(d, "B3"), "4_continuity_output"),
        "result never consumed": (lambda d: unconsumed(d, "A1"), "3_both_recovered_seams"),
        "blank finish": (lambda d: finish_arguments(d, "A1", final_response="  "), "3_both_recovered_seams"),
        "reuse without a worker": (lambda d: finish_arguments(d, "B1", final_response="",
                                                                reuse_latest_worker_answer=True),
                                   "3_both_recovered_seams"),
        "contradictory finish": (lambda d: finish_arguments(d, "A3", reuse_latest_worker_answer=True),
                                 "3_both_recovered_seams"),
        "unknown outcome": (lambda d: finish_arguments(d, "B3", outcome="done"), "3_both_recovered_seams"),
        "duplicate final": (lambda d: second_final(d, "A1"), "3_both_recovered_seams"),
        "another request's answer": (lambda d: other_answer(d, "A3", "B3"), "3_both_recovered_seams"),
        "answer substring only": (lambda d: truncated_final(d, "B1"), "3_both_recovered_seams"),
        "missing capture": (lambda d: d[0].__setitem__("A3", []), "1_identity"),
        "wrong effort": (lambda d: set_field(d, "B3", "effort", "medium"), "1_identity"),
        "unsupported route": (lambda d: set_field(d, "A1", "url", "https://proxy.invalid/v1/responses"),
                              "1_identity"),
        "exhausted requests": (lambda d: d[2].__setitem__("transport_requests", 33), "5_containment_evidence"),
    }
    for name, (mutate, criterion) in mutations.items():
        data = copy.deepcopy((wire, rows, totals))
        mutate(data)
        mutated = check.evaluate(run, *data)
        assert mutated["verdict"] == "fail" and mutated["criteria"][criterion] is False, name


# --------------------------------------------------------------------------------------------------
# Readiness: credential source, precedence and recorded inputs (pure rules; names and statuses only)
# --------------------------------------------------------------------------------------------------

READY_CREDENTIALS = {
    "installed": {".env.local": {"present": True, "credentials": [{"name": "API_KEY", "block": ""}],
                                 "competing_provider": []},
                  ".env": {"present": True, "credentials": [], "competing_provider": []}},
    "container": {"container_env": [], "source": ".env.local:API_KEY", "status": 200,
                  "models": {recovery.SOL_A: True, recovery.SOL_B: True}},
    "operator_secrets": {"resolved": True, "operator": 1, "names": []},
}


def test_a_single_installed_openai_key_that_lists_both_models_is_ready():
    assert check.credential_blockers(READY_CREDENTIALS, ROUTE) == []


@pytest.mark.parametrize("path,value", [
    (("installed", ".env.local", "credentials"), [{"name": "API_KEY", "block": ""}] * 2),
    (("installed", ".env.local", "credentials"), [{"name": "API_KEY", "block": "LiteLLM"}]),
    (("installed", ".env.local", "credentials"), [{"name": "OPENAI_API_KEY", "block": ""}]),
    (("installed", ".env.local", "competing_provider"), ["API_ENDPOINT"]),
    (("installed", ".env", "credentials"), [{"name": "API_KEY", "block": ""}]),
    (("container", "container_env"), ["API_KEY"]),
    (("container", "source"), ".env:API_KEY"),
    (("container", "status"), 401),
    (("container", "models"), {recovery.SOL_A: True, recovery.SOL_B: False}),
    (("operator_secrets", "resolved"), False),
    (("operator_secrets", "names"), ["API_KEY"]),
])
def test_each_wrong_key_source_or_shadow_blocks_readiness(path, value):
    import copy

    report = copy.deepcopy(READY_CREDENTIALS)
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert check.credential_blockers(report, ROUTE)


def test_installed_credentials_name_the_block_without_reading_values_into_the_report(tmp_path):
    (tmp_path / ".env.local").write_text("# LiteLLM\n#API_KEY=old\n# OpenAI\nprovider=openai\n"
                                         "API_ENDPOINT=https://api.openai.com/v1\nAPI_KEY=offline-placeholder\n")
    (tmp_path / ".env").write_text("# Model provider\n#API_KEY=\nSAGE_REASONING_EFFORT=\n")
    report = check.installed_credentials(tmp_path)
    assert report[".env.local"] == {"present": True, "credentials": [{"name": "API_KEY", "block": "OpenAI"}],
                                    "competing_provider": []}
    assert report[".env"]["credentials"] == [] and "offline-placeholder" not in json.dumps(report)
    (tmp_path / ".env.local").write_text("API_ENDPOINT=https://proxy.invalid/v1\nAPI_KEY=offline-placeholder\n")
    assert check.installed_credentials(tmp_path)[".env.local"]["competing_provider"] == ["API_ENDPOINT"]


def test_the_recorded_replay_inputs_are_present_for_every_step():
    report = check.recorded_inputs()
    assert all(report[name] for name in check.RECORDED_INPUTS)
    assert report["attempt1_steps"] == [step for step, *_ in check.STEPS]
