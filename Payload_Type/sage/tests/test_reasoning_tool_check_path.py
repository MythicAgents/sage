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

    asyncio.run(clear())
    yield
    asyncio.run(clear())
    service._CHANNEL_TURN_LOCKS.clear()


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


def test_the_frozen_sequence_under_the_check_settings_passes_every_criterion(monkeypatch, tmp_path):
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

    wires = {}
    for channel in ("A", "B"):
        first, third = f"call_{channel.lower()}1", f"call_{channel.lower()}3"
        wires[channel] = recovery.SolWire([
            *recovery._tool_round_trip(first, recovery.SEED_ANSWER, 1),
            *recovery._tool_round_trip(third, "Still amber-seven; WORKSTATION-5.", 3),
            [recovery.text_item("Your label is amber-seven.", 5)],
        ])
    current = {"wire": wires["A"]}
    # The metered transport wraps httpx's own transport; the stand-in replaces only that innermost layer.
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(current["wire"].handle))
    monkeypatch.setattr(httpx, "HTTPTransport", lambda *a, **k: httpx.MockTransport(current["wire"].handle))

    marks, rows, steps = {}, {}, {}

    async def scenario():
        processes = {}
        for index, (step, channel, api, effort, prompt, _kind) in enumerate(check.STEPS, start=1):
            current["wire"] = wires[channel]
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
    result = check.evaluate(run, wire, rows, {
        "transport_requests": len(every),
        "unpaired": sum(1 for item in every if item["status"] is None and item["outcome"] is None)})
    assert result["verdict"] == "pass", json.dumps(result, indent=1, default=str)
    assert [item["status"] for item in wire["A2"]] == [400] and [item["status"] for item in wire["B2"]] == [400]
    assert all(rows[step]["status"] == "complete" for step in ("A3", "A4", "B1", "B3", "B4"))
